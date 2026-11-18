from .base import Reply, ToolCall


class AnthropicModel:
    def __init__(self, model: str):
        import os

        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("set ANTHROPIC_API_KEY to use the anthropic provider")
        self.client = anthropic.Anthropic()
        self.model = model

    def complete(self, system, messages, tools):
        r = self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=system,
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
            messages=to_anthropic(messages),
        )
        return Reply(
            text="".join(b.text for b in r.content if b.type == "text"),
            tool_calls=[ToolCall(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"],
            input_tokens=r.usage.input_tokens,
            output_tokens=r.usage.output_tokens,
        )


def to_anthropic(messages):
    out = []
    for m in messages:
        if m["role"] == "user":
            if out and out[-1]["role"] == "user":  # e.g. peer messages right after tool results: one user turn
                prev = out[-1]["content"]
                prev = prev if isinstance(prev, list) else [{"type": "text", "text": prev}]
                out[-1]["content"] = prev + [{"type": "text", "text": m["content"]}]
            else:
                out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["text"]}] if m["text"] else []
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no output)"}]})
        else:  # tool results from one turn go back together in a single user message
            block = {"type": "tool_result", "tool_use_id": m["id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out
