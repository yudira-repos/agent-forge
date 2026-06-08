"""Abstract agent runtime — lifecycle, context, and result contracts."""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator


class AgentLifecycleState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


@dataclass
class AgentContext:
    """
    Immutable execution context passed to an agent on each invocation.

    Contains the agent's identity, the current correlation ID (for audit
    chaining), a message/prompt, and any tool definitions available.

    Example::

        ctx = AgentContext(
            agent_id="invoice-processor-001",
            correlation_id="run-abc123",
            prompt="Extract line items from the attached invoice.",
            tools=[...],
            metadata={"invoice_path": "/docs/inv_2024.pdf"},
        )
    """

    agent_id: str
    correlation_id: str
    prompt: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 4096
    temperature: float = 0.0
    stream: bool = False

    @classmethod
    def create(
        cls,
        agent_id: str,
        prompt: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> "AgentContext":
        return cls(
            agent_id=agent_id,
            correlation_id=str(uuid.uuid4()),
            prompt=prompt,
            tools=tools or [],
            **kwargs,
        )


@dataclass
class AgentResult:
    """
    The output of a single agent invocation.

    ``content`` holds the final text response.
    ``tool_calls`` contains any tool invocations the model requested.
    ``usage`` captures token consumption for billing/quota tracking.

    Example::

        result = await runtime.invoke(context)
        print(result.content)          # final answer
        print(result.usage)            # {"input_tokens": 512, "output_tokens": 128}
        print(result.stop_reason)      # "end_turn" | "tool_use" | "max_tokens"
    """

    content: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]
    stop_reason: str
    correlation_id: str
    duration_ms: float
    raw_response: Any = None  # underlying SDK response object

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class RuntimeAdapter(ABC):
    """
    Abstract base for all LLM provider adapters.

    Implement ``invoke`` and optionally ``stream`` to add support for
    a new LLM provider.  The adapter receives a normalised ``AgentContext``
    and must return a normalised ``AgentResult``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'anthropic', 'openai', 'vertex'."""
        ...

    @abstractmethod
    async def invoke(self, context: AgentContext) -> AgentResult:
        """Run the agent and return a complete result."""
        ...

    async def stream(self, context: AgentContext) -> AsyncIterator[str]:
        """
        Optionally stream tokens.  Default implementation buffers the
        full response and yields it as a single chunk.
        """
        result = await self.invoke(context)
        yield result.content

    def health_check(self) -> bool:
        """Return True if the adapter can reach its LLM provider."""
        return True


class AgentRuntime:
    """
    Enterprise agent runtime that wraps a provider adapter with
    full lifecycle management, governance hooks, and audit integration.

    Example::

        runtime = AgentRuntime(adapter=AnthropicAdapter(api_key="..."))
        result = await runtime.invoke(AgentContext.create(
            agent_id="my-agent",
            prompt="Summarise the attached report.",
        ))
    """

    def __init__(self, adapter: RuntimeAdapter) -> None:
        self._adapter = adapter
        self._state = AgentLifecycleState.CREATED
        self._hooks: list[Any] = []

    @property
    def state(self) -> AgentLifecycleState:
        return self._state

    @property
    def adapter_name(self) -> str:
        return self._adapter.name

    async def start(self) -> None:
        self._state = AgentLifecycleState.RUNNING

    async def pause(self) -> None:
        self._state = AgentLifecycleState.PAUSED

    async def resume(self) -> None:
        self._state = AgentLifecycleState.RUNNING

    async def terminate(self) -> None:
        self._state = AgentLifecycleState.TERMINATED

    async def invoke(self, context: AgentContext) -> AgentResult:
        """Invoke the agent and return a normalised result."""
        if self._state not in (AgentLifecycleState.RUNNING, AgentLifecycleState.CREATED):
            raise RuntimeError(
                f"Cannot invoke agent in state {self._state.value}. "
                "Call runtime.start() first."
            )
        self._state = AgentLifecycleState.RUNNING
        start = time.time()
        try:
            result = await self._adapter.invoke(context)
            result.duration_ms = (time.time() - start) * 1000
            return result
        except Exception:
            self._state = AgentLifecycleState.FAILED
            raise

    def health_check(self) -> bool:
        return self._adapter.health_check()
