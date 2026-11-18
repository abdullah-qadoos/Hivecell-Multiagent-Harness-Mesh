"""MCP server over stdio. One process per connected agent, so the process *is* the identity (decision 6)."""
import functools
import os
from pathlib import Path

from .mesh import Mesh, MeshError, wrap_message

# Only pure launchers are skipped. Never interpreters or shells: a python script acting as an MCP client
# must show up as python, not inherit the approval of whatever program it was started from.
LAUNCHERS = {"uv", "uvx", "axon"}


def client_process():
    """The MCP client process that started this server, skipping pure launchers."""
    import psutil

    p = psutil.Process(os.getpid()).parent()
    while p and Path(p.name()).stem.lower() in LAUNCHERS:
        p = p.parent()
    return p


def client_program() -> tuple[str, str, int | None]:
    """(program path, full command line, pid) of the MCP client."""
    import psutil

    p = client_process()
    if not p:
        return "", "", None
    try:
        return p.exe(), " ".join(p.cmdline())[:500], p.pid
    except psutil.Error:
        return p.name(), "", p.pid


class Session:
    """The tools one connected agent can call. No tool takes an agent id: it comes from the connection."""

    def __init__(self, mesh: Mesh, cwd: Path, program: str = "", parent: str = "", pid: int | None = None):
        self.mesh, self.cwd, self.program, self.parent, self.pid = mesh, Path(cwd), program, parent, pid
        self.aid: str | None = None

    def _me(self) -> str:
        if not self.aid:
            raise MeshError("call start_work first")
        return self.aid

    def _reply(self, lines) -> str:
        """Compact by default: result, unread count and what changed since your last call (fix #12)."""
        aid = self._me()
        out = [l for l in lines if l]
        a = self.mesh.agent(aid)
        if a["approval"] == "approved":
            changes = self.mesh.delta(aid)
            out.append(self.mesh.status_line(aid) + (f"; {len(changes)} changes since your last call" if changes else ""))
            out += [f"  {c}" for c in changes]
        return "\n".join(out)

    def start_work(self, name: str, harness: str = "", task_id: str | None = None,
                   expected_version: int | None = None, new_task: str | None = None, kind: str = "feature",
                   paths: list[str] | None = None, status: str = "working") -> str:
        out = []
        if not self.aid:
            self.aid = self.mesh.register(name, "external", harness, self.program, self.parent, worktree=self.cwd,
                                          client_pid=self.pid)
            out.append(f"registered as {self.aid}")
        a = self.mesh.agent(self.aid)
        if a["approval"] != "approved":
            return (f"registered as {self.aid}, waiting for approval. Ask the user to run "
                    f"`axon approve agent {self.aid}`, then call start_work again.")
        self.mesh.require(self.aid, status)
        if new_task:
            task_id = self.mesh.create_task(self.aid, new_task, kind, owner=self.aid)
            out.append(f"created task {task_id} (version 1), owned by you")
        elif task_id:
            if expected_version is None:
                raise MeshError("taking a task needs expected_version; read it with inspect_work")
            t = self.mesh.task(task_id)
            action = "accept" if t["status"] == "assigned" else "take"
            out.append(self.mesh.transfer(self.aid, task_id, action, expected_version))
        if paths:
            found = self.mesh.claim(self.aid, paths, task_id)
            out.append(f"claimed {len(paths)} path(s)")
            out += ["OVERLAP " + f for f in found] or ["no overlaps"]
        return self._reply(out)

    def inspect_work(self, what: str = "workspace", id: str | None = None, query: str | None = None,
                     full: bool = False) -> str:
        aid = self._me()
        m = self.mesh
        m.require(aid)
        if what == "workspace":
            tasks = m.store.q("select * from tasks where workspace_id=? and status not in ('complete','closed') "
                              "order by id", m.ws)
            agents = m.store.q("select * from agents where workspace_id=? and approval='approved'", m.ws)
            out = ["tasks:"] + [f"  {t['id']} v{t['version']} {t['status']} owner={t['owner_agent_id']} "
                                f"kind={t['kind']}: {t['title']}" for t in tasks]
            out += ["agents online:"] + [f"  {a['id']} ({a['reported_status']})" for a in agents if m.online(a)]
            out += ["active claims:"] + [f"  {c['path_norm']} {c['lock_type']} by {c['agent_id']}"
                                         for c in m.active_claims()]
            if not full:
                out = out[:60]
        elif what == "task":
            t = m.task(id)
            out = [f"{t['id']} v{t['version']} {t['status']} owner={t['owner_agent_id']} kind={t['kind']}",
                   f"title: {t['title']}"]
            out += [f"  [{n['kind']}] {n['body']}" for n in
                    m.store.q("select * from task_notes where task_id=? order by id", id)]
        elif what == "inbox":
            msgs = m.inbox(aid)
            out = [wrap_message(r) for r in msgs] or ["inbox empty"]
            if msgs:
                out.append(f"call update_work(ack=[...]) with the ids you have read: {[r['id'] for r in msgs]}")
        elif what == "outbox":
            out = [f"#{r['id']} to {r['to_agent']}: {r['state']}" for r in
                   m.store.q("select * from messages where from_agent=? order by id desc limit 20", aid)] or ["none sent"]
        elif what == "thread":
            root = int(id)
            rows = m.store.q("select * from messages where id=? or reply_to=? order by id", root, root)
            out = [wrap_message(r) for r in rows if aid in (r["to_agent"], r["from_agent"])] or ["no such thread"]
        elif what == "search":
            like = f"%{query or ''}%"
            out = [f"{t['id']} {t['status']}: {t['title']}" for t in
                   m.store.q("select * from tasks where workspace_id=? and title like ?", m.ws, like)] or ["no match"]
        else:
            raise MeshError("what must be workspace, task, inbox, outbox, thread or search")
        return self._reply(out)

    def update_work(self, task_id: str | None = None, note: str | None = None, note_kind: str = "finding",
                    to: str | None = None, message: str | None = None, urgent: bool = False,
                    reply_to: int | None = None, idempotency_key: str | None = None, status: str | None = None,
                    ack: list[int] | None = None, paths: list[str] | None = None) -> str:
        aid = self._me()
        self.mesh.require(aid, status)
        out = []
        if note:
            if not task_id:
                raise MeshError("a note needs task_id")
            self.mesh.note(aid, task_id, note_kind, note)
            out.append(f"noted on {task_id}")
        if message:
            if not to:
                raise MeshError("a message needs `to`")
            r = self.mesh.send(aid, to, message, task_id, urgent, idempotency_key, reply_to)
            out.append(f"message #{r['id']}: {r['state']}" + (" (duplicate, not resent)" if r.get("duplicate") else ""))
        if ack:
            self.mesh.ack(aid, ack)
            out.append(f"marked {len(ack)} read")
        if paths:
            found = self.mesh.claim(aid, paths, task_id)
            out += ["OVERLAP " + f for f in found] or [f"claimed {len(paths)} path(s), no overlaps"]
        return self._reply(out or ["ok"])

    def transfer_work(self, task_id: str, action: str, expected_version: int, to: str | None = None) -> str:
        return self._reply([self.mesh.transfer(self._me(), task_id, action, expected_version, to)])

    def finish_work(self, task_id: str, expected_version: int, state: str = "review_ready",
                    evidence: list[str] | None = None) -> str:
        return self._reply([self.mesh.finish(self._me(), task_id, expected_version, state, evidence or [])])

    def publish_contract(self, name: str, schema: str) -> str:
        v = self.mesh.publish_contract(self._me(), name, schema)
        return self._reply([f"published {name} v{v}"])

    def read_contracts(self, name: str | None = None) -> str:
        self.mesh.require(self._me())
        rows = self.mesh.contracts(name)
        return self._reply([f"## {r['name']} v{r['version']} (by {r['published_by']})\n{r['schema']}" for r in rows]
                           or ["no contracts"])

    def close(self):
        if self.aid:
            self.mesh.go_offline(self.aid)


TOOL_DOCS = {
    "start_work": "Register yourself (first call), then optionally take/accept a task (task_id + expected_version) "
                  "or create one (new_task), and claim paths before editing. Reports overlaps.",
    "inspect_work": "Read state. what = workspace | task (id) | inbox | outbox | thread (id = message id) | search (query).",
    "update_work": "Record a note on a task, message another agent (to + message), ack read messages, "
                   "set your status (working/blocked/idle), or claim more paths.",
    "transfer_work": "action = take | assign | accept | decline | release | reassign | offer | reopen. Needs expected_version.",
    "finish_work": "state = review_ready | complete | closed, with evidence lines. review_ready keeps your claims as soft locks.",
    "publish_contract": "Share an interface (API shape, types) other agents build against.",
    "read_contracts": "Read the latest version of every contract, or one by name.",
}


def build_server(session: Session):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("axonmesh", instructions="Coordinate with other agents in this repo. Call start_work first.")

    def safe(fn):
        @functools.wraps(fn)  # keeps the signature and type hints the SDK builds the tool schema from
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except MeshError as e:
                return f"REFUSED: {e}"
        return wrapper

    for name in TOOL_DOCS:
        server.tool(name=name, description=TOOL_DOCS[name])(safe(getattr(session, name)))
    return server


def serve(repo: str | Path = "."):
    cwd = Path(repo).resolve()
    program, cmdline, pid = client_program()
    session = Session(Mesh(cwd), cwd, program, cmdline, pid)
    try:
        build_server(session).run("stdio")
    finally:
        session.close()
