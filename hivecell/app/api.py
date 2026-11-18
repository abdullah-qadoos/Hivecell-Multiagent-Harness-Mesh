"""The desktop app's bridge: the window calls these methods directly (pywebview js_api), no server, no port.

No GUI code in here, so all of it is testable. Every public method returns {"ok": True, ...} or
{"ok": False, "error": "..."}; the UI never sees a Python traceback.
"""
import asyncio
import functools
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from axonmesh import Mesh
from axonmesh import setup as S

from .. import library as L
from ..models import Model, make_model
from ..orchestrator import Orchestrator, format_report, load_plan
from ..rules import load_rules
from ..sandbox import check_repo

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENTS = ROOT / "agents"
PROVIDER_KEYS = ["ANTHROPIC_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "OPENCODE_API_KEY", "NVIDIA_API_KEY",
                 "OPENAI_API_KEY"]
# key -> (provider, where to get one, note). The page can only open these exact URLs.
PROVIDER_INFO = {
    "ANTHROPIC_API_KEY": ("Anthropic Claude", "https://console.anthropic.com/settings/keys", "Paid"),
    "GEMINI_API_KEY": ("Google Gemini", "https://aistudio.google.com/apikey", "Free tier"),
    "XAI_API_KEY": ("xAI Grok", "https://console.x.ai", "Paid"),
    "OPENCODE_API_KEY": ("OpenCode Zen", "https://opencode.ai/zen", "Paid"),
    "NVIDIA_API_KEY": ("NVIDIA NIM", "https://build.nvidia.com", "Free credits"),
    "OPENAI_API_KEY": ("OpenAI", "https://platform.openai.com/api-keys", "Paid"),
}
SETUP_TARGETS = [
    ("claude-code", "Claude Code", "Registers Axonmesh, pre-approves its tools, adds the message hook."),
    ("cursor", "Cursor", "Registers Axonmesh in this repository's Cursor settings."),
    ("gemini", "Gemini CLI", "Registers Axonmesh and trusts only its tools."),
    ("codex", "Codex", "Adds Axonmesh to your Codex config (user level)."),
]


def safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            out = fn(self, *a, **kw)
            return {"ok": True, **(out or {})}
        except Exception as e:
            return {"ok": False, "error": f"{e}" or type(e).__name__}
    return wrapper


def _rows(mesh: Mesh, sql: str, *params) -> list[dict]:
    return [dict(r) for r in mesh.store.q(sql, *params)]


class Api:
    def __init__(self, model_factory: Callable[[str], Model] = make_model, agents_dir: Path = DEFAULT_AGENTS,
                 config_path: Path | None = None):
        # underscore names are never exposed to the page
        self._model_factory, self._agents_dir = model_factory, Path(agents_dir)
        self._config_path = config_path or Path(os.environ.get("AXONMESH_HOME", Path.home() / ".axonmesh")) / "app.json"
        self._window = None
        self._mesh: Mesh | None = None
        self._jobs: dict[str, dict] = {}
        self._notices: list[dict] = []
        self._lock = threading.Lock()

    # ---------- helpers ----------
    def _need_mesh(self) -> Mesh:
        if not self._mesh:
            raise RuntimeError("open a repository first")
        return self._mesh

    def _config(self) -> dict:
        return json.loads(self._config_path.read_text(encoding="utf-8")) if self._config_path.exists() else {}

    def _save_config(self, **changes):
        cfg = self._config() | changes
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def _notify(self, text: str):
        with self._lock:
            self._notices.append({"time": time.time(), "text": text})

    def _dialog(self, kind, **kw):
        import webview

        if not self._window:
            raise RuntimeError("no window")
        result = self._window.create_file_dialog(kind, **kw)
        if not result:
            return None
        return result if isinstance(result, str) else result[0]

    # ---------- repository ----------
    @safe
    def init(self):
        """Called once when the window loads: reopen the last repository."""
        last = self._config().get("repo")
        if last and Path(last).exists():
            try:
                self.open_repo.__wrapped__(self, last)
            except Exception:
                pass
        return {"repo": str(self._mesh.root) if self._mesh else None, "agents_dir": str(self._agents_dir)}

    @safe
    def open_repo(self, path: str):
        root = Path(path).resolve()
        check_repo(root)
        if self._mesh:
            self._mesh.close()
        self._mesh = Mesh(root)
        self._save_config(repo=str(self._mesh.root))
        return {"repo": str(self._mesh.root)}

    @safe
    def pick_folder(self):
        import webview

        return {"path": self._dialog(webview.FileDialog.FOLDER)}

    @safe
    def pick_file(self, kind: str = "plan"):
        import webview

        types = ("YAML plan (*.yaml;*.yml)",) if kind == "plan" else ("Spec (*.md;*.txt)", "All files (*.*)")
        return {"path": self._dialog(webview.FileDialog.OPEN, file_types=types)}

    @safe
    def read_text(self, path: str):
        return {"text": Path(path).read_text(encoding="utf-8")}

    # ---------- live data (what the dashboard used to serve) ----------
    @safe
    def state(self, run: str | None = None):
        m = self._need_mesh()
        run_sql, rp = (" and run_id=?", [run]) if run else ("", [])
        agents = _rows(m, f"select * from agents where workspace_id=?{run_sql}", m.ws, *rp)
        for a in agents:
            a["online"] = m.online(a)
            a.pop("rules_yaml", None)
        return {"agents": agents,
                "tasks": _rows(m, f"select * from tasks where workspace_id=?{run_sql} order by id", m.ws, *rp),
                "runs": _rows(m, "select id, mode, status, started_at, total_tokens, cost from runs "
                                 "where workspace_id=? order by started_at desc", m.ws)}

    @safe
    def events(self, run: str | None = None, after: int = 0):
        m = self._need_mesh()
        run_sql, rp = (" and run_id=?", [run]) if run else ("", [])
        rows = _rows(m, f"select * from events where workspace_id=? and id>?{run_sql} order by id desc limit 2000",
                     m.ws, after, *rp)
        return {"events": rows[::-1]}

    @safe
    def messages(self, run: str | None = None):
        m = self._need_mesh()
        run_sql, rp = (" and run_id=?", [run]) if run else ("", [])
        return {"messages": _rows(m, f"select * from messages where workspace_id=?{run_sql} order by id", m.ws, *rp)}

    # ---------- runs ----------
    @safe
    def start_run(self, plan_path: str, mode: str = "ask"):
        if mode not in ("plan", "auto", "ask"):
            raise ValueError("mode must be plan, auto or ask")
        mesh = self._need_mesh()
        plan, rules = load_plan(plan_path)
        orch = Orchestrator(plan, rules, mesh, mode, self._model_factory, notify=self._notify)
        jid = str(len(self._jobs) + 1)
        job = {"id": jid, "plan": plan_path, "mode": mode, "status": "running", "report": "", "started": time.time()}
        self._jobs[jid] = job

        def work():
            try:
                report = asyncio.run(orch.run())
                job["status"], job["report"] = report.status, format_report(report)
            except Exception as e:
                job["status"], job["report"] = "error", f"{type(e).__name__}: {e}"
            self._notify(f"Run {job.get('run_id') or jid} finished: {job['status']}")

        def watch_id():  # the run id exists once the orchestrator has started
            while job["status"] == "running" and not orch.run_id:
                time.sleep(0.1)
            job["run_id"] = orch.run_id

        threading.Thread(target=work, daemon=True).start()
        threading.Thread(target=watch_id, daemon=True).start()
        self._save_config(last_plan=plan_path)
        return {"job": jid}

    @safe
    def set_run_mode(self, run_id: str, mode: str):
        self._need_mesh().set_run_mode(run_id, mode)
        for job in self._jobs.values():
            if job.get("run_id") == run_id:
                job["mode"] = mode
        return {}

    @safe
    def jobs(self):
        return {"jobs": sorted(self._jobs.values(), key=lambda j: -j["started"])}

    @safe
    def notices(self, since: float = 0):
        with self._lock:
            return {"notices": [n for n in self._notices if n["time"] > since]}

    # ---------- agents ----------
    def _repo_root(self):
        return self._mesh.root if self._mesh else None

    @safe
    def agents(self):
        found = L.list_agents(self._repo_root())
        rows = [{"name": a["name"], "scope": a["scope"], "path": a["path"], "error": a["error"],
                 "data": a["rules"].model_dump() if a["rules"] else None} for a in found.values()]
        return {"agents": rows, "warnings": L.overlap_warnings(found), "has_repo": bool(self._mesh)}

    @safe
    def agent_templates(self):
        return {"templates": [r.model_dump() for r in L.templates().values()]}

    @safe
    def save_agent(self, data: dict, scope: str = "personal", replace: str | None = None):
        return {"path": str(L.save(data, scope, self._repo_root(), replace))}

    @safe
    def delete_agent(self, name: str, scope: str):
        L.delete(name, scope, self._repo_root())
        return {}

    @safe
    def draft_agent(self, description: str):
        if not description.strip():
            raise ValueError("describe what the agent should do")
        arch = load_rules(self._agents_dir / "architect.yaml")
        designer = arch.model_copy(update={
            "name": "agent-designer", "role": "Design one agent from a plain-words description",
            "rules": ["Pick a clear name and a one-sentence role", "Give it only the folders and tools it needs",
                      "Finish by calling submit_agent; if it is rejected, fix it and submit again"]})
        agent, res = L.draft(description, designer, self._model_factory(designer.model))
        if not agent:
            raise RuntimeError(f"couldn't draft an agent ({res.status})")
        return {"data": agent.model_dump(), "tokens": res.input_tokens + res.output_tokens}

    # ---------- plan (architect) ----------
    @safe
    def draft_plan(self, spec: str):
        from ..architect import make_plan

        mesh = self._need_mesh()
        if not spec.strip():
            raise ValueError("write a spec first")
        files = [Path(a["path"]) for a in L.list_agents(mesh.root).values() if a["rules"]]
        if not files:
            raise ValueError("add some agents first (Agents screen), the architect plans with them")
        arch = load_rules(self._agents_dir / "architect.yaml")
        out = mesh.root / ".hivecell" / "plans" / time.strftime("plan-%Y%m%d-%H%M%S.yaml")
        out.parent.mkdir(parents=True, exist_ok=True)
        text, res = make_plan(spec, mesh.root, files, arch, self._model_factory(arch.model), out, lambda t, p: None)
        if not text:
            raise RuntimeError(f"the architect did not produce a valid plan ({res.status})")
        return {"plan": text, "path": str(out), "tokens": res.input_tokens + res.output_tokens}

    @safe
    def plan_summary(self, text: str):
        """Parse and validate plan YAML for the Plan screen's task view. Errors come back as plain text."""
        from graphlib import TopologicalSorter

        import yaml

        from ..orchestrator import Plan

        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("the plan must be a YAML mapping")
        plan = Plan.model_validate(data)
        order = list(TopologicalSorter({t.id: set(t.depends_on) for t in plan.tasks}).static_order())
        tasks = sorted((t.model_dump() for t in plan.tasks), key=lambda t: order.index(t["id"]))
        return {"tasks": tasks, "contracts": [c.name for c in plan.contracts], "reviewer": plan.reviewer,
                "agents": sorted({t["agent"] for t in tasks})}

    @safe
    def save_plan(self, path: str, text: str):
        import yaml

        from ..orchestrator import Plan

        Plan.model_validate(yaml.safe_load(text))  # never save a plan that can't run
        Path(path).write_text(text, encoding="utf-8")
        return {"path": path}

    # ---------- approvals ----------
    @safe
    def approvals(self):
        m = self._need_mesh()
        return {"agents": _rows(m, "select id, name, harness, program_path, parent_process, heartbeat_at from agents "
                                   "where workspace_id=? and approval='pending' order by heartbeat_at", m.ws),
                "requests": _rows(m, "select * from approvals where state='pending' order by id")}

    @safe
    def decide(self, kind: str, id: str, approve: bool):
        m = self._need_mesh()
        if kind == "agent":
            m.decide_agent(id, bool(approve))
        elif kind == "request":
            m.decide_request(int(id), bool(approve))
        else:
            raise ValueError("kind must be agent or request")
        return {}

    # ---------- setup ----------
    @safe
    def doctor(self):
        return {"lines": S.doctor(self._need_mesh().root, Path.home())}

    @safe
    def setup_status(self):
        root = self._need_mesh().root
        tools = []
        for target, label, desc in SETUP_TARGETS:
            changes = S.plan(target, root, Path.home())
            tools.append({"target": target, "label": label, "desc": desc, "ready": not changes,
                          "files": [str(p) for p, _, _ in changes]})
        exe = S.axon_exe()
        return {"tools": tools, "launcher": exe, "launcher_ok": Path(exe).exists()}

    @safe
    def setup_preview(self, target: str):
        if target not in S.TARGETS:
            raise ValueError(f"target must be one of {S.TARGETS}")
        return {"diff": S.show(S.plan(target, self._need_mesh().root, Path.home())) or "already set up"}

    @safe
    def setup_apply(self, target: str):
        changes = S.plan(target, self._need_mesh().root, Path.home())
        S.apply(changes, Path.home() / ".axonmesh" / "setup-log.json")
        return {"files": [str(p) for p, _, _ in changes]}

    @safe
    def setup_undo(self):
        return {"files": S.undo(Path.home() / ".axonmesh" / "setup-log.json")}

    # ---------- API keys ----------
    @safe
    def keys(self):
        """Which keys are set. Values never leave Python."""
        return {"keys": [{"name": k, "set": bool(os.environ.get(k) or _user_env(k)), "provider": PROVIDER_INFO[k][0],
                          "url": PROVIDER_INFO[k][1], "note": PROVIDER_INFO[k][2]} for k in PROVIDER_KEYS]}

    @safe
    def open_key_page(self, name: str):
        """Open a provider's key page in the browser. Only the fixed URLs above, never one from the page."""
        import webbrowser

        if name not in PROVIDER_INFO:
            raise ValueError("unknown provider")
        webbrowser.open(PROVIDER_INFO[name][1])
        return {}

    @safe
    def set_key(self, name: str, value: str):
        if name not in PROVIDER_KEYS:
            raise ValueError("unknown key name")
        value = value.strip()
        _set_user_env(name, value or None)  # same place `setx` writes: per-user, survives restarts
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)
        return {}


def _user_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _set_user_env(name: str, value: str | None):
    if os.name != "nt":
        raise RuntimeError("saving keys is only supported on Windows; set the environment variable yourself")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        if value:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass
