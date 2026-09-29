"""AuditTrail — query and replay audit events from an InMemoryAuditSink."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import AuditEvent, EventSeverity, EventType
from .logger import InMemoryAuditSink


@dataclass
class AuditQuery:
    """
    Filter specification for querying an AuditTrail.

    Example::

        query = AuditQuery(
            agent_id="invoice-processor-001",
            event_types=[EventType.TOOL_INVOKED, EventType.POLICY_DENIED],
            min_severity=EventSeverity.WARNING,
        )
        results = trail.query(query)
    """

    agent_id: str | None = None
    correlation_id: str | None = None
    event_types: list[EventType] = field(default_factory=list)
    min_severity: EventSeverity = EventSeverity.DEBUG
    since: float | None = None  # Unix timestamp
    until: float | None = None  # Unix timestamp
    limit: int = 1000


@dataclass
class ChainVerification:
    """Result of verifying a correlation chain's integrity."""

    valid: bool
    checked: int
    broken_at: int | None = None  # index of the first bad event, if any
    reason: str | None = None


class AuditTrail:
    """
    Queryable, replayable view over captured audit events.

    Wraps an ``InMemoryAuditSink`` and provides filtering, timeline
    reconstruction, and per-correlation-id replay.

    Example::

        sink = InMemoryAuditSink()
        logger = AuditLogger(sinks=[sink])
        trail = AuditTrail(sink)

        # ... run agents ...

        events = trail.query(AuditQuery(agent_id="agent-001"))
        timeline = trail.timeline("run-123")
    """

    def __init__(self, sink: InMemoryAuditSink) -> None:
        self._sink = sink

    def query(self, q: AuditQuery) -> list[AuditEvent]:
        severity_order = list(EventSeverity)
        min_idx = severity_order.index(q.min_severity)

        results = []
        for event in self._sink.events:
            if q.agent_id and event.agent_id != q.agent_id:
                continue
            if q.correlation_id and event.correlation_id != q.correlation_id:
                continue
            if q.event_types and event.event_type not in q.event_types:
                continue
            if severity_order.index(event.severity) < min_idx:
                continue
            if q.since and event.timestamp < q.since:
                continue
            if q.until and event.timestamp > q.until:
                continue
            results.append(event)

        return results[: q.limit]

    def timeline(self, correlation_id: str) -> list[AuditEvent]:
        """Return all events for a correlation_id in chronological order."""
        events = [e for e in self._sink.events if e.correlation_id == correlation_id]
        return sorted(events, key=lambda e: e.timestamp)

    def replay(self, correlation_id: str) -> list[dict[str, Any]]:
        """
        Reconstruct the full decision trail for a workflow run.

        Returns a list of dicts suitable for rendering as a step-by-step
        narrative of what happened during the run.
        """
        events = self.timeline(correlation_id)
        steps = []
        for i, event in enumerate(events):
            steps.append(
                {
                    "step": i + 1,
                    "event_id": event.event_id,
                    "type": event.event_type.value,
                    "agent": event.agent_id,
                    "timestamp": event.timestamp,
                    "severity": event.severity.value,
                    "payload": event.payload,
                    "links_to": event.previous_event_id,
                }
            )
        return steps

    def verify_chain(self, correlation_id: str, hmac_key: bytes | None = None) -> ChainVerification:
        """
        Verify the hash chain for one correlation id, in write order.

        Detects edited events (hash mismatch), deleted or reordered events
        (link mismatch), and a chain that does not start at a root event.
        Use the same ``hmac_key`` the logger was created with.
        """
        events = [e for e in self._sink.events if e.correlation_id == correlation_id]
        prev: AuditEvent | None = None
        for i, event in enumerate(events):
            if event.compute_hash(hmac_key) != event.event_hash:
                return ChainVerification(False, i + 1, i, "event content does not match its hash")
            expected_prev_hash = prev.event_hash if prev else None
            expected_prev_id = prev.event_id if prev else None
            if (
                event.previous_hash != expected_prev_hash
                or event.previous_event_id != expected_prev_id
            ):
                return ChainVerification(
                    False, i + 1, i, "link to previous event is broken (missing or reordered)"
                )
            prev = event
        return ChainVerification(True, len(events))

    def violations(self, since: float | None = None) -> list[AuditEvent]:
        """Return all DENY and HITL events (for compliance reporting)."""
        return self.query(
            AuditQuery(
                event_types=[
                    EventType.POLICY_DENIED,
                    EventType.HITL_REQUESTED,
                    EventType.HITL_ESCALATED,
                    EventType.CREDENTIAL_REJECTED,
                ],
                min_severity=EventSeverity.WARNING,
                since=since,
                limit=10_000,
            )
        )

    def stats(self) -> dict[str, Any]:
        events = self._sink.events
        by_type: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        for e in events:
            by_type[e.event_type.value] = by_type.get(e.event_type.value, 0) + 1
            by_agent[e.agent_id] = by_agent.get(e.agent_id, 0) + 1
        return {
            "total_events": len(events),
            "by_type": by_type,
            "by_agent": by_agent,
            "violations": len(self.violations()),
        }
