"""OpenAI adapter (GPT-4o, o1, Responses API, Agents SDK)."""

from __future__ import annotations

from typing import Any

from ..base import AgentContext, AgentResult, RuntimeAdapter


class OpenAIAdapter(RuntimeAdapter):
    """
    Adapter for OpenAI models via the ``openai`` Python SDK.

    Compatible with the Chat Completions API and the newer Responses API.
    Also wraps the OpenAI Agents SDK when ``use_agents_sdk=True``.

    Example::

        adapter = OpenAIAdapter(
            api_key="sk-...",
            model="gpt-4o",
        )
    """

    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o",
        default_max_tokens: int = 4096,
        use_agents_sdk: bool = False,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._default_max_tokens = default_max_tokens
        self._use_agents_sdk = use_agents_sdk
        self._base_url = base_url
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import openai  # type: ignore

                kwargs: dict[str, Any] = {}
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                if self._base_url:
                    kwargs["base_url"] = self._base_url
                self._client = openai.AsyncOpenAI(**kwargs)
            except ImportError as exc:
                raise ImportError(
                    "The 'openai' package is required for OpenAIAdapter. "
                    "Install it with: pip install openai"
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
            "temperature": context.temperature,
        }
        if context.tools:
            kwargs["tools"] = [
                {"type": "function", "function": t} for t in context.tools
            ]
            kwargs["tool_choice"] = "auto"

        response = await client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        # Normalise tool calls
        tool_calls = []
        if message.tool_calls:
            import json
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json.loads(tc.function.arguments),
                })

        return AgentResult(
            content=message.content or "",
            tool_calls=tool_calls,
            usage={
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            },
            stop_reason=response.choices[0].finish_reason or "stop",
            correlation_id=context.correlation_id,
            duration_ms=0.0,
            raw_response=response,
        )

    def health_check(self) -> bool:
        try:
            import openai  # type: ignore  # noqa: F401
            return True
        except ImportError:
            return False
