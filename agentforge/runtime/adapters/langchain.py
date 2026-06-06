"""LangChain / LangGraph adapter."""

from __future__ import annotations

from typing import Any

from ..base import AgentContext, AgentResult, RuntimeAdapter


class LangChainAdapter(RuntimeAdapter):
    """
    Adapter for LangChain (and LangGraph) runtimes.

    Accepts any ``BaseChatModel`` from LangChain and wraps it in the
    AgentForge ``RuntimeAdapter`` interface.

    Example::

        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model="claude-opus-4-5", api_key="...")

        adapter = LangChainAdapter(llm=llm)
        runtime = AgentRuntime(adapter=adapter)
    """

    name = "langchain"

    def __init__(self, llm: Any | None = None, **llm_kwargs: Any) -> None:
        """
        Parameters
        ----------
        llm:
            A LangChain ``BaseChatModel`` instance.  If None, you must
            pass valid ``llm_kwargs`` and the adapter will construct a
            ``ChatOpenAI`` as the default.
        """
        self._llm = llm
        self._llm_kwargs = llm_kwargs

    def _get_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        try:
            from langchain_openai import ChatOpenAI  # type: ignore
            self._llm = ChatOpenAI(**self._llm_kwargs)
        except ImportError as exc:
            raise ImportError(
                "langchain-openai is required when no llm is provided to LangChainAdapter. "
                "Install it with: pip install langchain-openai"
            ) from exc
        return self._llm

    async def invoke(self, context: AgentContext) -> AgentResult:
        try:
            from langchain_core.messages import HumanMessage, AIMessage  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "langchain-core is required for LangChainAdapter. "
                "Install it with: pip install langchain-core"
            ) from exc

        llm = self._get_llm()
        messages = [HumanMessage(content=context.prompt)]

        response = await llm.ainvoke(messages)

        content = response.content if isinstance(response.content, str) else str(response.content)
        usage = {}
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            usage = {
                "input_tokens": response.usage_metadata.get("input_tokens", 0),
                "output_tokens": response.usage_metadata.get("output_tokens", 0),
            }

        return AgentResult(
            content=content,
            tool_calls=[],
            usage=usage,
            stop_reason="stop",
            correlation_id=context.correlation_id,
            duration_ms=0.0,
            raw_response=response,
        )

    def health_check(self) -> bool:
        try:
            import langchain_core  # type: ignore
            return True
        except ImportError:
            return False
