"""Axonmesh core: the shared work-state every agent goes through.

Plain Python, no MCP here, so the harness can call it in-process and the MCP server is a thin wrapper.
"""
import json
import os
import time
import uuid
from pathlib import Path

from . import paths as P
from .store import Store

TTL = 600  # seconds without activity before an agent counts as offline and its hard claims expire
TASK_KINDS = {"feature", "fix", "refactor", "test", "docs"}
FINISH_STATES = {"review_ready", "complete", "closed"}
NOTE_KINDS = {"decision", "assumption", "finding", "proposal", "undelivered"}


class MeshError(Exception):
    """A request Axonmesh refuses. The message is shown to the agent."""


def _line(s) -> str:
    """One escaped line: newlines can't forge extra items in a review packet (fix #9)."""
    return str(s).replace("\r", " ").replace("\n", " ⏎ ")[:500]


def wrap_message(m) -> str:
    """Peer content is clearly marked as untrusted, and can't fake the markers (fix #7)."""
    body = m["body"].replace("<<", "‹‹").replace(">>", "››")
    head = f"<<peer message #{m['id']} from {m['from_agent']}{' URGENT' if m['urgent'] else ''} (untrusted peer content)>>"
    return f"{head}\n{body}\n<<end peer message #{m['id']}>>"


class Mesh:
    def __init__(self, repo: str | Path, db_path: str | Path | None = None,
                 allowlist_path: str | Path | None = None, ttl: float = TTL):
        self.root = P.main_checkout(Path(repo))
        self.ttl = ttl
        self.fold = P.ignore_case(self.root)
        _exclude(self.root, ".axonmesh/")
        self.store = Store(db_path or self.root / ".axonmesh" / "axon.db")
        home = Path(os.environ.get("AXONMESH_HOME", Path.home() / ".axonmesh"))
        self.allowlist_path = Path(allowlist_path or home / "allowlist.json")
        with self.store.tx() as db:
            db.execute("insert or ignore into workspaces (repo_root, ignore_case) values (?,?)",
                       (str(self.root), int(self.fold)))
            db.execute("update workspaces set ignore_case=? where repo_root=?", (int(self.fold), str(self.root)))
        self.ws = self.store.one("select id from workspaces where repo_root=?", str(self.root))["id"]

    def close(self):
        self.store.db.close()

    # ---------- events ----------
    def log(self, aid, type_, payload=None, run_id=None):
        self.store.event(self.ws, run_id, aid, type_, payload or {})

    def delta(self, aid, limit=20) -> list[str]:
        """Events from others since this agent's last call: compact replies instead of full state (fix #12)."""
        a = self.agent(aid)
        rows = self.store.q("select * from events where workspace_id=? and id>? and coalesce(agent_id,'')!=? "
                            "order by id limit ?", self.ws, a["last_seen_event_id"], aid, limit)
        last = self.store.one("select max(id) m from events where workspace_id=?", self.ws)["m"] or 0
        with self.store.tx() as db:
            db.execute("update agents set last_seen_event_id=? where id=?", (last, aid))
        return [f"#{r['id']} {r['agent_id']} {r['type']} {_line(r['payload'])[:160]}" for r in rows]

    # ---------- runs and ledger ----------
    def create_run(self, spec: str, mode: str) -> str:
        if mode not in ("plan", "auto", "ask"):
            raise MeshError(f"mode must be plan, auto or ask, not {mode!r}")
        rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        with self.store.tx() as db:
            db.execute("insert into runs (id, workspace_id, spec, mode, status, started_at) values (?,?,?,?,?,?)",
                       (rid, self.ws, spec, mode, "running", time.time()))
        self.log(None, "run_started", {"mode": mode}, rid)
        return rid

    def run_mode(self, rid) -> str | None:
        row = self.store.one("select mode from runs where id=?", rid) if rid else None
        return row["mode"] if row else None

    def set_run_mode(self, rid, mode: str):
        """Switch a running run between ask and auto. Agents pick it up on their next command."""
        if mode not in ("ask", "auto"):
            raise MeshError("a running run can switch between ask and auto")
        with self.store.tx() as db:
            row = db.execute("select mode, status from runs where id=?", (rid,)).fetchone()
            if not row or row["status"] != "running":
                raise MeshError(f"run {rid} is not running")
            if row["mode"] == "plan":
                raise MeshError("a plan-only run can't switch modes")
            db.execute("update runs set mode=? where id=?", (mode, rid))
        self.log(None, "mode_changed", {"from": row["mode"], "to": mode}, rid)

    def finish_run(self, rid, status, tokens, cost):
        with self.store.tx() as db:
            db.execute("update runs set status=?, finished_at=?, total_tokens=?, cost=? where id=?",
                       (status, time.time(), tokens, cost, rid))
        self.log(None, "run_finished", {"status": status}, rid)

    def ledger(self, rid, tid, aid, model, input_tokens, output_tokens, cost):
        with self.store.tx() as db:
            db.execute("insert into token_ledger (run_id, task_id, agent_id, model, input, output, cost) "
                       "values (?,?,?,?,?,?,?)", (rid, tid, aid, model, input_tokens, output_tokens, cost))

    # ---------- agents, presence, approval ----------
    def register(self, name, origin, harness="", program_path="", parent_process="", run_id=None,
                 model="", rules_yaml="", commands=(), mode_override=None, worktree=None, client_pid=None) -> str:
        if origin not in ("internal", "external"):
            raise MeshError("origin must be internal or external")
        aid = f"{run_id}/{name}" if origin == "internal" and run_id else f"{name}-{uuid.uuid4().hex[:6]}"
        # internal agents were created by the harness from an approved plan; external ones need the user (decision 6)
        approved = origin == "internal" or self._allowed(program_path)
        now = time.time()
        with self.store.tx() as db:
            db.execute(
                "insert into agents (id, workspace_id, run_id, name, origin, harness, model, rules_yaml, commands, "
                "mode_override, approval, program_path, parent_process, worktree, heartbeat_at, last_activity_at, "
                "reported_status, client_pid, last_seen_event_id) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "(select coalesce(max(id),0) from events))",
                (aid, self.ws, run_id, name, origin, harness, model, rules_yaml, json.dumps(list(commands)),
                 mode_override, "approved" if approved else "pending", program_path, parent_process,
                 str(Path(worktree).resolve()) if worktree else str(self.root), now, now, "idle", client_pid))
        self.log(aid, "agent_joined", {"origin": origin, "harness": harness, "program": program_path,
                                       "approval": "approved" if approved else "pending"}, run_id)
        return aid

    def agent(self, aid):
        a = self.store.one("select * from agents where id=?", aid)
        if not a:
            raise MeshError(f"unknown agent {aid!r}")
        return a

    def agent_for_pids(self, pids: list[int]):
        """The online external agent whose MCP client is one of these processes (used by the Claude Code hook)."""
        if not pids:
            return None
        marks = ",".join("?" * len(pids))
        rows = self.store.q(f"select * from agents where workspace_id=? and origin='external' and client_pid in ({marks}) "
                            "order by heartbeat_at desc", self.ws, *pids)
        return next((a for a in rows if a["reported_status"] != "offline"), None)

    def require(self, aid, status=None):
        """Every agent action passes here: approval check plus presence refresh."""
        a = self.agent(aid)
        if a["approval"] != "approved":
            raise MeshError(f"agent {aid} is {a['approval']}: the user must run `axon approve agent {aid}`")
        self.touch(aid, status)
        return a

    def touch(self, aid, status=None):
        """Any write refreshes presence and keeps hard claims alive."""
        now = time.time()
        with self.store.tx() as db:
            db.execute("update agents set heartbeat_at=?, last_activity_at=?, reported_status=coalesce(?, "
                       "reported_status) where id=?", (now, now, status, aid))
            db.execute("update claims set expires_at=? where agent_id=? and lock_type='hard' and released=0",
                       (now + self.ttl, aid))

    def online(self, a) -> bool:
        return a["reported_status"] not in ("offline", "done") and a["heartbeat_at"] > time.time() - self.ttl

    def go_offline(self, aid):
        with self.store.tx() as db:
            db.execute("update agents set reported_status='offline' where id=?", (aid,))
        self.log(aid, "agent_left")

    def _allowed(self, program_path) -> bool:
        if not program_path or not self.allowlist_path.exists():
            return False
        allowed = json.loads(self.allowlist_path.read_text(encoding="utf-8"))
        return os.path.normcase(program_path) in {os.path.normcase(p) for p in allowed}

    def decide_agent(self, aid, approve: bool):
        a = self.agent(aid)
        with self.store.tx() as db:
            db.execute("update agents set approval=? where id=?", ("approved" if approve else "denied", aid))
        if approve and a["program_path"]:
            self.allowlist_path.parent.mkdir(parents=True, exist_ok=True)
            allowed = json.loads(self.allowlist_path.read_text(encoding="utf-8")) if self.allowlist_path.exists() else []
            if a["program_path"] not in allowed:
                self.allowlist_path.write_text(json.dumps(allowed + [a["program_path"]], indent=2), encoding="utf-8")
        self.log(None, "agent_approved" if approve else "agent_denied", {"agent": aid})

    # ---------- tasks ----------
    def create_task(self, aid, title, kind="feature", run_id=None, owner=None, depends_on=(), task_id=None,
                    status=None) -> str:
        if kind not in TASK_KINDS:
            raise MeshError(f"kind must be one of {sorted(TASK_KINDS)} (agent identity is a separate field)")
        tid = task_id or "t-" + uuid.uuid4().hex[:6]
        with self.store.tx() as db:
            db.execute("insert into tasks (id, workspace_id, run_id, kind, title, owner_agent_id, status, version, "
                       "depends_on) values (?,?,?,?,?,?,?,1,?)",
                       (tid, self.ws, run_id, kind, title, owner, status or ("in_progress" if owner else "open"),
                        json.dumps(list(depends_on))))
        self.log(aid, "task_created", {"task": tid, "title": title, "owner": owner}, run_id)
        return tid

    def task(self, tid):
        t = self.store.one("select * from tasks where id=?", tid)
        if not t:
            raise MeshError(f"unknown task {tid!r}")
        return t

    def _bump(self, db, tid, expected_version, **fields):
        """Versioned update: if someone else changed the task first, this one fails (fix: no double-taking)."""
        sets = "".join(f"{k}=?, " for k in fields)
        cur = db.execute(f"update tasks set {sets}version=version+1 where id=? and version=?",
                         (*fields.values(), tid, expected_version))
        if cur.rowcount == 0:
            now = db.execute("select version from tasks where id=?", (tid,)).fetchone()
            raise MeshError(f"version conflict on {tid}: it is at version {now[0] if now else '?'}, "
                            f"you sent {expected_version}. Re-read it and retry.")

    def set_status(self, tid, status, attempts=None):
        """Harness-only: the orchestrator is the authority for internal tasks."""
        with self.store.tx() as db:
            v = db.execute("select version from tasks where id=?", (tid,)).fetchone()[0]
            fields = {"status": status} | ({"attempts": attempts} if attempts is not None else {})
            self._bump(db, tid, v, **fields)
        self.log(None, "task_status", {"task": tid, "status": status})

    def transfer(self, aid, tid, action, expected_version, to=None) -> str:
        self.require(aid)
        t = self.task(tid)
        owner = t["owner_agent_id"]
        with self.store.tx() as db:
            if action == "take":
                if t["status"] != "open":
                    raise MeshError(f"{tid} is {t['status']}, only open tasks can be taken")
                self._bump(db, tid, expected_version, owner_agent_id=aid, status="in_progress")
            elif action in ("assign", "offer"):
                if not to:
                    raise MeshError(f"{action} needs `to`")
                self.agent(to)
                if action == "assign" and not (t["status"] == "open" or owner == aid):
                    raise MeshError("only open tasks or your own can be assigned")
                if action == "offer" and owner != aid:
                    raise MeshError("only the owner can offer a handoff")
                self._bump(db, tid, expected_version, status="assigned" if action == "assign" else t["status"])
                db.execute("insert into handoffs (task_id, from_agent, to_agent, state, created_at) "
                           "values (?,?,?,?,?)", (tid, aid, to, "offered", time.time()))
            elif action in ("accept", "decline"):
                h = db.execute("select * from handoffs where task_id=? and to_agent=? and state='offered' "
                               "order by id desc", (tid, aid)).fetchone()
                if not h:
                    raise MeshError(f"no handoff of {tid} is waiting for you")
                db.execute("update handoffs set state=? where id=?", (action + "ed", h["id"]))
                if action == "accept":
                    self._bump(db, tid, expected_version, owner_agent_id=aid, status="in_progress")
                    db.execute("update claims set agent_id=? where task_id=? and released=0", (aid, tid))
                else:
                    self._bump(db, tid, expected_version, status="open" if t["status"] == "assigned" else t["status"])
            elif action == "release":
                if owner != aid:
                    raise MeshError("only the owner can release a task")
                self._bump(db, tid, expected_version, owner_agent_id=None, status="open")
                db.execute("update claims set released=1 where task_id=?", (tid,))
            elif action == "reassign":
                if owner != aid or not to:
                    raise MeshError("only the owner can reassign, and it needs `to`")
                if self.agent(to)["approval"] != "approved":
                    raise MeshError(f"{to} is not approved")
                self._bump(db, tid, expected_version, owner_agent_id=to)
                db.execute("update claims set agent_id=? where task_id=? and released=0", (to, tid))
            elif action == "reopen":
                if t["status"] not in FINISH_STATES or owner not in (None, aid):
                    raise MeshError("only your own finished tasks can be reopened")
                self._bump(db, tid, expected_version, status="in_progress")
                db.execute("update claims set lock_type='hard', expires_at=? where task_id=? and released=0",
                           (time.time() + self.ttl, tid))
            else:
                raise MeshError("action must be take, assign, accept, decline, release, reassign, offer or reopen")
        self.log(aid, "task_" + action, {"task": tid, "to": to}, t["run_id"])
        return f"{tid}: {action} ok (version {expected_version + 1})"

    def finish(self, aid, tid, expected_version, state, evidence=()) -> str:
        self.require(aid)
        t = self.task(tid)
        if state not in FINISH_STATES:
            raise MeshError(f"state must be one of {sorted(FINISH_STATES)}")
        if t["owner_agent_id"] != aid:
            raise MeshError("only the owner can finish a task")
        with self.store.tx() as db:
            self._bump(db, tid, expected_version, status=state)
            for item in evidence:
                db.execute("insert into evidence (task_id, item, source, created_at) values (?,?,?,?)",
                           (tid, _line(item), "agent_reported", time.time()))
            if state == "review_ready":  # unmerged work stays protected (fix #3)
                db.execute("update claims set lock_type='soft', expires_at=null where task_id=? and released=0", (tid,))
            else:
                db.execute("update claims set released=1 where task_id=?", (tid,))
        self.log(aid, "task_finished", {"task": tid, "state": state}, t["run_id"])
        if state == "review_ready":
            return f"{tid}: review_ready, review packet at {self.review_packet(tid)}"
        return f"{tid}: {state}"

    def add_evidence(self, tid, item, source):
        with self.store.tx() as db:
            db.execute("insert into evidence (task_id, item, source, created_at) values (?,?,?,?)",
                       (tid, _line(item), source, time.time()))

    def note(self, aid, tid, kind, body):
        if kind not in NOTE_KINDS:
            raise MeshError(f"note kind must be one of {sorted(NOTE_KINDS)}")
        self.task(tid)
        with self.store.tx() as db:
            db.execute("insert into task_notes (task_id, agent_id, kind, body, created_at) values (?,?,?,?,?)",
                       (tid, aid, kind, body, time.time()))
        self.log(aid, "task_note", {"task": tid, "kind": kind})

    # ---------- claims and overlap ----------
    def norm(self, path, aid=None) -> str:
        base = Path(self.agent(aid)["worktree"]) if aid else self.root
        try:
            return P.normalize(path, self.root, base, self.fold)
        except ValueError as e:
            raise MeshError(str(e))

    def active_claims(self):
        return self.store.q("select * from claims where workspace_id=? and released=0 and "
                            "(lock_type='soft' or expires_at>?)", self.ws, time.time())

    def overlaps(self, aid, norm_paths) -> list[str]:
        """Claims by others, plus uncommitted changes in other worktrees (fix #3)."""
        found = []
        for c in self.active_claims():
            if c["agent_id"] == aid:
                continue
            for p in norm_paths:
                if P.overlaps(p, c["path_norm"]):
                    found.append(f"{p} overlaps {c['path_norm']} ({c['lock_type']} claim by {c['agent_id']})")
                elif p.casefold() == c["path_norm"].casefold():
                    found.append(f"warning: {p} differs only by case from {c['path_norm']} claimed by {c['agent_id']}")
        own = Path(self.agent(aid)["worktree"]).resolve() if aid else None
        for wt in P.worktrees(self.root):
            if own and wt.resolve() == own:
                continue
            try:
                dirty = P.dirty_files(wt)
            except RuntimeError:
                continue  # worktree folder removed
            for f in dirty:
                f = f.casefold() if self.fold else f
                for p in norm_paths:
                    if P.overlaps(p, f):
                        found.append(f"{p} has uncommitted changes in worktree {wt}")
        return found

    def claim(self, aid, paths, task_id=None, lock="hard", run_id=None) -> list[str]:
        self.require(aid)
        norm = [self.norm(p, aid) for p in paths]
        found = self.overlaps(aid, norm)
        mine = {c["path_norm"] for c in self.active_claims() if c["agent_id"] == aid}
        with self.store.tx() as db:
            for p in norm:
                if p not in mine:
                    db.execute("insert into claims (workspace_id, run_id, task_id, agent_id, path_norm, lock_type, "
                               "expires_at) values (?,?,?,?,?,?,?)",
                               (self.ws, run_id, task_id, aid, p, lock, time.time() + self.ttl))
        self.log(aid, "claim", {"paths": norm, "task": task_id, "overlaps": found}, run_id)
        return found

    def release_claims(self, aid=None, run_id=None):
        with self.store.tx() as db:
            if run_id:
                db.execute("update claims set released=1 where run_id=?", (run_id,))
            else:
                db.execute("update claims set released=1 where agent_id=?", (aid,))

    def soften_claims(self, run_id):
        with self.store.tx() as db:
            db.execute("update claims set lock_type='soft', expires_at=null where run_id=? and released=0", (run_id,))

    # ---------- messages ----------
    def send(self, aid, to, body, task_id=None, urgent=False, idempotency_key=None, reply_to=None, run_id=None) -> dict:
        self.require(aid)
        rcpt = self.agent(to)
        if idempotency_key:
            old = self.store.one("select id, state from messages where from_agent=? and idempotency_key=?",
                                 aid, idempotency_key)
            if old:  # a retried send never delivers twice
                return {"id": old["id"], "state": old["state"], "duplicate": True}
        state = "queued" if self.online(rcpt) else "recipient_offline"
        with self.store.tx() as db:
            cur = db.execute("insert into messages (workspace_id, run_id, task_id, from_agent, to_agent, body, urgent, "
                             "idempotency_key, reply_to, state, created_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                             (self.ws, run_id, task_id, aid, to, body, int(urgent), idempotency_key, reply_to, state,
                              time.time()))
            mid = cur.lastrowid
        if state == "recipient_offline" and task_id:  # whoever picks up the task next sees it (fix #4)
            self.note(aid, task_id, "undelivered", f"message #{mid} to {to}: {body}")
        self.log(aid, "message", {"id": mid, "to": to, "state": state, "urgent": urgent}, run_id)
        return {"id": mid, "state": state}

    def inbox(self, aid) -> list:
        rows = self.store.q("select * from messages where to_agent=? and state in ('queued','recipient_offline') "
                            "order by urgent desc, id", aid)
        if rows:
            with self.store.tx() as db:
                db.executemany("update messages set state='delivered' where id=?", [(r["id"],) for r in rows])
            self.log(aid, "delivered", {"ids": [r["id"] for r in rows]})
        return rows

    def ack(self, aid, ids):
        """Read receipts only from an explicit ack, never from scanning text (fix #8)."""
        with self.store.tx() as db:
            db.executemany("update messages set state='read' where id=? and to_agent=?", [(i, aid) for i in ids])
        self.log(aid, "read", {"ids": list(ids)})

    def unread(self, aid) -> tuple[int, int]:
        r = self.store.one("select count(*) n, coalesce(sum(urgent),0) u from messages where to_agent=? and "
                           "state in ('queued','recipient_offline')", aid)
        return r["n"], r["u"]

    def status_line(self, aid) -> str:
        n, u = self.unread(aid)
        return f"[{aid}] {n} unread" + (f" ({u} urgent)" if u else "")

    # ---------- contracts ----------
    def publish_contract(self, aid, name, schema, run_id=None) -> int:
        self.require(aid)
        v = (self.store.one("select max(version) v from contracts where workspace_id=? and name=?", self.ws, name)["v"]
             or 0) + 1
        with self.store.tx() as db:
            db.execute("insert into contracts (workspace_id, run_id, name, version, schema, published_by, created_at) "
                       "values (?,?,?,?,?,?,?)", (self.ws, run_id, name, v, schema, aid, time.time()))
        self.log(aid, "contract", {"name": name, "version": v}, run_id)
        return v

    def contracts(self, name=None):
        rows = self.store.q("select * from contracts c where workspace_id=? and version=(select max(version) from "
                            "contracts where workspace_id=c.workspace_id and name=c.name) order by name", self.ws)
        return [r for r in rows if name is None or r["name"] == name]

    # ---------- approvals (ask mode) ----------
    def request_approval(self, aid, run_id, task_id, request) -> int:
        with self.store.tx() as db:
            rid = db.execute("insert into approvals (run_id, agent_id, task_id, request, state, created_at) "
                             "values (?,?,?,?,?,?)", (run_id, aid, task_id, request, "pending", time.time())).lastrowid
        self.log(aid, "approval_requested", {"id": rid, "request": request}, run_id)
        return rid

    def decide_request(self, rid, approve: bool, by: str = "user"):
        with self.store.tx() as db:
            cur = db.execute("update approvals set state=?, decided_at=? where id=? and state='pending'",
                             ("approved" if approve else "denied", time.time(), rid))
            if cur.rowcount == 0:
                raise MeshError(f"no pending request #{rid}")
        row = self.store.one("select run_id, agent_id, request from approvals where id=?", rid)
        self.log(None, "approval_decided", {"id": rid, "approved": approve, "agent": row["agent_id"],
                                            "request": row["request"], "by": by}, row["run_id"])

    def wait_for(self, rid, timeout: float, poll: float = 1.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.store.one("select state from approvals where id=?", rid)["state"]
            if state != "pending":
                return state
            time.sleep(poll)
        return "timeout"

    # ---------- review packets and exports (decision 10) ----------
    def review_packet(self, tid) -> Path:
        t = self.task(tid)
        claims = self.store.q("select distinct path_norm from claims where task_id=?", tid)
        ev = self.store.q("select * from evidence where task_id=? order by id", tid)
        notes = self.store.q("select * from task_notes where task_id=? order by id", tid)
        lines = [f"# Review packet: {tid}", "",
                 f"> Generated from axon.db at {time.strftime('%Y-%m-%d %H:%M:%S')}. Do not edit: Axonmesh never reads this file.",
                 "", f"- Title: {_line(t['title'])}", f"- Kind: {t['kind']}", f"- Owner: {t['owner_agent_id']}",
                 f"- Status: {t['status']} (version {t['version']})", "", "## Scope (claimed paths)"]
        lines += [f"- {_line(c['path_norm'])}" for c in claims] or ["- none"]
        lines += ["", "## Evidence"]
        label = {"ci": "CI", "reviewer": "reviewer"}
        lines += [f"- [{label.get(e['source'], 'agent-reported')}] {e['item']}" for e in ev] or ["- none"]
        lines += ["", "## Notes, risks, decisions"]
        lines += [f"- [{n['kind']}] {_line(n['body'])}" for n in notes] or ["- none"]
        out = self.root / ".axonmesh" / "review" / f"{tid}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out

    def export(self, kind: str, out_dir: Path | None = None) -> Path:
        out_dir = Path(out_dir or self.root / ".axonmesh")
        out_dir.mkdir(parents=True, exist_ok=True)
        if kind == "events":
            out = out_dir / "events.jsonl"
            rows = self.store.q("select * from events where workspace_id=? order by id", self.ws)
            out.write_text("".join(json.dumps(dict(r)) + "\n" for r in rows), encoding="utf-8")
        elif kind == "state":
            out = out_dir / "WORK_STATE.json"
            state = {k: [dict(r) for r in self.store.q(f"select * from {k} where workspace_id=?", self.ws)]
                     for k in ("agents", "tasks", "claims", "contracts")}
            out.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        elif kind == "handoff":
            out = out_dir / "HANDOFF.md"
            lines = ["# Handoff", "", "> Generated from axon.db. Do not edit.", ""]
            for t in self.store.q("select * from tasks where workspace_id=? and status not in ('complete','closed')",
                                  self.ws):
                lines.append(f"## {t['id']}: {_line(t['title'])}")
                lines.append(f"- status {t['status']}, owner {t['owner_agent_id']}, version {t['version']}")
                for n in self.store.q("select * from task_notes where task_id=? order by id", t["id"]):
                    lines.append(f"- [{n['kind']}] {_line(n['body'])}")
                lines.append("")
            out.write_text("\n".join(lines), encoding="utf-8")
        else:
            raise MeshError("export kind must be events, state or handoff")
        return out


def _exclude(repo: Path, pattern: str):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if not exclude.exists() or pattern not in exclude.read_text(encoding="utf-8"):
        with exclude.open("a", encoding="utf-8") as f:
            f.write(f"\n{pattern}\n")
