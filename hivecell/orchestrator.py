"""Runs a plan: a task graph where each task belongs to one agent.

All state lives in Axonmesh (decision 12): the orchestrator keeps no task table of its own.
The plan is written by hand for now (see examples/); the architect agent comes later.
"""
import asyncio
import sys
import time
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from axonmesh import Mesh, wrap_message
from axonmesh.paths import glob_prefix

from . import sandbox
from .core.loop import run_agent
from .core.tools import Context, ToolRouter
from .models import Model, make_model
from .rules import AgentRules, load_rules


class TaskSpec(BaseModel):
    id: str
    agent: str
    title: str
    kind: str = "feature"
    depends_on: list[str] = []


class ContractSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    schema_: str = Field(alias="schema")


class Plan(BaseModel):
    spec: str
    agents: list[str]  # paths to rules files, relative to the plan file
    tasks: list[TaskSpec]
    contracts: list[ContractSpec] = []  # agreed before any code is written
    reviewer: str | None = None  # name of an agent (in `agents`) that reviews each task after its tests pass
    max_attempts: int = 2

    @model_validator(mode="after")
    def _check_ids(self):
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        for t in self.tasks:
            missing = set(t.depends_on) - set(ids)
            if missing:
                raise ValueError(f"task {t.id} depends on unknown tasks {sorted(missing)}")
        return self


def load_plan(path: str | Path) -> tuple[Plan, dict[str, AgentRules]]:
    path = Path(path)
    plan = Plan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    rules = {}
    for p in plan.agents:
        r = load_rules(path.parent / p)
        rules[r.name] = r
    check_agents(plan, rules)
    return plan, rules


def check_agents(plan: Plan, rules: dict[str, AgentRules]):
    for t in plan.tasks:
        if t.agent not in rules:
            raise ValueError(f"task {t.id} uses agent {t.agent!r}, which has no rules file")
    if plan.reviewer and plan.reviewer not in rules:
        raise ValueError(f"reviewer {plan.reviewer!r} has no rules file")
    if plan.reviewer and any(t.agent == plan.reviewer for t in plan.tasks):
        raise ValueError("the reviewer can't also own tasks: it would review its own work")


@dataclass
class TaskReport:
    id: str
    agent: str
    status: str = "skipped"  # done | proposed | failed | skipped (a dependency failed)
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    notes: list[str] = field(default_factory=list)
    proposal: str = ""


@dataclass
class RunReport:
    run_id: str
    mode: str
    status: str
    seconds: float
    tasks: list[TaskReport]
    branches: dict[str, str]
    rejected_calls: int = 0
    messages: int = 0
    overlaps_prevented: list[str] = field(default_factory=list)
    integration: str = ""

    @property
    def tokens(self):
        return sum(t.input_tokens + t.output_tokens for t in self.tasks)

    @property
    def cost(self):
        return sum(t.cost for t in self.tasks)

    @property
    def retries(self):
        return sum(max(t.attempts - 1, 0) for t in self.tasks)


def _stderr(msg: str):
    print(msg, file=sys.stderr, flush=True)


class Orchestrator:
    def __init__(
        self,
        plan: Plan,
        rules: dict[str, AgentRules],
        mesh: Mesh,
        mode: str = "ask",
        model_factory: Callable[[str], Model] = make_model,
        notify: Callable[[str], None] = _stderr,
    ):
        self.plan, self.rules, self.mesh, self.mode = plan, rules, mesh, mode
        self.repo = mesh.root
        self.model_factory, self.notify = model_factory, notify
        self.specs = {t.id: t for t in plan.tasks}
        self.reports = {t.id: TaskReport(t.id, t.agent) for t in plan.tasks}
        self.spent = {name: 0 for name in rules}  # token budget is per agent, across attempts
        self.worktrees: dict[str, Path] = {}
        self.branches: dict[str, str] = {}
        self.aids: dict[str, str] = {}  # agent name -> mesh agent id
        self.tids: dict[str, str] = {}  # plan task id -> mesh task id
        self.run_id = ""

    def log(self, agent: str, type_: str, payload: dict):
        self.mesh.log(self.aids.get(agent), type_, payload, self.run_id)

    async def run(self) -> RunReport:
        start = time.time()
        sandbox.check_repo(self.repo)
        graph = TopologicalSorter({t.id: set(t.depends_on) for t in self.plan.tasks})
        graph.prepare()  # raises CycleError before any agent starts
        m = self.mesh
        self.run_id = m.create_run(self.plan.spec, self.mode)

        # one worktree per agent, created one at a time: git locks the repo while adding one
        for name in dict.fromkeys(t.agent for t in self.plan.tasks):
            r = self.rules[name]
            self.worktrees[name], self.branches[name] = sandbox.make_worktree(self.repo, self.run_id, name)
            self.aids[name] = m.register(name, "internal", "hivecell", run_id=self.run_id, model=r.model,
                                         rules_yaml=r.model_dump_json(), commands=r.commands,
                                         mode_override=r.mode, worktree=self.worktrees[name])
        self.base = sandbox.git(self.repo, "rev-parse", "HEAD")  # what the reviewer diffs against
        if self.plan.reviewer:
            r = self.rules[self.plan.reviewer]
            self.aids[self.plan.reviewer] = m.register(r.name, "internal", "hivecell", run_id=self.run_id,
                                                       model=r.model, rules_yaml=r.model_dump_json())
        if self.plan.contracts:  # agreed before any code is written
            arch = m.register("architect", "internal", "hivecell", run_id=self.run_id)
            for c in self.plan.contracts:
                m.publish_contract(arch, c.name, c.schema_, self.run_id)
        for t in self.plan.tasks:
            self.tids[t.id] = m.create_task(None, t.title, t.kind, self.run_id, owner=self.aids[t.agent],
                                            depends_on=t.depends_on, task_id=f"{self.run_id}:{t.id}", status="pending")

        report = RunReport(self.run_id, self.mode, "running", 0, list(self.reports.values()), self.branches)
        # ownership map claimed before any agent starts: overlaps are prevented, not found later (fix #11)
        if self.mode != "plan":
            for name, aid in self.aids.items():
                prefixes = [p for p in (glob_prefix(g) for g in self.rules[name].owns) if p]
                clashes = [c for c in m.claim(aid, prefixes, run_id=self.run_id) if not c.startswith("warning")]
                report.overlaps_prevented += [f"{name}: {c}" for c in clashes]
            if report.overlaps_prevented:
                for line in report.overlaps_prevented:
                    self.notify(f"OVERLAP before start: {line}")
                return self._finish(report, "blocked", start)

        agent_locks = {name: asyncio.Lock() for name in self.worktrees}  # one task at a time per worktree

        async def start_task(tid):
            async with agent_locks[self.specs[tid].agent]:
                try:
                    return await asyncio.to_thread(self._run_task, tid)
                except Exception as e:  # e.g. a merge conflict: fail this task, keep the run going
                    rep = self.reports[tid]
                    rep.status = "failed"
                    rep.notes.append(f"{type(e).__name__}: {e}")
                    self.log(self.specs[tid].agent, "error", {"task": self.tids[tid], "error": str(e)})
                    m.set_status(self.tids[tid], "failed")
                    return False

        pending: set[asyncio.Task] = set()
        while graph.is_active():
            for tid in graph.get_ready():
                pending.add(asyncio.create_task(start_task(tid), name=tid))
            if not pending:
                break  # everything left depends on a failed task
            finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for f in finished:
                if f.result():
                    graph.done(f.get_name())

        if self.mode == "plan":
            ok = all(r.status == "proposed" for r in self.reports.values())
            return self._finish(report, "proposed" if ok else "failed", start)
        if all(r.status == "done" for r in self.reports.values()):
            return self._finish(report, "passed" if self._integrate(report) else "failed", start)
        return self._finish(report, "failed", start)

    def _finish(self, report: RunReport, status: str, start: float) -> RunReport:
        m = self.mesh
        if status == "passed":
            m.release_claims(run_id=self.run_id)
        else:
            m.soften_claims(self.run_id)  # unmerged work stays protected until a human looks (fix #3)
        for name, aid in self.aids.items():
            m.go_offline(aid)
        report.status = status
        report.seconds = time.time() - start
        report.rejected_calls = m.store.one("select count(*) n from events where run_id=? and type='rejected'",
                                            self.run_id)["n"]
        report.messages = m.store.one("select count(*) n from messages where run_id=?", self.run_id)["n"]
        m.finish_run(self.run_id, status, report.tokens, report.cost)
        return report

    def _integrate(self, report: RunReport) -> bool:
        """Merge every agent's branch into one integration branch and run all test commands there."""
        wt, branch = sandbox.make_worktree(self.repo, self.run_id, "integration")
        report.integration = branch
        for name in self.worktrees:
            sandbox.merge(wt, self.branches[name])
        ok = True
        for t in self.plan.tasks:
            rules = self.rules[t.agent]
            if not rules.test_cmd:
                continue
            code, out = ToolRouter(rules, wt).test()
            self.mesh.add_evidence(self.tids[t.id], f"integration: `{rules.test_cmd}` exit {code}", "ci")
            self.log(t.agent, "integration", {"task": self.tids[t.id], "exit": code, "output": out[-1000:]})
            if code:
                ok = False
                self.reports[t.id].notes.append(f"integration tests failed (exit {code})")
        for t in self.plan.tasks:
            self.mesh.set_status(self.tids[t.id], "complete" if ok else "review_ready")
        return ok

    def _run_task(self, tid: str) -> bool:
        spec, rep, m = self.specs[tid], self.reports[tid], self.mesh
        rules, wt, aid, mtid = self.rules[spec.agent], self.worktrees[spec.agent], self.aids[spec.agent], self.tids[tid]
        log = lambda type_, payload: m.log(aid, type_, {"task": mtid, **payload}, self.run_id)

        # bring in finished work from other agents this task depends on
        for dep in spec.depends_on:
            dep_agent = self.specs[dep].agent
            if dep_agent != spec.agent and self.mode != "plan":
                sandbox.merge(wt, self.branches[dep_agent])
                log("merged", {"branch": self.branches[dep_agent]})

        def inbox() -> list[str]:
            m.touch(aid, "working")
            msgs = m.inbox(aid)
            if msgs:  # the harness put them in the agent's context, so they are read
                m.ack(aid, [r["id"] for r in msgs])
            return [wrap_message(r) for r in msgs]

        ctx = Context(m, aid, self.run_id, mtid, self.aids, self.notify)
        feedback = ""
        for attempt in range(1, self.plan.max_attempts + 1):
            rep.attempts = attempt
            m.set_status(mtid, "in_progress", attempts=attempt)
            remaining = rules.budget.max_tokens - self.spent[spec.agent]
            if remaining <= 0:
                rep.notes.append("agent token budget used up")
                break
            prompt = f"Project spec:\n{self.plan.spec}\n\nYour task ({tid}): {spec.title}"
            if feedback:
                prompt += f"\n\nYour previous attempt failed. Fix this:\n{feedback}"
            router = ToolRouter(rules, wt, ctx, self.mode)
            try:
                res = run_agent(rules, self.model_factory(rules.model), router, prompt, log,
                                token_limit=remaining, inbox=inbox)
            except Exception as e:
                feedback = f"harness error: {type(e).__name__}: {e}"
                rep.notes.append(feedback)
                log("error", {"attempt": attempt, "error": feedback})
                continue

            cost = rules.cost(res.input_tokens, res.output_tokens)
            self.spent[spec.agent] += res.input_tokens + res.output_tokens
            rep.input_tokens += res.input_tokens
            rep.output_tokens += res.output_tokens
            rep.cost += cost
            m.ledger(self.run_id, mtid, aid, rules.model, res.input_tokens, res.output_tokens, cost)

            if res.status != "done":
                feedback = f"you stopped without finishing ({res.status})"
                rep.notes.append(f"attempt {attempt}: {res.status}")
                continue

            if router.mode == "plan":
                rep.proposal, rep.status = res.summary, "proposed"
                m.note(aid, mtid, "proposal", res.summary)
                m.set_status(mtid, "proposed")
                return True

            code, out = router.test()  # quality gate: the done check, run by the harness, not the agent
            log("gate", {"attempt": attempt, "exit": code, "output": out[-1000:]})
            if rules.test_cmd:  # evidence is only labeled CI when a check really ran
                m.add_evidence(mtid, f"gate: `{rules.test_cmd}` exit {code}", "ci")
            if code != 0:
                feedback = f"the tests failed (exit {code}):\n{out}"
                rep.notes.append(f"attempt {attempt}: tests failed (exit {code})")
                continue
            sandbox.commit_all(wt, f"{tid}: {res.summary}"[:200])
            if self.plan.reviewer:  # gate step 2: tests passed, now the reviewer agent
                approved, findings = self._review(spec, rules, wt, rep, mtid)
                m.add_evidence(mtid, f"review: {'approved' if approved else 'changes requested'}: {findings}",
                               "reviewer")
                if not approved:
                    feedback = f"the reviewer requested changes:\n{findings}"
                    rep.notes.append(f"attempt {attempt}: reviewer requested changes")
                    continue
            m.add_evidence(mtid, f"summary: {res.summary}", "agent_reported")
            rep.status = "done"
            m.set_status(mtid, "review_ready")
            m.review_packet(mtid)
            return True

        sandbox.commit_all(wt, f"{tid}: failed, work in progress")  # keep the work for inspection
        rep.status = "failed"
        m.set_status(mtid, "failed")
        return False


    def _review(self, spec: TaskSpec, author: AgentRules, wt: Path, rep: TaskReport, mtid: str) -> tuple[bool, str]:
        from .reviewer import review

        name = self.plan.reviewer
        rv, aid, m = self.rules[name], self.aids[name], self.mesh
        contracts = "\n\n".join(f"## {c['name']} v{c['version']}\n{c['schema']}" for c in m.contracts())
        log = lambda type_, payload: m.log(aid, type_, {"task": mtid, **payload}, self.run_id)
        approved, findings, res = review(rv, self.model_factory(rv.model), wt, self.base, author, spec.title,
                                         contracts, log)
        cost = rv.cost(res.input_tokens, res.output_tokens)
        rep.input_tokens += res.input_tokens  # review cost is part of what the task cost
        rep.output_tokens += res.output_tokens
        rep.cost += cost
        m.ledger(self.run_id, mtid, aid, rv.model, res.input_tokens, res.output_tokens, cost)
        log("review", {"approved": approved, "findings": findings[:1000]})
        return approved, findings


def format_report(r: RunReport) -> str:
    lines = [f"Run {r.run_id} ({r.mode} mode): {r.status.upper()} in {r.seconds:.1f}s", ""]
    lines.append(f"{'task':<16}{'agent':<12}{'status':<10}{'tries':>5}{'tokens':>10}{'cost $':>9}")
    for t in r.tasks:
        lines.append(f"{t.id:<16}{t.agent:<12}{t.status:<10}{t.attempts:>5}"
                     f"{t.input_tokens + t.output_tokens:>10}{t.cost:>9.4f}")
        lines += [f"    - {n}" for n in t.notes]
        if t.proposal:
            lines += ["    proposal:"] + [f"      {l}" for l in t.proposal.splitlines()]
    lines += [
        "",
        f"total tokens {r.tokens}, cost ${r.cost:.4f}, retries {r.retries}, "
        f"rejected tool calls {r.rejected_calls}, messages {r.messages}",
        "branches: " + ", ".join(f"{a} -> {b}" for a, b in r.branches.items()),
    ]
    if r.integration:
        lines.append(f"integration branch: {r.integration}")
    lines += [f"overlap: {o}" for o in r.overlaps_prevented]
    lines.append(f"details: axon events --run {r.run_id}   |   axon messages --run {r.run_id}")
    return "\n".join(lines)
