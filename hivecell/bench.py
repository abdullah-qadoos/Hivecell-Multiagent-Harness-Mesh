"""Benchmark: the same projects built three ways, scored by hidden acceptance tests.

  solo         one agent owns everything and does the whole job
  team         backend + test agents, ownership split, coordinated through Axonmesh
  team-review  team plus the reviewer agent in the quality gate

Agents never see acceptance_test.py: it is copied into the integration branch only after the run,
so a run "succeeds" only if the merged result really meets the spec.
"""
import asyncio
import os
import shutil
import stat
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from axonmesh import Mesh

from .models import Model, make_model
from .orchestrator import Orchestrator, Plan, TaskSpec
from .rules import AgentRules

PROJECTS_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "projects"
CONFIGS = ["solo", "team", "team-review"]
BASE_TOOLS = ["read_file", "write_file", "list_files", "run_tests", "send_message", "read_contracts"]


def projects() -> list[Path]:
    return sorted(p for p in PROJECTS_DIR.iterdir() if (p / "spec.md").exists())


def setup(config: str, module: str, model: str) -> tuple[dict[str, AgentRules], list[TaskSpec], str | None]:
    lib = f"services/api/{module}.py"
    stdlib = "Use only the Python standard library"
    backend = AgentRules(name="backend", model=model, role=f"Implement {lib} with unit tests in services/api/tests",
                         owns=["services/api/**"], can_read=["tests/**"], tools=BASE_TOOLS,
                         rules=[stdlib, "Every public function needs a unit test"],
                         test_cmd="python -m pytest -q services/api")
    tester = AgentRules(name="test", model=model, role="Write black-box tests in tests/ against the spec",
                        owns=["tests/**"], can_read=["services/api/**"], tools=BASE_TOOLS,
                        rules=[stdlib, "Never edit the code under test; message backend about bugs"],
                        test_cmd="python -m pytest -q tests")
    solo = AgentRules(name="solo", model=model, role=f"Implement {lib} and its tests",
                      owns=["services/api/**", "tests/**"], tools=BASE_TOOLS,
                      rules=[stdlib, "Unit tests in services/api/tests, black-box tests in tests/"],
                      test_cmd="python -m pytest -q")
    reviewer = AgentRules(name="reviewer", model=model, role="Review each task against its rules and the spec",
                          owns=[], can_read=["**"], tools=["read_file", "list_files"],
                          rules=["Request changes only for real problems, citing path:line", "Finish with submit_review"])
    if config == "solo":
        return {"solo": solo}, [TaskSpec(id="all", agent="solo", title=f"Implement {lib} per the spec, with tests")], None
    tasks = [TaskSpec(id="impl", agent="backend", title=f"Implement {lib} per the spec, with unit tests"),
             TaskSpec(id="blackbox", agent="test", kind="test", title="Black-box tests in tests/ for the spec",
                      depends_on=["impl"])]
    team = {"backend": backend, "test": tester}
    if config == "team-review":
        return team | {"reviewer": reviewer}, tasks, "reviewer"
    return team, tasks, None


def run_one(project: Path, config: str, model: str, workdir: Path,
            model_factory: Callable[[str], Model] = make_model) -> dict:
    repo = workdir / f"{project.name}-{config}"
    if repo.exists():  # git marks object files read-only on Windows
        shutil.rmtree(repo, onexc=lambda fn, p, e: (os.chmod(p, stat.S_IWRITE), fn(p)))
    repo.mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost", *a],
                                    cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (repo / "README.md").write_text(f"benchmark: {project.name}\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")

    rules, tasks, reviewer = setup(config, project.name, model)
    plan = Plan(spec=(project / "spec.md").read_text(encoding="utf-8"), agents=[], tasks=tasks, reviewer=reviewer)
    mesh = Mesh(repo, allowlist_path=workdir / "allowlist.json")
    started = time.time()
    try:
        report = asyncio.run(Orchestrator(plan, rules, mesh, "auto", model_factory, notify=lambda m: None).run())
    finally:
        mesh.close()
    accepted = False
    if report.integration:
        wt = repo / ".hivecell" / "worktrees" / report.run_id / "integration"
        shutil.copy(project / "acceptance_test.py", wt / "acceptance_test.py")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "acceptance_test.py"],
                           cwd=wt, capture_output=True, text=True, timeout=300)
        accepted = r.returncode == 0
    return {"project": project.name, "config": config, "status": report.status, "accepted": accepted,
            "tokens": report.tokens, "cost": report.cost, "seconds": round(time.time() - started, 1),
            "retries": report.retries, "rejected": report.rejected_calls, "messages": report.messages}


def to_markdown(rows: list[dict], model: str) -> str:
    out = [f"# Benchmark results", "", f"Model: `{model}`, {time.strftime('%Y-%m-%d')}", "",
           "| config | success | avg tokens | avg cost $ | avg seconds | retries | rejected calls |",
           "|---|---|---|---|---|---|---|"]
    for c in dict.fromkeys(r["config"] for r in rows):
        rs = [r for r in rows if r["config"] == c]
        ok = sum(r["accepted"] for r in rs)
        out.append(f"| {c} | {ok}/{len(rs)} | {statistics.mean(r['tokens'] for r in rs):,.0f} | "
                   f"{statistics.mean(r['cost'] for r in rs):.4f} | {statistics.mean(r['seconds'] for r in rs):.0f} | "
                   f"{sum(r['retries'] for r in rs)} | {sum(r['rejected'] for r in rs)} |")
    out += ["", "| project | config | run | hidden tests | tokens | seconds | retries |", "|---|---|---|---|---|---|---|"]
    out += [f"| {r['project']} | {r['config']} | {r['status']} | {'pass' if r['accepted'] else 'FAIL'} | "
            f"{r['tokens']:,} | {r['seconds']} | {r['retries']} |" for r in rows]
    return "\n".join(out) + "\n"


def main(model: str, configs: list[str], names: list[str] | None, out: Path, workdir: Path) -> int:
    chosen = [p for p in projects() if not names or p.name in names]
    rows = []
    for project in chosen:
        for config in configs:
            print(f"{project.name} / {config} ...", flush=True)
            try:
                rows.append(run_one(project, config, model, workdir))
            except Exception as e:  # one broken run shouldn't lose the others' results
                print(f"  error: {type(e).__name__}: {e}")
                rows.append({"project": project.name, "config": config, "status": "error", "accepted": False,
                             "tokens": 0, "cost": 0, "seconds": 0, "retries": 0, "rejected": 0, "messages": 0})
            print(f"  {rows[-1]['status']}, hidden tests {'pass' if rows[-1]['accepted'] else 'FAIL'}")
    out.write_text(to_markdown(rows, model), encoding="utf-8")
    print(f"results: {out}")
    return 0
