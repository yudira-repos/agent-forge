"""Audit event types and severity levels."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    # Identity & auth
    AGENT_REGISTERED = "agent.registered"
    AGENT_DEREGISTERED = "agent.deregistered"
    CREDENTIAL_ISSUED = "credential.issued"
    CREDENTIAL_VERIFIED = "credential.verified"
    CREDENTIAL_REJECTED = "credential.rejected"
    DELEGATION_CREATED = "delegation.created"
    DELEGATION_REVOKED = "delegation.revoked"

    # Runtime lifecycle
    AGENT_STARTED = "agent.started"
    AGENT_PAUSED = "agent.paused"
    AGENT_RESUMED = "agent.resumed"
    AGENT_TERMINATED = "agent.terminated"
    AGENT_FAILED = "agent.failed"

    # Tool / action calls
    TOOL_INVOKED = "tool.invoked"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"

    # Governance
    POLICY_EVALUATED = "policy.evaluated"
    POLICY_DENIED = "policy.denied"
    POLICY_HITL_TRIGGERED = "policy.hitl_triggered"

    # HITL
    HITL_REQUESTED = "hitl.requested"
    HITL_APPROVED = "hitl.approved"
    HITL_REJECTED = "hitl.rejected"
    HITL_TIMEOUT = "hitl.timeout"
    HITL_ESCALATED = "hitl.escalated"

    # Data access
    DATA_READ = "data.read"
    DATA_WRITTEN = "data.written"
    DATA_DELETED = "data.deleted"

    # Custom / extension
    CUSTOM = "custom"


class EventSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class AuditEvent:
    """
    Immutable record of a single auditable occurrence.

    Every event has a globally unique ``event_id`` and a ``correlation_id``
    that links all events belonging to the same agent session or workflow run.

    Example::

        event = AuditEvent.create(
            event_type=EventType.TOOL_INVOKED,
            agent_id="invoice-processor-001",
            correlation_id="run-abc123",
            payload={"tool": "read_pdf", "file": "invoice_2024.pdf"},
        )
    """

    event_id: str
    event_type: EventType
    agent_id: str
    correlation_id: str
    timestamp: float
    severity: EventSeverity
    payload: dict[str, Any]
    metadata: dict[str, Any]
    # Chained hash for tamper-evidence (each event hashes the previous event_id)
    previous_event_id: str | None

    @classmethod
    def create(
        cls,
        event_type: EventType,
        agent_id: str,
        correlation_id: str,
        payload: dict[str, Any] | None = None,
        severity: EventSeverity = EventSeverity.INFO,
        metadata: dict[str, Any] | None = None,
        previous_event_id: str | None = None,
    ) -> "AuditEvent":
        return cls(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            agent_id=agent_id,
            correlation_id=correlation_id,
            timestamp=time.time(),
            severity=severity,
            payload=payload or {},
            metadata=metadata or {},
            previous_event_id=previous_event_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "agent_id": self.agent_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "severity": self.severity.value,
            "payload": self.payload,
            "metadata": self.metadata,
            "previous_event_id": self.previous_event_id,
        }

    def __repr__(self) -> str:
        return (
            f"AuditEvent(type={self.event_type.value!r}, "
            f"agent={self.agent_id!r}, "
            f"ts={self.timestamp:.3f})"
        )
