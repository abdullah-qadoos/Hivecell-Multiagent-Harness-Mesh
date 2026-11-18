"""Architect, reviewer, Claude Code hook, setup/doctor, desktop app bridge and benchmark."""
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from axonmesh import Mesh
from axonmesh import setup as S
from axonmesh.hook import run_hook
from conftest import FakeModel, call, rules
from hivecell import bench
from hivecell.architect import make_plan
from hivecell.orchestrator import Orchestrator, Plan, format_report, load_plan

ROOT = Path(__file__).parents[1]


# ---------- architect ----------
def test_architect_is_corrected_until_the_plan_is_valid(repo, tmp_path):
    (repo / ".env").write_text("SECRET=1")
    bad = "tasks:\n  - {id: a, agent: nobody, title: x}\n"
    cycle = "tasks:\n  - {id: a, agent: backend, title: x, depends_on: [b]}\n  - {id: b, agent: backend, title: y, depends_on: [a]}\n"
    good = ("tasks:\n  - {id: impl, agent: backend, title: build it}\n"
            "  - {id: tests, agent: test, kind: test, title: test it, depends_on: [impl]}\n"
            "contracts:\n  - {name: api, schema: 'add(a, b) -> int'}\nreviewer: reviewer\n")
    model = FakeModel([[call("read_file", path=".env")], [call("submit_plan", plan_yaml=bad)],
                       [call("submit_plan", plan_yaml=cycle)], [call("submit_plan", plan_yaml=good)]])
    out = tmp_path / "plan.yaml"
    text, res = make_plan("build an adder", repo, sorted((ROOT / "agents").glob("*.yaml")),
                          rules(name="architect", tools=["read_file", "list_files"]), model, out, lambda t, p: None)
    assert res.status == "done" and out.exists()
    results = [m["content"] for m in model.last_messages if m["role"] == "tool"]
    assert results[0].startswith("REJECTED: hidden")  # .env never reaches the model
    assert "unknown agents" in results[1] and "cycle" in results[2] and results[3] == "plan accepted"
    plan, agents = load_plan(out)  # the written file is a runnable plan
    assert set(agents) == {"backend", "test", "reviewer"} and plan.contracts[0].schema_ == "add(a, b) -> int"


# ---------- reviewer ----------
CALC = "def add(a, b):\n    return a + b\n"
TEST = "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\nfrom calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def test_reviewer_rejection_sends_work_back(repo, tmp_path):
    agents = {"backend": rules(name="backend", model="fake/backend", owns=["src/**"], test_cmd="python -m pytest -q src"),
              "reviewer": rules(name="reviewer", model="fake/reviewer", owns=[], can_read=["**"],
                                tools=["read_file", "list_files"])}
    models = {
        "backend": FakeModel([[call("write_file", path="src/calc.py", content=CALC),
                               call("write_file", path="src/test_calc.py", content=TEST)],
                              [call("report_done", summary="v1")],
                              [call("write_file", path="src/calc.py", content='"""Adds."""\n' + CALC)],
                              [call("report_done", summary="v2 with docstring")]]),
        "reviewer": FakeModel([[call("read_diff")], [call("submit_review", approved=False, findings="src/calc.py:1: no docstring")],
                               [call("read_diff")], [call("submit_review", approved=True, findings="looks good")]]),
    }
    plan = Plan(spec="adder", agents=[], tasks=[{"id": "calc", "agent": "backend", "title": "adder"}],
                reviewer="reviewer", contracts=[{"name": "adder", "schema": "add(a, b)"}])
    mesh = Mesh(repo, allowlist_path=tmp_path / "a.json")
    orch = Orchestrator(plan, agents, mesh, "auto", lambda s: models[s.split("/")[1]], notify=lambda m: None)
    report = asyncio.run(orch.run())
    assert report.status == "passed", format_report(report)
    assert report.tasks[0].attempts == 2
    assert "no docstring" in models["backend"].last_messages[0]["content"]  # findings came back as feedback
    diff_seen = [m["content"] for m in models["reviewer"].last_messages if m["role"] == "tool"][0]
    assert "+def add" in diff_seen
    sources = {r["source"] for r in mesh.store.q("select source from evidence")}
    assert {"ci", "reviewer", "agent_reported"} <= sources
    assert mesh.contracts()[0]["name"] == "adder"


def test_reviewer_cannot_own_tasks():
    with pytest.raises(ValueError, match="review its own"):
        from hivecell.orchestrator import check_agents
        check_agents(Plan(spec="x", agents=[], tasks=[{"id": "a", "agent": "r", "title": "t"}], reviewer="r"),
                     {"r": rules(name="r")})


# ---------- Claude Code hook ----------
def test_hook_logs_tool_calls_and_delivers_messages(repo, tmp_path):
    mesh = Mesh(repo, allowlist_path=tmp_path / "a.json")
    cc = mesh.register("claude-code", "external", "claude-code", program_path="C:/claude.exe", client_pid=4242)
    other = mesh.register("codex", "external", "codex", program_path="C:/codex.exe")
    mesh.decide_agent(cc, True)
    mesh.decide_agent(other, True)
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "a.py"}, "cwd": str(repo)})

    assert run_hook(payload, pids=[1, 4242], mesh=mesh) is None  # nothing unread
    assert mesh.store.one("select count(*) n from events where type='external_tool' and agent_id=?", cc)["n"] == 1

    mesh.send(other, cc, "contract changed: id is a string", urgent=True)
    out = json.loads(run_hook(payload, pids=[4242], mesh=mesh))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "untrusted peer content" in ctx and "id is a string" in ctx and "ack=" in ctx
    assert run_hook(payload, pids=[999], mesh=mesh) is None  # not an Axonmesh agent's process


def test_hook_command_never_fails(tmp_path):
    r = subprocess.run([sys.executable, "-m", "axonmesh.cli", "--repo", str(tmp_path), "hook"], input="not json",
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and r.stdout == ""


# ---------- setup / doctor ----------
def test_setup_shows_applies_and_undoes_exactly(tmp_path):
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / ".claude").mkdir(parents=True)
    mine = {"permissions": {"allow": ["Bash(ls:*)"]}, "theme": "dark"}
    (repo / ".claude" / "settings.json").write_text(json.dumps(mine))
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "x"\n')
    log = home / ".axonmesh" / "setup-log.json"

    for target in ("claude-code", "codex"):
        changes = S.plan(target, repo, home)
        assert "+" in S.show(changes)
        S.apply(changes, log)
        assert S.plan(target, repo, home) == []  # idempotent
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    allow = settings["permissions"]["allow"]
    assert "Bash(ls:*)" in allow and "mcp__axonmesh__start_work" in allow
    assert not any("*" in a for a in allow if "axonmesh" in a)  # exact names, never wildcards
    assert "hook" in settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert (repo / ".claude" / "settings.json.axon-backup").exists()
    assert any(l.startswith("OK  claude-code") for l in S.doctor(repo, home))

    S.undo(log)
    assert json.loads((repo / ".claude" / "settings.json").read_text()) == {**mine, "hooks": {"PostToolUse": []}}
    assert (home / ".codex" / "config.toml").read_text().strip() == 'model = "x"'
    assert any(l.startswith("FIX claude-code") for l in S.doctor(repo, home))


# ---------- desktop app bridge (no window needed) ----------
def test_app_bridge_full_flow(repo, tmp_path, monkeypatch):
    from hivecell.app.api import Api

    monkeypatch.setenv("AXONMESH_HOME", str(tmp_path / "home"))
    plan = tmp_path / "plan.yaml"
    backend = {"name": "backend", "model": "fake/backend", "role": "r", "owns": ["src/**"],
               "tools": ["write_file", "run_command"], "test_cmd": "python -c \"print('ok')\""}
    (tmp_path / "backend.yaml").write_text(yaml.safe_dump(backend))
    plan.write_text(yaml.safe_dump({"spec": "x", "agents": ["backend.yaml"],
                                    "tasks": [{"id": "t", "agent": "backend", "title": "t"}]}))
    model = FakeModel([[call("write_file", path="src/a.py", content="x = 1")],
                       [call("run_command", command="rm -rf /")],  # ask mode: waits for the user
                       [call("report_done", summary="done")]])
    api = Api(model_factory=lambda s: model, config_path=tmp_path / "app.json")

    assert not api.state()["ok"] and "open a repository" in api.state()["error"]  # errors, never tracebacks
    assert not api.open_repo(str(tmp_path))["ok"]  # not a git repo
    assert api.open_repo(str(repo))["ok"]
    assert Api(config_path=tmp_path / "app.json").init()["repo"] == str(api._mesh.root)  # remembered

    assert api.start_run(str(plan), "ask")["ok"]
    for _ in range(100):  # the run is waiting on the approval
        reqs = api.approvals()["requests"]
        if reqs:
            break
        time.sleep(0.1)
    assert reqs and "rm -rf" in reqs[0]["request"]
    assert any("APPROVAL NEEDED" in n["text"] for n in api.notices()["notices"])
    assert api.decide("request", str(reqs[0]["id"]), False)["ok"]
    for _ in range(100):
        job = api.jobs()["jobs"][0]
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "passed", job["report"]
    assert job["run_id"] and "rejected tool calls 1" in job["report"]  # the denied command
    assert any(e["type"] == "approval_decided" for e in api.events(job["run_id"])["events"])
    assert api.state(job["run_id"])["tasks"][0]["status"] == "complete"

    assert "+" in api.setup_preview("claude-code")["diff"]
    assert not api.setup_preview("bogus")["ok"]
    assert not api.save_plan(str(tmp_path / "bad.yaml"), "tasks: nope")["ok"]  # never saves an unrunnable plan
    assert not api.set_key("PATH", "x")["ok"]  # only provider key names
    monkeypatch.setenv("GEMINI_API_KEY", "sk-secret-value")
    keys = api.keys()["keys"]
    assert all(set(k) == {"name", "set", "provider", "url", "note"} for k in keys)  # values never sent to the page
    assert "sk-secret-value" not in json.dumps(keys)
    assert next(k for k in keys if k["name"] == "GEMINI_API_KEY")["set"]
    assert not api.open_key_page("PATH")["ok"]  # only the fixed provider pages can be opened

    s = api.plan_summary("spec: x\nagents: []\ntasks:\n- {id: b, agent: x, title: B, depends_on: [a]}\n- {id: a, agent: x, title: A}\n")
    assert [t["id"] for t in s["tasks"]] == ["a", "b"]  # shown in the order they run
    assert not api.plan_summary("tasks: [nope")["ok"]
    assert {t["target"] for t in api.setup_status()["tools"]} == {"claude-code", "cursor", "gemini", "codex"}


# ---------- creating agents ----------
CLIPPER = {"name": "clipper", "category": "media", "model": "anthropic/claude-haiku-5-5", "role": "cut clips",
           "owns": ["clips/**"], "can_read": ["input/**"], "tools": ["read_file", "write_file", "run_command"],
           "commands": ["ffmpeg *"], "image": "jrottenberg/ffmpeg:6-ubuntu", "test_cmd": None}


def test_agent_library_save_merge_and_delete(repo, tmp_path, monkeypatch):
    from hivecell import library as L

    monkeypatch.setenv("HIVECELL_HOME", str(tmp_path / "hx"))
    L.save(CLIPPER, "personal", None)
    with pytest.raises(ValueError, match="already exists"):
        L.save(CLIPPER, "personal", None)  # new agents never overwrite silently
    L.save({**CLIPPER, "role": "repo version"}, "repo", repo)
    agents = L.list_agents(repo)
    assert agents["clipper"]["scope"] == "repo" and agents["clipper"]["rules"].role == "repo version"  # repo wins
    assert L.list_agents(None)["clipper"]["scope"] == "personal"

    L.save({**CLIPPER, "name": "clipper2"}, "personal", None, replace="clipper")  # rename while editing
    assert not (L.personal_dir() / "clipper.yaml").exists() and (L.personal_dir() / "clipper2.yaml").exists()

    for bad, why in [({**CLIPPER, "name": "Bad Name"}, "name"), ({**CLIPPER, "name": "architect"}, "reserved"),
                     ({**CLIPPER, "tools": ["rm_rf"]}, "unknown tools")]:
        with pytest.raises(Exception, match=why):
            L.save(bad, "personal", None)

    L.save({**CLIPPER, "name": "editor", "owns": ["clips/final/**"]}, "personal", None)
    assert any("clipper2" in w and "editor" in w for w in L.overlap_warnings(L.list_agents(None)))

    (L.personal_dir() / "broken.yaml").write_text("name: [oops")
    assert L.list_agents(None)["broken"]["error"]  # broken files are shown, not hidden
    L.delete("editor", "personal", None)
    assert "editor" not in L.list_agents(None)
    assert {"clipper", "transcriber", "picker", "researcher", "backend"} <= set(L.templates())
    assert "architect" not in L.templates()


def test_describe_an_agent_and_the_ai_drafts_it(tmp_path, monkeypatch):
    from hivecell.app.api import Api

    monkeypatch.setenv("HIVECELL_HOME", str(tmp_path / "hx"))
    bad = "name: Clip Maker\nrole: x\nowns: []\ntools: []\n"
    good = yaml.safe_dump({k: v for k, v in CLIPPER.items() if k != "model"})
    model = FakeModel([[call("submit_agent", agent_yaml=bad)], [call("submit_agent", agent_yaml=good)]])
    api = Api(model_factory=lambda s: model, config_path=tmp_path / "app.json")
    r = api.draft_agent("cut my podcast into vertical clips")
    assert r["ok"] and r["data"]["name"] == "clipper" and r["data"]["commands"] == ["ffmpeg *"]
    assert r["data"]["model"]  # filled in when the description didn't pick one
    assert "REJECTED" in [m for m in model.last_messages if m["role"] == "tool"][0]["content"]
    assert api.save_agent(r["data"], "personal")["ok"]
    assert [a["name"] for a in api.agents()["agents"]] == ["clipper"]
    assert not api.save_agent({**r["data"], "name": ""}, "personal")["ok"]
    assert not api.draft_plan("x")["ok"]  # no repository open: a clear error, not a crash


def test_agent_without_done_check_is_judged_by_the_reviewer(repo, tmp_path):
    agents = {"writer": rules(name="writer", model="fake/writer", owns=["out/**"], test_cmd=None),
              "reviewer": rules(name="reviewer", model="fake/reviewer", owns=[], tools=["read_file"])}
    models = {"writer": FakeModel([[call("write_file", path="out/post.md", content="# Hello")],
                                   [call("report_done", summary="wrote it")]]),
              "reviewer": FakeModel([[call("submit_review", approved=True, findings="fine")]])}
    plan = Plan(spec="post", agents=[], tasks=[{"id": "post", "agent": "writer", "title": "write"}], reviewer="reviewer")
    mesh = Mesh(repo, allowlist_path=tmp_path / "a.json")
    report = asyncio.run(Orchestrator(plan, agents, mesh, "auto", lambda s: models[s.split("/")[1]],
                                      notify=lambda m: None).run())
    assert report.status == "passed", format_report(report)
    assert {r["source"] for r in mesh.store.q("select source from evidence")} == {"reviewer", "agent_reported"}


# ---------- benchmark ----------
@pytest.mark.parametrize("project", bench.projects(), ids=lambda p: p.name)
def test_reference_solutions_pass_hidden_tests(project, tmp_path):
    (tmp_path / "services" / "api").mkdir(parents=True)
    shutil.copy(project / "solution.py", tmp_path / "services" / "api" / f"{project.name}.py")
    shutil.copy(project / "acceptance_test.py", tmp_path / "acceptance_test.py")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "acceptance_test.py"],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_bench_runner_scores_with_hidden_tests(tmp_path):
    project = next(p for p in bench.projects() if p.name == "lru")
    solution = (project / "solution.py").read_text()
    unit = ("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\nfrom lru import LRUCache\n\n"
            "def test_put_get():\n    c = LRUCache(1)\n    c.put(1, 1)\n    assert c.get(1) == 1\n")
    script = [[call("write_file", path="services/api/lru.py", content=solution),
               call("write_file", path="services/api/tests/test_lru.py", content=unit)],
              [call("report_done", summary="done")]]
    row = bench.run_one(project, "solo", "fake/solo", tmp_path, lambda s: FakeModel(script))
    assert row["status"] == "passed" and row["accepted"] and row["tokens"] == 400
    # a wrong-but-self-tested solution passes its own gate but fails the hidden tests
    wrong = [[call("write_file", path="services/api/lru.py", content=solution.replace("last=False", "last=True")),
              call("write_file", path="services/api/tests/test_lru.py", content=unit)],
             [call("report_done", summary="done")]]
    row = bench.run_one(project, "solo", "fake/solo", tmp_path, lambda s: FakeModel(wrong))
    assert row["status"] == "passed" and not row["accepted"]
    assert "| solo | 0/1 |" in bench.to_markdown([row], "fake/solo")
