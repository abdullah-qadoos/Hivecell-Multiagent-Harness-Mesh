"""Claude Code PostToolUse hook (decisions 4 and 8).

After every Claude Code tool call it:
  1. records the call in the Axonmesh event log, so mesh-only runs show up in the event viewer;
  2. hands any unread peer messages to Claude as extra context, so they arrive within seconds.

The hook finds its agent by process ancestry: the Claude Code process that runs this hook is the same one
that launched the Axonmesh MCP server, whose pid was stored when the agent registered.
It never blocks Claude Code: any error means "do nothing".
"""
import json
import os

from .mesh import Mesh, wrap_message


def ancestor_pids() -> list[int]:
    import psutil

    try:
        return [p.pid for p in psutil.Process(os.getpid()).parents()]
    except psutil.Error:
        return []


def run_hook(stdin_text: str, repo: str = ".", pids: list[int] | None = None, mesh: Mesh | None = None) -> str | None:
    data = json.loads(stdin_text or "{}")
    m = mesh or Mesh(data.get("cwd") or repo)
    a = m.agent_for_pids(pids if pids is not None else ancestor_pids())
    if not a or a["approval"] != "approved":
        return None
    aid = a["id"]
    tool = data.get("tool_name", "?")
    m.log(aid, "external_tool", {"tool": tool, "input": json.dumps(data.get("tool_input"), default=str)[:300]})
    m.touch(aid)
    msgs = m.inbox(aid)
    if not msgs:
        return None
    ids = [r["id"] for r in msgs]
    context = "\n\n".join(wrap_message(r) for r in msgs)
    context += f"\n\nWhen you have read these, call the axonmesh update_work tool with ack={ids}."
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}})
