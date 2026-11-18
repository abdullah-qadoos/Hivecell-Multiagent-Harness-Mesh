"""Tool router. Every rule in the agent's YAML is checked here, in code, before a tool runs."""
import fnmatch
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..rules import READ_ONLY_TOOLS, AgentRules

MAX_READ = 100_000
MAX_OUTPUT = 4_000

SPECS = {
    "read_file": {
        "name": "read_file",
        "description": "Read a text file. Path is relative to the workspace root.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    "write_file": {
        "name": "write_file",
        "description": "Create or overwrite a text file. Only paths you own are allowed.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    "list_files": {
        "name": "list_files",
        "description": "List the files you are allowed to read.",
        "parameters": {"type": "object", "properties": {}},
    },
    "run_tests": {
        "name": "run_tests",
        "description": "Run this agent's test command and return the exit code and output.",
        "parameters": {"type": "object", "properties": {}},
    },
    "run_command": {
        "name": "run_command",
        "description": "Run a shell command in a Docker container that only sees your workspace. "
                       "In ask mode, commands not pre-approved in your rules wait for the user.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
    "send_message": {
        "name": "send_message",
        "description": "Message another agent by name. Use urgent for things like contract changes.",
        "parameters": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}, "urgent": {"type": "boolean"}},
            "required": ["to", "body"],
        },
    },
    "publish_contract": {
        "name": "publish_contract",
        "description": "Publish an interface (API shape, types) that other agents build against.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "schema": {"type": "string"}},
            "required": ["name", "schema"],
        },
    },
    "read_contracts": {
        "name": "read_contracts",
        "description": "Read the latest version of every published contract, or one by name.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
    "report_done": {
        "name": "report_done",
        "description": "Call once the task is finished. The harness then runs the tests as a quality gate.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    },
}


class Rejected(Exception):
    """A tool call that broke the agent's rules. The model is told why."""


def safe_rel(path: str) -> bool:
    """Hidden paths (.git, .env, .axonmesh...) are never sent to a model provider by read-anything agents."""
    return not any(p.startswith(".") and p not in (".", "..") for p in path.replace("\\", "/").split("/"))


@dataclass
class Context:
    """Link to the coordination layer for one agent working on one task."""
    mesh: object  # axonmesh.Mesh
    aid: str
    run_id: str
    task_id: str
    names: dict[str, str]  # agent name in this run -> agent id
    notify: Callable[[str], None] = print


_docker: bool | None = None


def docker_available() -> bool:
    global _docker
    if _docker is None:
        try:
            _docker = subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _docker = False
    return _docker


class ToolRouter:
    def __init__(self, rules: AgentRules, root: Path, ctx: Context | None = None, mode: str = "auto"):
        self.rules = rules
        self.root = Path(root).resolve()
        self.ctx = ctx
        self._mode = mode
        self.done: str | None = None

    @property
    def mode(self) -> str:
        """Read live: the user can switch a running run between ask and auto."""
        if self.rules.mode:  # a per-agent override always wins
            return self.rules.mode
        if self.ctx:
            current = self.ctx.mesh.run_mode(self.ctx.run_id)
            if current:
                return current
        return self._mode

    def allowed(self) -> list[str]:
        tools = [t for t in self.rules.tools if self.mode != "plan" or t in READ_ONLY_TOOLS]
        return tools + ["report_done"]

    def specs(self) -> list[dict]:
        return [SPECS[n] for n in self.allowed()]

    def call(self, name: str, args: dict) -> str:
        if name not in self.allowed():
            why = " in plan mode" if name in self.rules.tools else ""
            raise Rejected(f"tool {name!r} is not allowed for agent {self.rules.name!r}{why}")
        return getattr(self, "_" + name)(**args)

    def _need_ctx(self) -> Context:
        if not self.ctx:
            raise Rejected("no coordination layer in this run")
        return self.ctx

    def _resolve(self, path: str) -> tuple[Path, str]:
        full = (self.root / path).resolve()  # resolve() also follows symlinks, so they can't escape
        if not full.is_relative_to(self.root):
            raise Rejected(f"{path!r} is outside the workspace")
        return full, full.relative_to(self.root).as_posix()

    def _read_file(self, path: str) -> str:
        full, rel = self._resolve(path)
        if not self.rules.may_read(rel):
            raise Rejected(f"{rel} is not readable; allowed: {self.rules.owns + self.rules.can_read}")
        return full.read_text(encoding="utf-8")[:MAX_READ]

    def _write_file(self, path: str, content: str) -> str:
        full, rel = self._resolve(path)
        if not self.rules.may_write(rel):
            raise Rejected(f"{rel} is outside your owned paths {self.rules.owns}")
        if self.ctx:  # another agent (e.g. a Claude Code session) may hold it, or have unmerged changes
            m = self.ctx.mesh
            clashes = [o for o in m.overlaps(self.ctx.aid, [m.norm(rel, self.ctx.aid)]) if not o.startswith("warning")]
            if clashes:
                raise Rejected("; ".join(clashes))
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return f"wrote {rel} ({len(content)} chars)"

    def _list_files(self) -> str:
        files = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in filenames:
                rel = Path(dirpath, f).relative_to(self.root).as_posix()
                if self.rules.may_read(rel):
                    files.append(rel)
        return "\n".join(sorted(files)[:500]) or "(no readable files yet)"

    def _run_tests(self) -> str:
        code, out = self.test()
        return f"exit {code}\n{out}"

    def _wait_for_approval(self, rid: int, timeout: float, poll: float = 0.5) -> str:
        m = self.ctx.mesh
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = m.store.one("select state from approvals where id=?", rid)["state"]
            if state != "pending":
                return state
            if self.mode == "auto":  # the user switched the run to auto: nothing waits any more
                try:
                    m.decide_request(rid, True, by="switched to auto")
                except Exception:
                    continue  # decided by the user at the same moment: read it again
                return "approved"
            time.sleep(poll)
        return "timeout"

    def _run_command(self, command: str) -> str:
        listed = any(fnmatch.fnmatchcase(command, pat) for pat in self.rules.commands)
        if self.mode == "ask" and not listed:
            ctx = self._need_ctx()
            rid = ctx.mesh.request_approval(ctx.aid, ctx.run_id, ctx.task_id, f"run `{command}`")
            ctx.notify(f"APPROVAL NEEDED #{rid}: {ctx.aid} wants to run `{command}`. "
                       f"Approve: axon approve request {rid}   Deny: axon deny request {rid}")
            state = self._wait_for_approval(rid, timeout=self.rules.budget.max_minutes * 60)
            if state != "approved":
                raise Rejected(f"`{command}` was not approved ({state})")
        # safety floor: agent commands only ever run in a container that sees just this worktree, no secrets
        if not docker_available():
            raise Rejected("run_command needs Docker, which isn't running")
        cmd = ["docker", "run", "--rm", "-v", f"{self.root}:/work", "-w", "/work", self.rules.image, "sh", "-c", command]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return "exit 124\ncommand timed out after 600s"
        return f"exit {r.returncode}\n{(r.stdout + r.stderr)[-MAX_OUTPUT:]}"

    def _send_message(self, to: str, body: str, urgent: bool = False) -> str:
        ctx = self._need_ctx()
        r = ctx.mesh.send(ctx.aid, ctx.names.get(to, to), body, ctx.task_id, urgent, run_id=ctx.run_id)
        return f"message #{r['id']}: {r['state']}"

    def _publish_contract(self, name: str, schema: str) -> str:
        ctx = self._need_ctx()
        return f"published {name} v{ctx.mesh.publish_contract(ctx.aid, name, schema, ctx.run_id)}"

    def _read_contracts(self, name: str | None = None) -> str:
        rows = self._need_ctx().mesh.contracts(name)
        return "\n\n".join(f"## {r['name']} v{r['version']}\n{r['schema']}" for r in rows) or "no contracts yet"

    def _report_done(self, summary: str) -> str:
        self.done = summary
        return "recorded"

    def test(self) -> tuple[int, str]:
        """Run the agent's test_cmd. The command comes from the rules file, never from the model."""
        if not self.rules.test_cmd:
            return 0, "this agent has no done check"
        cmd = shlex.split(self.rules.test_cmd)
        if cmd[0] == "python" and not getattr(sys, "frozen", False):
            # from source: the harness's own interpreter, which has pytest. In the packaged app sys.executable
            # is Hivecell.exe, so "python" is left to the Python installed on the user's PATH.
            cmd[0] = sys.executable
        try:
            r = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return 124, "tests timed out after 300s"
        except FileNotFoundError:
            return 127, f"{cmd[0]!r} was not found. Install it, or change this agent's done check."
        return r.returncode, (r.stdout + r.stderr)[-MAX_OUTPUT:]
