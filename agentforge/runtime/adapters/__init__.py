"""Provider adapters — Anthropic, OpenAI, LangChain, Vertex."""

from .anthropic import AnthropicAdapter
from .langchain import LangChainAdapter
from .openai import OpenAIAdapter
from .vertex import VertexAdapter

_ADAPTER_MAP = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "langchain": LangChainAdapter,
    "vertex": VertexAdapter,
}


def get_adapter(name: str, **kwargs):
    """
    Factory: instantiate an adapter by name.

    Example::

        adapter = get_adapter("anthropic", api_key="sk-ant-...")
        adapter = get_adapter("openai", api_key="sk-...")
        adapter = get_adapter("vertex", project="my-gcp-project", location="us-central1")
    """
    cls = _ADAPTER_MAP.get(name.lower())
    if cls is None:
        raise ValueError(
            f"Unknown adapter '{name}'. "
            f"Available: {list(_ADAPTER_MAP.keys())}"
        )
    return cls(**kwargs)


__all__ = [
    "AnthropicAdapter",
    "OpenAIAdapter",
    "LangChainAdapter",
    "VertexAdapter",
    "get_adapter",
]
