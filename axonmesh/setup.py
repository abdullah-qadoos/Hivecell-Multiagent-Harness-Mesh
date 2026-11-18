"""`axon setup` / `axon doctor` (decision 7).

setup: shows the exact diff per file, writes only after you confirm, backs files up, pre-approves only the exact
Axonmesh tool names, records what it added so `--undo` removes exactly that.
doctor: read-only report of what is missing and the command that fixes it.

Targets:
  claude-code  .mcp.json, .claude/settings.json (allow list + PostToolUse hook)      project level
  cursor       .cursor/mcp.json                                                     project level
  gemini       .gemini/settings.json (includeTools + trust, limited to Axonmesh)    project level
  codex        ~/.codex/config.toml (marked block)                                 user level
"""
import difflib
import json
import shutil
import sys
from pathlib import Path

TOOLS = ["start_work", "inspect_work", "update_work", "transfer_work", "finish_work", "publish_contract", "read_contracts"]
TARGETS = ["claude-code", "cursor", "gemini", "codex"]
BEGIN, END = "# >>> axonmesh (added by axon setup)", "# <<< axonmesh"


def axon_exe() -> str:
    """The installed `axon` launcher, so clients don't pay uv's start-up cost on every call."""
    here = Path(sys.executable).parent
    for name in ("axon.exe", "axon"):
        if (here / name).exists():
            return str(here / name)
    return shutil.which("axon") or "axon"


def server_entry() -> dict:
    return {"command": axon_exe(), "args": ["--repo", ".", "serve"]}


def hook_entry() -> dict:
    return {"matcher": "*", "hooks": [{"type": "command", "command": f'"{axon_exe()}" --repo . hook'}]}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.read_text(encoding="utf-8").strip() else {}


def plan(target: str, repo: Path, home: Path) -> list[tuple[Path, str, str]]:
    """[(file, old text, new text)] for one target. Nothing is written here."""
    changes = []

    def json_change(path: Path, edit):
        old = _read_json(path)
        new = edit(json.loads(json.dumps(old)))
        old_t = path.read_text(encoding="utf-8") if path.exists() else ""
        new_t = json.dumps(new, indent=2) + "\n"
        if new != old:
            changes.append((path, old_t, new_t))

    if target in ("claude-code", "cursor"):
        mcp = repo / (".mcp.json" if target == "claude-code" else ".cursor/mcp.json")
        json_change(mcp, lambda d: {**d, "mcpServers": {**d.get("mcpServers", {}), "axonmesh": server_entry()}})
    if target == "claude-code":
        def settings(d):
            perms = d.setdefault("permissions", {})
            allow = perms.setdefault("allow", [])
            allow += [f"mcp__axonmesh__{t}" for t in TOOLS if f"mcp__axonmesh__{t}" not in allow]
            post = d.setdefault("hooks", {}).setdefault("PostToolUse", [])
            if not any("hook" in h.get("command", "") and "axon" in h.get("command", "")
                       for e in post for h in e.get("hooks", [])):
                post.append(hook_entry())
            return d
        json_change(repo / ".claude" / "settings.json", settings)
    if target == "gemini":
        entry = {**server_entry(), "includeTools": TOOLS, "trust": True}
        json_change(repo / ".gemini" / "settings.json",
                    lambda d: {**d, "mcpServers": {**d.get("mcpServers", {}), "axonmesh": entry}})
    if target == "codex":
        path = home / ".codex" / "config.toml"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if BEGIN not in old:
            e = server_entry()
            block = (f"\n{BEGIN}\n[mcp_servers.axonmesh]\ncommand = {json.dumps(e['command'])}\n"
                     f"args = {json.dumps(e['args'])}\n{END}\n")
            changes.append((path, old, old + block))
    return changes


def show(changes) -> str:
    out = []
    for path, old, new in changes:
        out += difflib.unified_diff(old.splitlines(), new.splitlines(), f"{path} (before)", f"{path} (after)", lineterm="")
    return "\n".join(out)


def apply(changes, log_path: Path):
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    for path, old, new in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + ".axon-backup")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(new, encoding="utf-8")
        log.append({"file": str(path), "existed": bool(old)})
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")


def undo(log_path: Path) -> list[str]:
    """Remove exactly what setup added; leave everything else in those files alone."""
    if not log_path.exists():
        return []
    done = []
    for item in json.loads(log_path.read_text(encoding="utf-8")):
        path = Path(item["file"])
        if not path.exists():
            continue
        if path.suffix == ".toml":
            text = path.read_text(encoding="utf-8")
            if BEGIN in text:
                start, end = text.index(BEGIN), text.index(END) + len(END)
                path.write_text((text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")).lstrip("\n"),
                                encoding="utf-8")
        else:
            d = _read_json(path)
            d.get("mcpServers", {}).pop("axonmesh", None)
            allow = d.get("permissions", {}).get("allow")
            if allow is not None:
                d["permissions"]["allow"] = [a for a in allow if not a.startswith("mcp__axonmesh__")]
            post = d.get("hooks", {}).get("PostToolUse")
            if post is not None:
                d["hooks"]["PostToolUse"] = [e for e in post if e != hook_entry()]
            path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        done.append(str(path))
    log_path.unlink()
    return sorted(set(done))


def doctor(repo: Path, home: Path) -> list[str]:
    """Read-only. One line per check."""
    ok, bad = "OK  ", "FIX "
    lines = []
    exe = axon_exe()
    lines.append((ok if Path(exe).exists() or shutil.which(exe) else bad) + f"axon launcher: {exe}")
    for target in TARGETS:
        missing = plan(target, repo, home)
        if missing:
            files = ", ".join(str(p) for p, _, _ in missing)
            lines.append(f"{bad}{target}: not set up ({files}). Run: axon setup {target}")
        else:
            lines.append(f"{ok}{target}: registered" + (", tools pre-approved, hook installed" if target == "claude-code" else ""))
    lines.append("NOTE codex: tool approvals in headless `codex exec` depend on its approval policy; check it allows MCP calls.")
    lines.append("NOTE gemini/cursor: config keys follow their docs at the time of writing; verify after upgrades.")
    return lines
