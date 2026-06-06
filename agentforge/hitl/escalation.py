"""Escalation policies — when and how to escalate unresolved approval requests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class EscalationTier(str, Enum):
    L1 = "l1"          # First-line reviewer (team lead)
    L2 = "l2"          # Manager
    L3 = "l3"          # Executive / CISO
    EMERGENCY = "emergency"  # 24/7 on-call


@dataclass
class EscalationLevel:
    tier: EscalationTier
    reviewer_group: str   # e.g. "finance-leads", "security-oncall"
    timeout_seconds: int  # time before escalating to next tier
    notify: list[str] = field(default_factory=list)   # email / Slack handles


@dataclass
class EscalationPolicy:
    """
    Defines how an unresolved approval request escalates through tiers.

    Escalation is time-driven: if the L1 reviewer doesn't act within
    ``timeout_seconds``, the request is bumped to L2, then L3, etc.

    Example::

        policy = EscalationPolicy(
            name="high-value-payments",
            levels=[
                EscalationLevel(EscalationTier.L1, "finance-leads", 900),
                EscalationLevel(EscalationTier.L2, "finance-managers", 1800),
                EscalationLevel(EscalationTier.L3, "cfo-office", 3600),
            ],
        )
    """

    name: str
    levels: list[EscalationLevel] = field(default_factory=list)
    description: str = ""
    on_final_timeout: str = "auto_reject"  # "auto_reject" | "auto_approve" | "block"

    def current_tier(self, request_age_seconds: float) -> EscalationLevel | None:
        """Return the tier that should currently own this request."""
        elapsed = 0.0
        for level in self.levels:
            elapsed += level.timeout_seconds
            if request_age_seconds < elapsed:
                return level
        return None  # exhausted all tiers

    def is_exhausted(self, request_age_seconds: float) -> bool:
        total = sum(l.timeout_seconds for l in self.levels)
        return request_age_seconds >= total

    @classmethod
    def standard(cls) -> "EscalationPolicy":
        """Pre-built standard escalation: 15min → 30min → 60min."""
        return cls(
            name="standard",
            levels=[
                EscalationLevel(EscalationTier.L1, "team-leads", 900),
                EscalationLevel(EscalationTier.L2, "managers", 1800),
                EscalationLevel(EscalationTier.L3, "executives", 3600),
            ],
            on_final_timeout="auto_reject",
        )

    @classmethod
    def fast_track(cls) -> "EscalationPolicy":
        """For time-sensitive operations: 5min → 10min → 20min."""
        return cls(
            name="fast-track",
            levels=[
                EscalationLevel(EscalationTier.L1, "team-leads", 300),
                EscalationLevel(EscalationTier.L2, "managers", 600),
                EscalationLevel(EscalationTier.EMERGENCY, "on-call", 1200),
            ],
            on_final_timeout="auto_reject",
        )
