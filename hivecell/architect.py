"""Architect agent: spec -> plan (task graph, ownership via agents, contracts).

The architect proposes; the harness validates. Every submitted plan is checked in code (YAML, schema,
known agents, dependencies, cycles) and errors go back to the architect until it gets it right.
The written plan file is the plan gate: you read or edit it, then run it.
"""
import os
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

import yaml
from pydantic import ValidationError

from .core.loop import AgentResult, Log, run_agent
from .core.tools import SPECS, Rejected, ToolRouter, safe_rel
from .models import Model
from .orchestrator import Plan, check_agents
from .rules import AgentRules, load_rules

SUBMIT = {
    "name": "submit_plan",
    "description": "Submit the plan as YAML. It is validated; fix any error you get back and submit again.",
    "parameters": {"type": "object", "properties": {"plan_yaml": {"type": "string"}}, "required": ["plan_yaml"]},
}

FORMAT = """Plan format (YAML):
tasks:
  - id: short-id
    agent: <one of the agents above>
    kind: feature | fix | refactor | test | docs
    title: one concrete, testable sentence
    depends_on: [other-task-ids]      # only real dependencies; independent tasks run in parallel
contracts:                            # interfaces agreed before coding, so agents can work in parallel
  - name: todo-store
    schema: |
      class TodoStore: add(title: str) -> int; ...
reviewer: <agent name>                # optional, only if a reviewer agent is listed"""


class ArchitectRouter:
    mode = "architect"

    def __init__(self, repo: Path, catalog: dict[str, tuple[Path, AgentRules]], out: Path, spec: str):
        reader = AgentRules(name="architect", model="-", role="-", owns=[], can_read=["**"],
                            tools=["read_file", "list_files"])
        self.reader = ToolRouter(reader, repo)
        self.catalog, self.out, self.spec = catalog, out, spec
        self.done: str | None = None
        self.plan_text = ""

    def specs(self):
        return [SPECS["read_file"], SPECS["list_files"], SUBMIT]

    def call(self, name, args):
        if name == "read_file":
            if not safe_rel(args.get("path", "")):
                raise Rejected("hidden files and folders are off limits")
            return self.reader.call(name, args)
        if name == "list_files":
            return self.reader.call(name, args)
        if name == "submit_plan":
            return self._submit(**args)
        raise Rejected(f"unknown tool {name!r}")

    def _submit(self, plan_yaml: str) -> str:
        try:
            data = yaml.safe_load(plan_yaml)
        except yaml.YAMLError as e:
            raise Rejected(f"not valid YAML: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
            raise Rejected("the plan must be a mapping with a non-empty `tasks` list")
        used = {t.get("agent") for t in data["tasks"] if isinstance(t, dict)}
        if data.get("reviewer"):
            used.add(data["reviewer"])
        unknown = used - set(self.catalog)
        if unknown:
            raise Rejected(f"unknown agents {sorted(map(str, unknown))}; available: {sorted(self.catalog)}")
        data["spec"] = self.spec
        data["agents"] = [_rel_or_abs(self.catalog[n][0], self.out.parent) for n in sorted(used)]
        try:
            plan = Plan.model_validate(data)
            check_agents(plan, {n: self.catalog[n][1] for n in used})
            TopologicalSorter({t.id: set(t.depends_on) for t in plan.tasks}).prepare()
        except (ValidationError, ValueError) as e:
            raise Rejected(f"invalid plan: {e}")
        except CycleError as e:
            raise Rejected(f"dependency cycle: {e.args[1]}")
        ordered = {k: data[k] for k in ("spec", "agents", "tasks", "contracts", "reviewer", "max_attempts") if k in data}
        self.plan_text = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
        self.done = f"{len(plan.tasks)} tasks"
        return "plan accepted"


def _rel_or_abs(path: Path, start: Path) -> str:
    try:
        return Path(os.path.relpath(path, start)).as_posix()
    except ValueError:  # different drives on Windows
        return Path(path).as_posix()


def catalog_text(catalog) -> str:
    lines = []
    for name, (_, r) in catalog.items():
        lines.append(f"- {name}: {r.role}\n  owns {r.owns}, reads {r.can_read}, tools {r.tools}")
    return "\n".join(lines)


def make_plan(spec: str, repo: Path, agent_files: list[Path], architect: AgentRules, model: Model,
              out: Path, log: Log) -> tuple[str, AgentResult]:
    catalog = {}
    for f in agent_files:
        r = load_rules(f)
        if r.name != architect.name:
            catalog[r.name] = (Path(f).resolve(), r)
    router = ArchitectRouter(repo, catalog, out.resolve(), spec)
    prompt = (f"Spec:\n{spec}\n\nAvailable agents (each can only write the paths it owns):\n{catalog_text(catalog)}\n\n"
              f"{FORMAT}\n\nRead the repository if it helps, then call submit_plan.")
    res = run_agent(architect, model, router, prompt, log)
    if router.plan_text:
        out.write_text(router.plan_text, encoding="utf-8")
    return router.plan_text, res
