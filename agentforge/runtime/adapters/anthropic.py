"""Anthropic Claude adapter (claude-3-5-sonnet, claude-opus-4, etc.)."""

from __future__ import annotations

from typing import Any

from ..base import AgentContext, AgentResult, RuntimeAdapter


class AnthropicAdapter(RuntimeAdapter):
    """
    Adapter for Anthropic's Claude models via the ``anthropic`` Python SDK.

    Supports tool use, streaming, and the full Claude model family.

    Example::

        adapter = AnthropicAdapter(
            api_key="sk-ant-...",
            model="claude-opus-4-5",
            default_max_tokens=4096,
        )
        runtime = AgentRuntime(adapter=adapter)
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-opus-4-5",
        default_max_tokens: int = 4096,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._default_max_tokens = default_max_tokens
        self._base_url = base_url
        self._client: Any = None  # lazy-init on first call

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic  # type: ignore

                kwargs: dict[str, Any] = {}
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                if self._base_url:
                    kwargs["base_url"] = self._base_url
                self._client = anthropic.AsyncAnthropic(**kwargs)
            except ImportError as exc:
                raise ImportError(
                    "The 'anthropic' package is required for AnthropicAdapter. "
                    "Install it with: pip install anthropic"
                ) from exc
        return self._client

    async def invoke(self, context: AgentContext) -> AgentResult:
        client = self._get_client()

        messages = list(context.history) + [
            {"role": "user", "content": context.prompt}
        ]

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": context.max_tokens or self._default_max_tokens,
            "messages": messages,
        }
        if context.tools:
            kwargs["tools"] = context.tools
        if context.temperature != 0.0:
            kwargs["temperature"] = context.temperature

        response = await client.messages.create(**kwargs)

        # Normalise tool calls
        tool_calls = []
        content_text = ""
        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        return AgentResult(
            content=content_text,
            tool_calls=tool_calls,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            stop_reason=response.stop_reason or "end_turn",
            correlation_id=context.correlation_id,
            duration_ms=0.0,  # filled by AgentRuntime
            raw_response=response,
        )

    def health_check(self) -> bool:
        try:
            import anthropic  # type: ignore  # noqa: F401
            return True
        except ImportError:
            return False
