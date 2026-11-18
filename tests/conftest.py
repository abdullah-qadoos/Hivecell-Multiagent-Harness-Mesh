import itertools
import subprocess

import pytest

from hivecell.models import Reply, ToolCall
from hivecell.rules import AgentRules

_ids = itertools.count()


def call(name, **args):
    return ToolCall(f"c{next(_ids)}", name, args)


class FakeModel:
    """Replays a script: each item is one model turn, a list of tool calls."""

    def __init__(self, turns, tokens_per_turn=100):
        self.turns = list(turns)
        self.tokens = tokens_per_turn

    def complete(self, system, messages, tools):
        self.last_messages = messages
        calls = self.turns.pop(0) if self.turns else []
        return Reply("", calls, self.tokens, self.tokens)


def rules(**over):
    base = dict(name="backend", model="fake/x", role="r", owns=["src/**"], can_read=["docs/**"],
                tools=["read_file", "write_file", "list_files", "run_tests"])
    return AgentRules(**{**base, **over})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=r, check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    (r / "README.md").write_text("target\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return r
