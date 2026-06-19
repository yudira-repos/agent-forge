"""
Shared HITL Registry — cross-product-line human-in-the-loop configuration
==========================================================================

The **single elegant seam** that makes HITL consistent across every product
line.  Each product line registers a ``HITLConfig`` that describes:

  * Which escalation policy to use (speed, tiers, reviewer groups)
  * How to notify reviewers (channel + message format)
  * How to render the domain-specific context in the approval request
  * When to auto-approve vs. auto-reject on timeout

Agent code never hard-codes any of this.  It just calls:

    decision = await registry.request_approval(
        product_line=ProductLine.HEALTH,
        agent_id=...,
        correlation_id=...,
        action=...,
        resource=...,
        context={...},
    )

And the registry routes to the right policy, notifier, and context formatter.

This means:
  - Adding a 4th product line is one ``registry.register(...)`` call.
  - Swapping Slack for PagerDuty in Health is a one-line config change.
  - All HITL telemetry is emitted from this one place.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from agentforge.hitl import (
    ApprovalDecision,
    ApprovalRequest,
    EscalationPolicy,
    HITLOrchestrator,
)
from agentforge.hitl.escalation import EscalationLevel, EscalationTier


# ─────────────────────────────────────────────────────────────────────────────
# Product Line Enum
# ─────────────────────────────────────────────────────────────────────────────

class ProductLine(str, Enum):
    HEALTH = "health"
    INVESTMENT = "investment"
    KIDS = "kids"


# ─────────────────────────────────────────────────────────────────────────────
# Approval Context Renderer
# ─────────────────────────────────────────────────────────────────────────────

# Each product line supplies a renderer that turns raw agent context into a
# human-readable summary surfaced to the reviewer.
ContextRenderer = Callable[[dict[str, Any]], str]


def _health_renderer(ctx: dict[str, Any]) -> str:
    lines = [
        f"Patient : {ctx.get('patient_id', 'N/A')}",
        f"Action  : {ctx.get('clinical_action', 'N/A')}",
        f"Risk    : {ctx.get('risk_level', 'unknown').upper()}",
    ]
    if "medication" in ctx:
        lines.append(f"Med     : {ctx['medication']} {ctx.get('dosage', '')}")
    if "referral_specialty" in ctx:
        lines.append(f"Referral: {ctx['referral_specialty']}")
    if "rationale" in ctx:
        lines.append(f"Rationale: {ctx['rationale']}")
    return "\n".join(lines)


def _investment_renderer(ctx: dict[str, Any]) -> str:
    lines = [
        f"Account    : {ctx.get('account_id', 'N/A')}",
        f"Trade      : {ctx.get('trade_type', 'N/A')} {ctx.get('ticker', '')}",
        f"Amount     : ${ctx.get('amount_usd', 0):,.2f}",
        f"Risk Band  : {ctx.get('risk_band', 'unknown').upper()}",
        f"Portfolio % : {ctx.get('portfolio_pct', 0):.1f}%",
    ]
    if "rationale" in ctx:
        lines.append(f"Rationale: {ctx['rationale']}")
    return "\n".join(lines)


def _kids_renderer(ctx: dict[str, Any]) -> str:
    lines = [
        f"Child      : {ctx.get('child_name', 'N/A')} (age {ctx.get('child_age', '?')})",
        f"Activity   : {ctx.get('activity_name', 'N/A')}",
        f"Provider   : {ctx.get('provider', 'N/A')}",
        f"Date/Time  : {ctx.get('scheduled_time', 'N/A')}",
        f"Cost       : ${ctx.get('cost_usd', 0):.2f}",
    ]
    if "safety_flags" in ctx and ctx["safety_flags"]:
        lines.append(f"⚠ Safety   : {', '.join(ctx['safety_flags'])}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# HITL Config — one per product line
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HITLConfig:
    """
    Domain-specific HITL configuration for one product line.

    Encapsulates everything the shared orchestrator needs to handle
    approvals correctly for this domain — without leaking domain logic
    into the shared layer.
    """

    product_line: ProductLine
    escalation_policy: EscalationPolicy
    context_renderer: ContextRenderer
    notification_channel: str        # e.g. "slack:#clinical-approvals"
    default_timeout_seconds: int = 3600
    auto_approve_on_timeout: bool = False  # HEALTH/INVESTMENT: never auto-approve

    def render_context(self, ctx: dict[str, Any]) -> str:
        return self.context_renderer(ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-built escalation policies — domain-tuned
# ─────────────────────────────────────────────────────────────────────────────

def _health_escalation() -> EscalationPolicy:
    """
    Clinical urgency model: 10 min to attending → 20 min to department head →
    30 min to CMO.  NEVER auto-approve on timeout; block if exhausted.
    """
    return EscalationPolicy(
        name="clinical-review",
        description="Clinical decision escalation — patient safety first",
        levels=[
            EscalationLevel(EscalationTier.L1, "attending-physicians", 600,
                            notify=["#clinical-approvals"]),
            EscalationLevel(EscalationTier.L2, "department-heads", 1200,
                            notify=["#clinical-approvals", "dept-head@hospital.org"]),
            EscalationLevel(EscalationTier.L3, "cmo-office", 1800,
                            notify=["#clinical-approvals", "cmo@hospital.org"]),
        ],
        on_final_timeout="auto_reject",   # block the action; safer than auto-approve
    )


def _investment_escalation() -> EscalationPolicy:
    """
    Compliance-driven: 15 min to senior advisor → 30 min to compliance officer →
    60 min to CIO.  Auto-reject on timeout — never execute an unreviewed trade.
    """
    return EscalationPolicy(
        name="trade-compliance",
        description="Investment trade review — fiduciary duty compliance",
        levels=[
            EscalationLevel(EscalationTier.L1, "senior-advisors", 900,
                            notify=["#trade-approvals"]),
            EscalationLevel(EscalationTier.L2, "compliance-officers", 1800,
                            notify=["#trade-approvals", "compliance@firm.com"]),
            EscalationLevel(EscalationTier.L3, "cio-office", 3600,
                            notify=["#trade-approvals", "cio@firm.com"]),
        ],
        on_final_timeout="auto_reject",
    )


def _kids_escalation() -> EscalationPolicy:
    """
    Parent-first model: immediate notification to parent → 30 min to second
    guardian.  Activity is booked only with explicit parental consent.
    Auto-reject if parent doesn't respond within the window.
    """
    return EscalationPolicy(
        name="parental-consent",
        description="Parental consent required for all activity bookings",
        levels=[
            EscalationLevel(EscalationTier.L1, "primary-parent", 1800,
                            notify=["sms", "app-push"]),
            EscalationLevel(EscalationTier.L2, "secondary-guardian", 3600,
                            notify=["sms", "email"]),
        ],
        on_final_timeout="auto_reject",   # never book without consent
    )


# ─────────────────────────────────────────────────────────────────────────────
# HITL Registry — the central routing table
# ─────────────────────────────────────────────────────────────────────────────

class HITLRegistry:
    """
    Routes approval requests to the correct domain policy.

    One shared ``HITLOrchestrator`` per product line, each wired to its
    own escalation policy, notification channel, and context renderer.

    Usage::

        registry = HITLRegistry.default()

        decision = await registry.request_approval(
            product_line=ProductLine.HEALTH,
            agent_id="medication-agent-001",
            correlation_id=run_id,
            action="prescribe_medication",
            resource="ehr-system",
            context={
                "patient_id": "P-4421",
                "medication": "Amoxicillin",
                "dosage": "500mg",
                "risk_level": "medium",
                "clinical_action": "prescribe_medication",
            },
        )
    """

    def __init__(self) -> None:
        self._configs: dict[ProductLine, HITLConfig] = {}
        self._orchestrators: dict[ProductLine, HITLOrchestrator] = {}
        # Captured notifications per product line (for demo / testing)
        self.notifications: list[dict[str, Any]] = []

    def register(self, config: HITLConfig) -> None:
        """Register a product line HITL config. Safe to call at startup."""
        self._configs[config.product_line] = config

        async def _notifier(request: ApprovalRequest, reviewer_group: str) -> None:
            rendered = config.render_context(request.context)
            note = {
                "product_line": config.product_line.value,
                "channel": config.notification_channel,
                "reviewer_group": reviewer_group,
                "action": request.action,
                "summary": rendered,
                "request_id": request.request_id,
                "escalation_level": request.escalation_level,
                "sent_at": time.time(),
            }
            self.notifications.append(note)
            # In production: replace with real Slack / PagerDuty / SMS call
            print(
                f"\n  📬 [{config.product_line.value.upper()} HITL] "
                f"→ {config.notification_channel} / {reviewer_group}\n"
                f"     Action : {request.action}\n"
                + "\n".join(f"     {line}" for line in rendered.splitlines())
            )

        self._orchestrators[config.product_line] = HITLOrchestrator(
            escalation_policy=config.escalation_policy,
            notify=_notifier,
            poll_interval=0.05,
        )

    async def request_approval(
        self,
        product_line: ProductLine,
        agent_id: str,
        correlation_id: str,
        action: str,
        resource: str,
        context: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> ApprovalDecision:
        """
        Submit an approval request through the correct product-line pipeline.

        The caller never needs to know which escalation policy, notification
        channel, or reviewer group is in play — the registry handles all of it.
        """
        if product_line not in self._configs:
            raise KeyError(f"No HITL config registered for product line '{product_line}'")

        config = self._configs[product_line]
        orchestrator = self._orchestrators[product_line]

        return await orchestrator.request_approval(
            agent_id=agent_id,
            correlation_id=correlation_id,
            action=action,
            resource=resource,
            context=context or {},
            timeout_seconds=timeout_seconds or config.default_timeout_seconds,
        )

    async def resolve(
        self,
        product_line: ProductLine,
        decision: ApprovalDecision,
    ) -> None:
        """Resolve a pending request (called by the reviewer UI or webhook)."""
        orchestrator = self._orchestrators.get(product_line)
        if orchestrator:
            await orchestrator.decide(decision)

    def queue_depth(self, product_line: ProductLine) -> int:
        """Number of pending approvals for a given product line."""
        orch = self._orchestrators.get(product_line)
        return orch.queue_depth() if orch else 0

    def all_pending(self) -> dict[str, list[dict[str, Any]]]:
        """Return all pending requests across product lines (for dashboard)."""
        result: dict[str, list[dict[str, Any]]] = {}
        for pl, orch in self._orchestrators.items():
            pending = orch.pending_requests()
            if pending:
                result[pl.value] = [r.to_dict() for r in pending]
        return result

    @classmethod
    def default(cls) -> "HITLRegistry":
        """
        Factory: returns a registry pre-configured for all three product lines.

        Call this once at application startup and inject the registry into
        every workflow that needs HITL.
        """
        reg = cls()

        reg.register(HITLConfig(
            product_line=ProductLine.HEALTH,
            escalation_policy=_health_escalation(),
            context_renderer=_health_renderer,
            notification_channel="slack:#clinical-approvals",
            default_timeout_seconds=2400,   # 40 min total window
            auto_approve_on_timeout=False,
        ))

        reg.register(HITLConfig(
            product_line=ProductLine.INVESTMENT,
            escalation_policy=_investment_escalation(),
            context_renderer=_investment_renderer,
            notification_channel="slack:#trade-approvals",
            default_timeout_seconds=5400,   # 90 min total window
            auto_approve_on_timeout=False,
        ))

        reg.register(HITLConfig(
            product_line=ProductLine.KIDS,
            escalation_policy=_kids_escalation(),
            context_renderer=_kids_renderer,
            notification_channel="sms+push",
            default_timeout_seconds=5400,   # 90 min parent window
            auto_approve_on_timeout=False,
        ))

        return reg
