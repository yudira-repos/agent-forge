"""Audit logger with pluggable sinks (console, file, custom)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .events import AuditEvent, EventSeverity, EventType


class AuditSink(ABC):
    """Interface for audit log destinations."""

    @abstractmethod
    def write(self, event: AuditEvent) -> None: ...

    def flush(self) -> None:
        pass


class ConsoleAuditSink(AuditSink):
    """Write events to stdout as formatted JSON lines."""

    def __init__(self, min_severity: EventSeverity = EventSeverity.INFO) -> None:
        self._min = list(EventSeverity).index(min_severity)

    def write(self, event: AuditEvent) -> None:
        if list(EventSeverity).index(event.severity) >= self._min:
            print(json.dumps(event.to_dict()), flush=True)


class FileAuditSink(AuditSink):
    """
    Append events to a newline-delimited JSON log file.

    Example::

        sink = FileAuditSink("./audit.log")
        logger = AuditLogger(sinks=[sink])
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8")

    def write(self, event: AuditEvent) -> None:
        self._file.write(json.dumps(event.to_dict()) + "\n")

    def flush(self) -> None:
        self._file.flush()

    def __del__(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass


class InMemoryAuditSink(AuditSink):
    """Captures events in memory — useful for testing and AuditTrail queries."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class AuditLogger:
    """
    Central audit logger for AgentForge.

    Emits structured AuditEvents to one or more sinks.  Maintains a
    per-correlation chain so each event links to the previous one —
    creating a tamper-evident linked list of events per workflow run.

    Example::

        logger = AuditLogger()  # defaults to ConsoleAuditSink
        logger.log(
            EventType.TOOL_INVOKED,
            agent_id="agent-001",
            correlation_id="run-123",
            payload={"tool": "send_email"},
        )
    """

    def __init__(self, sinks: list[AuditSink] | None = None) -> None:
        self._sinks = sinks or [ConsoleAuditSink()]
        # Track last event_id per correlation_id for chaining
        self._chain_heads: dict[str, str] = {}

    def add_sink(self, sink: AuditSink) -> None:
        self._sinks.append(sink)

    def log(
        self,
        event_type: EventType,
        agent_id: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
        severity: EventSeverity = EventSeverity.INFO,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Emit a single audit event to all registered sinks."""
        previous = self._chain_heads.get(correlation_id)
        event = AuditEvent.create(
            event_type=event_type,
            agent_id=agent_id,
            correlation_id=correlation_id,
            payload=payload,
            severity=severity,
            metadata=metadata,
            previous_event_id=previous,
        )
        self._chain_heads[correlation_id] = event.event_id
        for sink in self._sinks:
            sink.write(event)
        return event

    # Convenience methods for common event types
    def tool_invoked(self, agent_id: str, correlation_id: str, tool: str, **kwargs: Any) -> AuditEvent:
        return self.log(
            EventType.TOOL_INVOKED, agent_id, correlation_id,
            payload={"tool": tool, **kwargs},
        )

    def tool_completed(self, agent_id: str, correlation_id: str, tool: str, duration_ms: float, **kwargs: Any) -> AuditEvent:
        return self.log(
            EventType.TOOL_COMPLETED, agent_id, correlation_id,
            payload={"tool": tool, "duration_ms": duration_ms, **kwargs},
        )

    def policy_denied(self, agent_id: str, correlation_id: str, rules: list[str], context: dict[str, Any]) -> AuditEvent:
        return self.log(
            EventType.POLICY_DENIED, agent_id, correlation_id,
            payload={"denied_by": rules, "context": context},
            severity=EventSeverity.WARNING,
        )

    def hitl_requested(self, agent_id: str, correlation_id: str, request_id: str, reason: str) -> AuditEvent:
        return self.log(
            EventType.HITL_REQUESTED, agent_id, correlation_id,
            payload={"request_id": request_id, "reason": reason},
            severity=EventSeverity.WARNING,
        )

    def flush(self) -> None:
        for sink in self._sinks:
            sink.flush()
