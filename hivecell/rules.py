"""Agent rules files: one YAML per agent, validated on load."""
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

KNOWN_TOOLS = {"read_file", "write_file", "list_files", "run_tests", "run_command",
               "send_message", "publish_contract", "read_contracts"}
READ_ONLY_TOOLS = {"read_file", "list_files", "read_contracts", "send_message"}  # what plan mode allows


class Budget(BaseModel):
    max_tokens: int = 400_000
    max_minutes: float = 30


class Price(BaseModel):
    input_per_mtok: float = 0
    output_per_mtok: float = 0


class AgentRules(BaseModel):
    name: str
    model: str  # "provider/model-id", e.g. anthropic/claude-sonnet-5-5, grok/grok-4
    role: str
    owns: list[str]
    can_read: list[str] = []
    tools: list[str]
    rules: list[str] = []
    budget: Budget = Budget()
    done_when: str = ""
    category: str = ""  # for the template gallery: coding, media, research...
    test_cmd: str | None = "python -m pytest -q"  # the done check; None/empty = no check, the reviewer decides
    commands: list[str] = []  # globs run_command may run without asking in ask mode, e.g. "pip install *"
    mode: Literal["plan", "auto", "ask"] | None = None  # per-agent override of the run mode
    image: str = "python:3.13-slim"  # Docker image run_command uses
    # shortcut: price lives in each rules file, move to a shared price table when agents share models
    price: Price = Price()

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, tools):
        unknown = set(tools) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"unknown tools {sorted(unknown)}; v0 supports {sorted(KNOWN_TOOLS)}")
        return tools

    @field_validator("test_cmd")
    @classmethod
    def _blank_is_none(cls, v):
        return v.strip() if v and v.strip() else None

    def may_write(self, rel: str) -> bool:
        return _matches(rel, self.owns)

    def may_read(self, rel: str) -> bool:
        return _matches(rel, self.owns + self.can_read)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.price.input_per_mtok + output_tokens * self.price.output_per_mtok) / 1e6


def _matches(rel: str, globs: list[str]) -> bool:
    path = PurePosixPath(rel)
    return any(path.full_match(g) for g in globs)


def load_rules(path: str | Path) -> AgentRules:
    return AgentRules.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
