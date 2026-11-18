import asyncio
from graphlib import CycleError
from pathlib import Path

import pytest

from axonmesh import Mesh
from conftest import FakeModel, call, rules
from hivecell.orchestrator import Orchestrator, Plan, format_report, load_plan
from hivecell.sandbox import git

CALC = "def add(a, b):\n    return a + b\n"
GOOD_TEST = "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\nfrom calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
BAD_TEST = GOOD_TEST.replace("== 5", "== 6")
SRC_TEST = GOOD_TEST.replace("parents[1]", "parent")
TESTS_TEST = GOOD_TEST.replace("parents[1]", "parents[1] / 'src'")

TASKS = [
    {"id": "calc", "agent": "backend", "title": "write calc"},
    {"id": "tests", "agent": "test", "title": "test calc", "depends_on": ["calc"], "kind": "test"},
]


def make(repo, tmp_path, scripts, tasks, mode="auto", **plan_kw):
    agents = {
        "backend": rules(name="backend", owns=["src/**"], test_cmd="python -m pytest -q src",
                         tools=["read_file", "write_file", "list_files", "send_message"]),
        "test": rules(name="test", owns=["tests/**"], can_read=["src/**"], test_cmd="python -m pytest -q tests"),
    }
    agents = {n: a.model_copy(update={"model": f"fake/{n}"}) for n, a in agents.items()}
    models = {name: FakeModel(turns) for name, turns in scripts.items()}
    mesh = Mesh(repo, allowlist_path=tmp_path / "allow.json")
    plan = Plan(spec="calc", agents=[], tasks=tasks, **plan_kw)
    orch = Orchestrator(plan, agents, mesh, mode, lambda spec: models[spec.split("/")[1]], notify=lambda m: None)
    return orch, asyncio.run(orch.run()), models


GOOD_SCRIPTS = {
    "backend": [[call("write_file", path="src/calc.py", content=CALC),
                 call("write_file", path="src/test_calc.py", content=SRC_TEST)],
                [call("send_message", to="test", body="calc.add is ready, ints only")],
                [call("report_done", summary="calc done")]],
    "test": [[call("read_file", path="src/calc.py")],
             [call("write_file", path="tests/test_add.py", content=TESTS_TEST)],
             [call("report_done", summary="tests done")]],
}


def test_two_agents_through_axonmesh(repo, tmp_path):
    orch, report, models = make(repo, tmp_path, GOOD_SCRIPTS, TASKS)
    assert report.status == "passed", format_report(report)
    assert [t.status for t in report.tasks] == ["done", "done"]
    assert report.tokens == 6 * 200 and report.messages == 1
    # the test agent got the backend's message injected, marked as untrusted peer content
    first_prompt = [m for m in models["test"].last_messages if m["role"] == "user"]
    assert any("untrusted peer content" in m["content"] and "ints only" in m["content"] for m in first_prompt)
    # integration branch has everyone's work; main is untouched
    files = set(git(repo, "ls-tree", "-r", "--name-only", report.integration).split())
    assert {"src/calc.py", "tests/test_add.py"} <= files
    assert git(repo, "ls-tree", "-r", "--name-only", "main").split() == ["README.md"]
    m = orch.mesh
    assert m.store.one("select status from runs")["status"] == "passed"
    assert {r["status"] for r in m.store.q("select status from tasks")} == {"complete"}
    assert m.store.one("select count(*) n from evidence where source='ci'")["n"] >= 4
    assert not m.active_claims()  # released after a passing run
    assert (repo / ".axonmesh" / "review" / f"{orch.tids['calc']}.md").exists()


def test_failed_gate_retries_with_feedback(repo, tmp_path):
    scripts = {"backend": [
        [call("write_file", path="src/calc.py", content=CALC),
         call("write_file", path="src/test_calc.py", content=BAD_TEST.replace("parents[1]", "parent"))],
        [call("report_done", summary="first try")],
        [call("write_file", path="src/test_calc.py", content=SRC_TEST)],
        [call("report_done", summary="fixed")],
    ]}
    orch, report, models = make(repo, tmp_path, scripts, TASKS[:1])
    assert report.status == "passed", format_report(report)
    assert report.tasks[0].attempts == 2 and report.retries == 1
    assert "tests failed" in models["backend"].last_messages[0]["content"]


def test_failure_skips_dependents_and_keeps_soft_locks(repo, tmp_path):
    orch, report, _ = make(repo, tmp_path, {"backend": [], "test": []}, TASKS, max_attempts=1)
    assert report.status == "failed"
    assert [t.status for t in report.tasks] == ["failed", "skipped"]
    assert {c["lock_type"] for c in orch.mesh.active_claims()} == {"soft"}


def test_external_claim_blocks_run_before_any_agent_starts(repo, tmp_path):
    mesh = Mesh(repo, allowlist_path=tmp_path / "allow.json")
    ext = mesh.register("claude-code", "external", "claude-code", program_path="C:/x/claude.exe")
    mesh.decide_agent(ext, True)
    mesh.claim(ext, ["src/calc.py"])
    orch, report, models = make(repo, tmp_path, GOOD_SCRIPTS, TASKS)
    assert report.status == "blocked"
    assert any("claude-code" in o for o in report.overlaps_prevented)
    assert models["backend"].turns == GOOD_SCRIPTS["backend"]  # no agent ever ran


def test_plan_mode_only_proposes(repo, tmp_path):
    scripts = {"backend": [[call("write_file", path="src/x.py", content="no")],
                           [call("report_done", summary="I would build calc.add")]]}
    orch, report, _ = make(repo, tmp_path, scripts, TASKS[:1], mode="plan")
    assert report.status == "proposed"
    assert report.tasks[0].proposal == "I would build calc.add"
    assert report.rejected_calls == 1  # the write was refused in plan mode
    assert not (orch.worktrees["backend"] / "src/x.py").exists()


def test_cycle_is_rejected_before_any_agent_runs(repo, tmp_path):
    tasks = [{"id": "a", "agent": "backend", "title": "a", "depends_on": ["b"]},
             {"id": "b", "agent": "backend", "title": "b", "depends_on": ["a"]}]
    with pytest.raises(CycleError):
        make(repo, tmp_path, {"backend": []}, tasks)


def test_example_plan_loads():
    plan, agents = load_plan(Path(__file__).parents[1] / "examples/todo_api.yaml")
    assert set(agents) == {"backend", "test", "reviewer"} and len(plan.tasks) == 2
    assert plan.reviewer == "reviewer" and plan.contracts[0].name == "todo-store"
