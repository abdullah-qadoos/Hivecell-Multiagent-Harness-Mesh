from .base import Model, Reply, ToolCall
from .openai_compat import PROVIDERS


def make_model(spec: str) -> Model:
    """'anthropic/claude-sonnet-5-5', 'gemini/...', 'grok/...', 'opencode/...', 'ollama/qwen2.5-coder:7b'."""
    provider, _, model = spec.partition("/")
    if not model:
        raise ValueError(f"model must look like 'provider/model-id', got {spec!r}")
    if provider in ("anthropic", "claude"):
        from .anthropic_model import AnthropicModel

        return AnthropicModel(model)
    if provider in PROVIDERS:
        from .openai_compat import OpenAICompatModel

        return OpenAICompatModel(provider, model)
    raise ValueError(f"unknown provider {provider!r}; use anthropic or one of {sorted(PROVIDERS)}")


__all__ = ["Model", "Reply", "ToolCall", "make_model"]
