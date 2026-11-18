"""Agent library: where user-made agents live, and how a plain-words description becomes one.

Two places, the repo's version wins on a name clash:
  personal  ~/.hivecell/agents/<name>.yaml        every repo can use them
  repo      <repo>/.hivecell-agents/<name>.yaml   commit it to share with a team
Templates ship in the project's agents/ folder.
"""
import os
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from axonmesh.paths import glob_prefix, overlaps

from .core.loop import AgentResult, run_agent
from .core.tools import Rejected
from .models import Model
from .rules import KNOWN_TOOLS, AgentRules, load_rules

TEMPLATES = Path(__file__).resolve().parents[1] / "agents"
SYSTEM_AGENTS = {"architect"}  # used by the harness itself, not offered as a team member
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def personal_dir() -> Path:
    return Path(os.environ.get("HIVECELL_HOME", Path.home() / ".hivecell")) / "agents"


def repo_dir(repo: Path) -> Path:
    return Path(repo) / ".hivecell-agents"


def _dir(scope: str, repo: Path | None) -> Path:
    if scope == "personal":
        return personal_dir()
    if scope == "repo":
        if not repo:
            raise ValueError("open a repository to save agents in it")
        return repo_dir(repo)
    raise ValueError("scope must be personal or repo")


def _load_dir(d: Path, scope: str) -> dict[str, dict]:
    out = {}
    for f in sorted(d.glob("*.yaml")) if d.exists() else []:
        try:
            r = load_rules(f)
            out[r.name] = {"name": r.name, "scope": scope, "path": str(f), "rules": r, "error": None}
        except (ValidationError, ValueError, yaml.YAMLError) as e:  # show broken files instead of hiding them
            out[f.stem] = {"name": f.stem, "scope": scope, "path": str(f), "rules": None, "error": str(e)}
    return out


def list_agents(repo: Path | None) -> dict[str, dict]:
    agents = _load_dir(personal_dir(), "personal")
    if repo:
        agents |= _load_dir(repo_dir(repo), "repo")  # repo version wins
    return agents


def templates() -> dict[str, AgentRules]:
    return {r.name: r for r in (load_rules(f) for f in sorted(TEMPLATES.glob("*.yaml"))) if r.name not in SYSTEM_AGENTS}


def validate(data: dict) -> AgentRules:
    name = str(data.get("name", ""))
    if not NAME.match(name):
        raise ValueError("name: lowercase letters, digits, - or _, up to 40 characters")
    if name in SYSTEM_AGENTS:
        raise ValueError(f"{name!r} is reserved for the harness")
    return AgentRules.model_validate(data)


def save(data: dict, scope: str, repo: Path | None, replace: str | None = None) -> Path:
    """Validate, then write. `replace` is the agent being edited (may be renamed); otherwise names must be new."""
    rules = validate(data)
    d = _dir(scope, repo)
    path = d / f"{rules.name}.yaml"
    if path.exists() and rules.name != replace:
        raise ValueError(f"an agent named {rules.name!r} already exists here")
    d.mkdir(parents=True, exist_ok=True)
    dump = rules.model_dump(exclude_defaults=False)
    if not rules.price.input_per_mtok and not rules.price.output_per_mtok:
        dump.pop("price")
    path.write_text(yaml.safe_dump(dump, sort_keys=False, allow_unicode=True), encoding="utf-8")
    if replace and replace != rules.name:  # renamed: remove the old file
        (d / f"{replace}.yaml").unlink(missing_ok=True)
    return path


def delete(name: str, scope: str, repo: Path | None):
    if not NAME.match(name):
        raise ValueError("bad name")
    path = _dir(scope, repo) / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"no agent {name!r} in {scope}")
    path.unlink()


def overlap_warnings(agents: dict[str, dict]) -> list[str]:
    """Two agents writing the same folders would block each other before a run starts."""
    owned = [(n, glob_prefix(g)) for n, a in agents.items() if a["rules"] for g in a["rules"].owns]
    warnings = []
    for i, (a, pa) in enumerate(owned):
        for b, pb in owned[i + 1:]:
            if a != b and (not pa or not pb or overlaps(pa, pb)):
                warnings.append(f"{a} and {b} both write {pa or pb or 'everything'}: don't put them in the same run")
    return sorted(set(warnings))


# ---------- describe it in plain words -> agent ----------
SUBMIT = {
    "name": "submit_agent",
    "description": "Submit the agent as YAML. It is validated; fix any error you get back and submit again.",
    "parameters": {"type": "object", "properties": {"agent_yaml": {"type": "string"}}, "required": ["agent_yaml"]},
}

FIELDS = f"""Fields:
name: lowercase-with-dashes
category: coding | media | research | writing | data | other
model: provider/model-id (providers: anthropic, gemini, grok, opencode, nvidia, openai, ollama)
role: one sentence, what it does
owns: folders it may write, as globs like "clips/**"  (keep them specific)
can_read: folders it may read
tools: subset of {sorted(KNOWN_TOOLS)}
commands: shell command patterns it may run without asking, like "ffmpeg *" (needs the run_command tool)
image: Docker image that has those programs, e.g. "jrottenberg/ffmpeg:6-ubuntu" or "python:3.13-slim"
rules: list of concrete rules
test_cmd: a command that exits 0 when the work is done right, or null if there is no automatic check
budget: {{max_tokens: 200000, max_minutes: 20}}"""


class _DraftRouter:
    mode = "architect"  # same finishing convention: the rules say to call the submit tool

    def __init__(self, default_model: str):
        self.default_model = default_model
        self.done: str | None = None
        self.agent: AgentRules | None = None

    def specs(self):
        return [SUBMIT]

    def call(self, name, args):
        if name != "submit_agent":
            raise Rejected(f"unknown tool {name!r}")
        try:
            data = yaml.safe_load(args["agent_yaml"])
            if not isinstance(data, dict):
                raise ValueError("the agent must be a YAML mapping")
            data.setdefault("model", self.default_model)
            self.agent = validate(data)
        except (yaml.YAMLError, ValidationError, ValueError) as e:
            raise Rejected(f"invalid agent: {e}")
        self.done = self.agent.name
        return "agent accepted"


def draft(description: str, drafter: AgentRules, model: Model) -> tuple[AgentRules | None, AgentResult]:
    router = _DraftRouter(drafter.model)
    example = yaml.safe_dump(templates()["clipper"].model_dump(exclude={"price"}), sort_keys=False) \
        if "clipper" in templates() else ""
    prompt = (f"Design one agent for this need:\n{description}\n\n{FIELDS}\n\nExample agent:\n{example}\n"
              "Give it only the tools and folders it needs. Then call submit_agent.")
    res = run_agent(drafter, model, router, prompt, lambda t, p: None)
    return router.agent, res
