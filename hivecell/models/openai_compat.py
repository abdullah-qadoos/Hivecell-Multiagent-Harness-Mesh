"""One adapter for every provider that speaks the OpenAI chat-completions format."""
import json
import os

from .base import Reply, ToolCall

# provider -> (base_url, api key env var)
PROVIDERS = {
    "openai": (None, "OPENAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "grok": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "opencode": ("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}


class OpenAICompatModel:
    def __init__(self, provider: str, model: str):
        from openai import OpenAI

        base_url, key_env = PROVIDERS[provider]
        key = os.environ.get(key_env) if key_env else "ollama"
        if not key:
            raise RuntimeError(f"set {key_env} to use the {provider} provider")
        self.client = OpenAI(base_url=base_url, api_key=key)
        self.model = model

    def complete(self, system, messages, tools):
        r = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}] + to_openai(messages),
            tools=[{"type": "function", "function": t} for t in tools],
        )
        msg = r.choices[0].message
        return Reply(
            text=msg.content or "",
            tool_calls=[ToolCall(c.id, c.function.name, _args(c.function.arguments)) for c in msg.tool_calls or []],
            input_tokens=r.usage.prompt_tokens if r.usage else 0,
            output_tokens=r.usage.completion_tokens if r.usage else 0,
        )


def _args(raw: str) -> dict:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        args = None
    # bad JSON reaches the tool router as an unknown arg, so the model sees an error and can retry
    return args if isinstance(args, dict) else {"_invalid_arguments": raw}


def to_openai(messages):
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg = {"role": "assistant", "content": m["text"] or None}
            if m["tool_calls"]:
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                    for c in m["tool_calls"]
                ]
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
    return out
