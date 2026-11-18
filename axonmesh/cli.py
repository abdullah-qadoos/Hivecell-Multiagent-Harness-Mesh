"""`axon`: the MCP server plus the human side: viewers, approvals, exports."""
import argparse
import json
import sys
import time
from datetime import datetime

from .mesh import Mesh, MeshError

RED, DIM, BOLD, END = "\033[31m", "\033[2m", "\033[1m", "\033[0m"
ALERT_TYPES = {"rejected", "agent_denied", "error"}


def _ts(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _event_line(r) -> str:
    p = json.loads(r["payload"] or "{}")
    bad = r["type"] in ALERT_TYPES or (r["type"] == "claim" and p.get("overlaps"))
    text = f"{_ts(r['created_at'])} #{r['id']:<5} {str(r['agent_id'] or '-'):<28} {r['type']:<18} " \
           f"{json.dumps(p, ensure_ascii=False)[:150]}"
    return f"{RED}{text}{END}" if bad else text


def cmd_events(m: Mesh, a):
    sql = "select * from events where workspace_id=? and id>?"
    params = [m.ws, 0]
    for col, val in (("agent_id", a.agent), ("type", a.type), ("run_id", a.run)):
        if val:
            sql += f" and {col} like ?"
            params.append(f"%{val}%")
    if a.rejected:
        sql += " and type in ('rejected','agent_denied','error')"
    if a.task:
        sql += " and payload like ?"
        params.append(f'%"task": "{a.task}"%')
    rows = m.store.q(sql + " order by id", *params)[-a.last:]
    for r in rows:
        print(_event_line(r))
    last = rows[-1]["id"] if rows else (m.store.one("select max(id) m from events")["m"] or 0)
    while a.follow:
        time.sleep(1)
        params[1] = last
        for r in m.store.q(sql + " order by id", *params):
            print(_event_line(r), flush=True)
            last = r["id"]


def cmd_messages(m: Mesh, a):
    sql = "select * from messages where workspace_id=?"
    params = [m.ws]
    if a.run:
        sql += " and run_id=?"
        params.append(a.run)
    if a.agent:
        sql += " and (from_agent like ? or to_agent like ?)"
        params += [f"%{a.agent}%"] * 2
    for r in m.store.q(sql + " order by id", *params):
        urgent = f"{BOLD}URGENT{END} " if r["urgent"] else ""
        thread = f" (reply to #{r['reply_to']})" if r["reply_to"] else ""
        state = f"{RED}{r['state']}{END}" if r["state"] == "recipient_offline" else r["state"]
        print(f"{_ts(r['created_at'])} #{r['id']} {r['from_agent']} -> {r['to_agent']} [{state}]{thread} {urgent}")
        print(f"    {r['body'][:300]}")
        if a.effects:  # what the receiver did right after reading it
            after = m.store.q("select * from events where agent_id=? and created_at>=? order by id limit ?",
                              r["to_agent"], r["created_at"], a.effects)
            for e in after:
                print(f"    {DIM}-> {_event_line(e)}{END}")


def cmd_agents(m: Mesh, a):
    for r in m.store.q("select * from agents where workspace_id=? order by heartbeat_at desc", m.ws):
        flag = RED if r["approval"] != "approved" else ""
        print(f"{flag}{r['id']:<34} {r['origin']:<9} {r['approval']:<9} "
              f"{'online' if m.online(r) else 'offline':<8} {r['program_path'] or ''}{END if flag else ''}")


def cmd_approvals(m: Mesh, a):
    for r in m.store.q("select * from approvals where state='pending' order by id"):
        print(f"#{r['id']} {r['agent_id']} task {r['task_id']}: {r['request']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="axon", description="Axonmesh coordination layer")
    p.add_argument("--repo", default=".")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server over stdio (what MCP clients launch)")
    e = sub.add_parser("events", help="event viewer: every tool call and action")
    e.add_argument("--follow", "-f", action="store_true")
    e.add_argument("--agent")
    e.add_argument("--type")
    e.add_argument("--run")
    e.add_argument("--task")
    e.add_argument("--rejected", action="store_true", help="only rejected / denied / errors")
    e.add_argument("--last", type=int, default=200)
    ms = sub.add_parser("messages", help="message viewer: every agent-to-agent message")
    ms.add_argument("--run")
    ms.add_argument("--agent")
    ms.add_argument("--effects", type=int, default=0, metavar="N",
                    help="show the receiver's next N events after each message")
    sub.add_parser("agents", help="connected agents and their approval state")
    sub.add_parser("approvals", help="pending ask-mode requests")
    for verb in ("approve", "deny"):
        v = sub.add_parser(verb)
        v.add_argument("what", choices=["agent", "request"])
        v.add_argument("id")
    x = sub.add_parser("export", help="write a read-only export from axon.db")
    x.add_argument("kind", choices=["events", "state", "handoff"])
    sub.add_parser("hook", help="Claude Code PostToolUse hook (reads the hook JSON on stdin)")
    st = sub.add_parser("setup", help="register Axonmesh with an agent tool (shows the diff, asks first)")
    st.add_argument("target", nargs="?", choices=["claude-code", "cursor", "gemini", "codex"])
    st.add_argument("--yes", action="store_true", help="apply without asking")
    st.add_argument("--undo", action="store_true", help="remove exactly what setup added")
    sub.add_parser("doctor", help="read-only check of every supported tool's setup")
    a = p.parse_args(argv)

    if a.cmd == "serve":
        from .server import serve

        serve(a.repo)
        return 0
    if a.cmd == "hook":
        try:
            from .hook import run_hook

            out = run_hook(sys.stdin.read(), a.repo)
            if out:
                print(out)
        except Exception:
            pass  # a coordination hiccup must never break the user's Claude Code session
        return 0
    if a.cmd in ("setup", "doctor"):
        from pathlib import Path

        from . import setup as S

        repo, home = Path(a.repo).resolve(), Path.home()
        log = Path.home() / ".axonmesh" / "setup-log.json"
        if a.cmd == "doctor":
            print("\n".join(S.doctor(repo, home)))
            return 0
        if a.undo:
            removed = S.undo(log)
            print("removed Axonmesh entries from:\n  " + "\n  ".join(removed) if removed else "nothing to undo")
            return 0
        if not a.target:
            print("choose a target: claude-code, cursor, gemini or codex", file=sys.stderr)
            return 2
        changes = S.plan(a.target, repo, home)
        if not changes:
            print(f"{a.target}: already set up")
            return 0
        print(S.show(changes))
        if not a.yes and input("\nApply these changes? Backups are made first. [y/N] ").strip().lower() != "y":
            print("nothing changed")
            return 1
        S.apply(changes, log)
        print(f"done. Undo any time with: axon setup --undo")
        return 0
    m = Mesh(a.repo)
    try:
        if a.cmd in ("approve", "deny"):
            ok = a.cmd == "approve"
            m.decide_agent(a.id, ok) if a.what == "agent" else m.decide_request(int(a.id), ok)
            print(f"{a.what} {a.id}: {'approved' if ok else 'denied'}")
        elif a.cmd == "export":
            print(m.export(a.kind))
        else:
            {"events": cmd_events, "messages": cmd_messages, "agents": cmd_agents, "approvals": cmd_approvals}[a.cmd](m, a)
    except MeshError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
