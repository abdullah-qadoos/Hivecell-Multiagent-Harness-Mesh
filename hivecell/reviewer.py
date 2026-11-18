"""Reviewer agent: second step of the quality gate, after the tests pass.

It sees the task's diff, the author's rules file and the contracts, and must call submit_review.
No verdict counts as a rejection: nothing gets through without a review.
"""
from pathlib import Path

from axonmesh.paths import glob_prefix

from .core.loop import AgentResult, Log, run_agent
from .core.tools import SPECS, Rejected, ToolRouter, safe_rel
from .models import Model
from .rules import AgentRules
from .sandbox import git

MAX_DIFF = 60_000

READ_DIFF = {
    "name": "read_diff",
    "description": "The full diff of this task's work in the author's owned paths.",
    "parameters": {"type": "object", "properties": {}},
}
SUBMIT = {
    "name": "submit_review",
    "description": "Your verdict. findings: concrete problems as 'path:line: problem', or why it is fine.",
    "parameters": {
        "type": "object",
        "properties": {"approved": {"type": "boolean"}, "findings": {"type": "string"}},
        "required": ["approved", "findings"],
    },
}


class ReviewRouter:
    mode = "review"

    def __init__(self, worktree: Path, base: str, author: AgentRules, contracts: str):
        reader = AgentRules(name="reviewer", model="-", role="-", owns=[], can_read=["**"],
                            tools=["read_file", "list_files"])
        self.reader = ToolRouter(reader, worktree)
        self.worktree, self.base, self.author, self.contracts = worktree, base, author, contracts
        self.done: str | None = None
        self.verdict: tuple[bool, str] | None = None

    def specs(self):
        return [SPECS["read_file"], SPECS["list_files"], READ_DIFF, SUBMIT]

    def call(self, name, args):
        if name == "read_file" and not safe_rel(args.get("path", "")):
            raise Rejected("hidden files and folders are off limits")
        if name in ("read_file", "list_files"):
            return self.reader.call(name, args)
        if name == "read_diff":
            prefixes = [p for p in (glob_prefix(g) for g in self.author.owns) if p]
            diff = git(self.worktree, "diff", self.base, "HEAD", "--", *prefixes)
            return diff[:MAX_DIFF] or "(empty diff)"
        if name == "submit_review":
            self.verdict = (bool(args["approved"]), str(args["findings"]))
            self.done = "approved" if self.verdict[0] else "changes requested"
            return "recorded"
        raise Rejected(f"unknown tool {name!r}")


def review(reviewer: AgentRules, model: Model, worktree: Path, base: str, author: AgentRules, task_title: str,
           contracts: str, log: Log) -> tuple[bool, str, AgentResult]:
    router = ReviewRouter(worktree, base, author, contracts)
    rules = "\n".join(f"- {r}" for r in author.rules) or "- none"
    prompt = (f"Review the work for task: {task_title}\n\n"
              f"Author: {author.name}, role: {author.role}\nOwned paths: {author.owns}\nAuthor's rules:\n{rules}\n"
              f"Done when: {author.done_when}\n\nContracts:\n{contracts or 'none'}\n\n"
              "The tests already pass. Check the diff against the rules and the contracts: correctness, missing "
              "edge cases, missing tests the rules require, contract mismatches. Then call submit_review.")
    res = run_agent(reviewer, model, router, prompt, log)
    if router.verdict is None:
        return False, f"the reviewer gave no verdict ({res.status})", res
    return router.verdict[0], router.verdict[1], res
