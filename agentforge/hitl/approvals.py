"""Approval request models and status lifecycle."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


@dataclass
class ApprovalRequest:
    """
    A request for human review and approval of an agent action.

    When a governance policy returns REQUIRE_HITL, the runtime creates an
    ``ApprovalRequest`` and suspends the agent until a human resolves it.

    Example::

        request = ApprovalRequest.create(
            agent_id="payment-agent-001",
            correlation_id="run-abc",
            action="initiate_wire_transfer",
            resource="payment-gateway",
            context={"amount_usd": 75000, "recipient": "Vendor Corp"},
            timeout_seconds=3600,
        )
    """

    request_id: str
    agent_id: str
    correlation_id: str
    action: str
    resource: str
    context: dict[str, Any]
    created_at: float
    expires_at: float
    status: ApprovalStatus
    reviewer_id: str | None
    resolved_at: float | None
    reason: str | None  # reviewer's note
    escalation_level: int  # 0 = first-line, 1 = manager, 2 = exec

    @classmethod
    def create(
        cls,
        agent_id: str,
        correlation_id: str,
        action: str,
        resource: str,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 3600,
    ) -> "ApprovalRequest":
        now = time.time()
        return cls(
            request_id=str(uuid.uuid4()),
            agent_id=agent_id,
            correlation_id=correlation_id,
            action=action,
            resource=resource,
            context=context or {},
            created_at=now,
            expires_at=now + timeout_seconds,
            status=ApprovalStatus.PENDING,
            reviewer_id=None,
            resolved_at=None,
            reason=None,
            escalation_level=0,
        )

    @property
    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING

    @property
    def is_resolved(self) -> bool:
        return self.status in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.TIMEOUT,
            ApprovalStatus.CANCELLED,
        )

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at and self.is_pending

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "correlation_id": self.correlation_id,
            "action": self.action,
            "resource": self.resource,
            "context": self.context,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "reviewer_id": self.reviewer_id,
            "resolved_at": self.resolved_at,
            "reason": self.reason,
            "escalation_level": self.escalation_level,
        }


@dataclass
class ApprovalDecision:
    """The resolution of an ApprovalRequest by a human reviewer."""

    request_id: str
    reviewer_id: str
    approved: bool
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    decided_at: float = field(default_factory=time.time)
