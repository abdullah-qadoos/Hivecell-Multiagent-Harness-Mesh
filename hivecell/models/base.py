"""Provider-neutral message format shared by every adapter.

messages: {"role": "user", "content": str}
          {"role": "assistant", "text": str, "tool_calls": [ToolCall]}
          {"role": "tool", "id": str, "content": str}
tools:    {"name": str, "description": str, "parameters": <JSON schema>}
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class Model(Protocol):
    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> Reply: ...
