"""
Investment Workflow — Portfolio Management & Trade Execution
=============================================================
Product line: INVESTMENT

Agents in this workflow:

  1. PortfolioAnalysisAgent  — read-only analysis of holdings (autonomous)
  2. RiskAssessmentAgent     — evaluates trade risk band (autonomous)
  3. RebalanceAgent          — generates trade recommendations (HITL if
                               trade > $10k OR high/critical risk band)
  4. TradeExecutionAgent     — submits order to broker (HITL always)

Compliance:  SOC 2 profile + custom fiduciary rules
Escalation:  trade-compliance policy (senior-advisor → compliance → CIO)
Channel:     slack:#trade-approvals

HITL contract
─────────────
All HITL calls route through the shared HITLRegistry.  The investment domain
owns its thresholds and reviewer groups; the registry owns delivery + tracking.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agentforge.audit import AuditLogger, AuditTrail, EventSeverity, EventType
from agentforge.audit.logger import InMemoryAuditSink
from agentforge.governance import PolicyContext, PolicyEngine, SOC2Profile
from agentforge.governance.policy import Policy, PolicyEffect, PolicyRule
from agentforge.hitl import ApprovalDecision

from ..shared.hitl_registry import HITLRegistry, ProductLine


# ─────────────────────────────────────────────────────────────────────────────
# Domain models
# ─────────────────────────────────────────────────────────────────────────────

class RiskBand(str, Enum):
    CONSERVATIVE = "conservative"   # ≤ 2% portfolio impact
    MODERATE     = "moderate"       # 2–5% impact
    AGGRESSIVE   = "aggressive"     # 5–10% impact
    CRITICAL     = "critical"       # > 10% impact


class TradeType(str, Enum):
    BUY  = "buy"
    SELL = "sell"


# Dollar threshold above which a trade ALWAYS needs HITL
HITL_TRADE_THRESHOLD_USD = 10_000


@dataclass
class Portfolio:
    account_id: str
    total_value_usd: float
    holdings: dict[str, float]   # ticker → current value in USD


@dataclass
class TradeOrder:
    trade_type: TradeType
    ticker: str
    amount_usd: float
    rationale: str


@dataclass
class TradeDecision:
    order: TradeOrder
    risk_band: RiskBand
    portfolio_pct: float
    requires_hitl: bool
    approved: bool = False
    reviewer_id: str | None = None
    rejection_reason: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# SOC2 + fiduciary governance
# ─────────────────────────────────────────────────────────────────────────────

def _investment_engine() -> PolicyEngine:
    """
    SOC 2 baseline + fiduciary rules.

    Additional rules beyond SOC2:
    - Trades above $10k in production require HITL.
    - Trades in risk band AGGRESSIVE or CRITICAL require HITL.
    - All trade executions must be AUDIT-logged.
    """
    engine = SOC2Profile.engine()

    large_trade = Policy(
        name="fiduciary-large-trade",
        description="Fiduciary: Trades above threshold require human sign-off",
    )
    large_trade.add_rule(PolicyRule(
        name="large-trade-hitl",
        effect=PolicyEffect.REQUIRE_HITL,
        conditions=[
            lambda ctx: ctx.action in ("execute_trade", "rebalance"),
            lambda ctx: ctx.metadata.get("amount_usd", 0) >= HITL_TRADE_THRESHOLD_USD,
        ],
    ))

    high_risk_trade = Policy(
        name="fiduciary-high-risk",
        description="Fiduciary: High-risk trades require human approval",
    )
    high_risk_trade.add_rule(PolicyRule(
        name="high-risk-hitl",
        effect=PolicyEffect.REQUIRE_HITL,
        conditions=[
            lambda ctx: ctx.action in ("execute_trade", "rebalance"),
            lambda ctx: ctx.metadata.get("risk_band") in ("aggressive", "critical"),
        ],
    ))

    engine.add_policy(large_trade)
    engine.add_policy(high_risk_trade)
    return engine


# ─────────────────────────────────────────────────────────────────────────────
# Agents
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioAnalysisAgent:
    """Read-only portfolio analysis — fully autonomous, no HITL."""

    async def analyze(self, portfolio: Portfolio) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        total = portfolio.total_value_usd
        allocation = {
            ticker: round(val / total * 100, 1)
            for ticker, val in portfolio.holdings.items()
        }
        top_position = max(portfolio.holdings, key=portfolio.holdings.get)
        return {
            "account_id": portfolio.account_id,
            "total_value_usd": total,
            "allocation_pct": allocation,
            "top_position": top_position,
            "position_count": len(portfolio.holdings),
            "analysis": "Portfolio is equity-heavy; consider fixed-income rebalance",
        }


class RiskAssessmentAgent:
    """Evaluates the risk band of a proposed trade — autonomous."""

    async def assess(
        self,
        trade: TradeOrder,
        portfolio: Portfolio,
    ) -> tuple[RiskBand, float]:
        await asyncio.sleep(0.02)
        pct = (trade.amount_usd / portfolio.total_value_usd) * 100

        if pct <= 2.0:
            band = RiskBand.CONSERVATIVE
        elif pct <= 5.0:
            band = RiskBand.MODERATE
        elif pct <= 10.0:
            band = RiskBand.AGGRESSIVE
        else:
            band = RiskBand.CRITICAL

        return band, round(pct, 2)


class RebalanceAgent:
    """
    Converts an analysis recommendation into a concrete trade order.

    HITL triggers:
    - Trade amount ≥ $10,000 (HITL_TRADE_THRESHOLD_USD)
    - Risk band is AGGRESSIVE or CRITICAL
    - Governance engine returns REQUIRE_HITL
    """

    async def recommend(
        self,
        portfolio: Portfolio,
        analysis: dict[str, Any],
        trade: TradeOrder,
        hitl_registry: HITLRegistry,
        correlation_id: str,
        logger: AuditLogger,
        gov_engine: PolicyEngine,
    ) -> TradeDecision:
        await asyncio.sleep(0.02)

        risk_agent = RiskAssessmentAgent()
        risk_band, portfolio_pct = await risk_agent.assess(trade, portfolio)

        ctx = PolicyContext(
            agent_id="rebalance-agent",
            agent_roles=["investment-agent"],
            action="rebalance",
            resource="trading-system",
            environment="production",
            metadata={
                "amount_usd": trade.amount_usd,
                "risk_band": risk_band.value,
            },
        )
        gov_decision = gov_engine.evaluate(ctx)

        needs_hitl = (
            gov_decision.requires_hitl
            or trade.amount_usd >= HITL_TRADE_THRESHOLD_USD
            or risk_band in (RiskBand.AGGRESSIVE, RiskBand.CRITICAL)
        )

        logger.log(EventType.POLICY_EVALUATED, "rebalance-agent", correlation_id,
                   payload={"effect": gov_decision.effect.value, "risk_band": risk_band.value})

        if needs_hitl:
            print(
                f"\n  [RebalanceAgent] HITL required — ${trade.amount_usd:,.0f} "
                f"{trade.trade_type.value} {trade.ticker} ({risk_band.value} risk, "
                f"{portfolio_pct:.1f}% of portfolio)"
            )

            hitl_decision = await hitl_registry.request_approval(
                product_line=ProductLine.INVESTMENT,
                agent_id="rebalance-agent",
                correlation_id=correlation_id,
                action="rebalance",
                resource="trading-system",
                context={
                    "account_id": portfolio.account_id,
                    "trade_type": trade.trade_type.value,
                    "ticker": trade.ticker,
                    "amount_usd": trade.amount_usd,
                    "risk_band": risk_band.value,
                    "portfolio_pct": portfolio_pct,
                    "rationale": trade.rationale,
                },
                timeout_seconds=30,
            )
            return TradeDecision(
                order=trade,
                risk_band=risk_band,
                portfolio_pct=portfolio_pct,
                requires_hitl=True,
                approved=hitl_decision.approved,
                reviewer_id=hitl_decision.reviewer_id,
                rejection_reason=hitl_decision.reason if not hitl_decision.approved else None,
            )

        # Small, low-risk trade — auto-approved
        return TradeDecision(
            order=trade,
            risk_band=risk_band,
            portfolio_pct=portfolio_pct,
            requires_hitl=False,
            approved=True,
        )


class TradeExecutionAgent:
    """
    Submits the final order to the broker.  ALWAYS requires HITL — no
    automated trade execution regardless of size.

    This is a hard rule: the governance engine and this agent both enforce it.
    Even if the rebalance was already approved, the execution step gets an
    independent sign-off.  This provides dual-control for compliance.
    """

    async def execute(
        self,
        decision: TradeDecision,
        portfolio: Portfolio,
        hitl_registry: HITLRegistry,
        correlation_id: str,
        logger: AuditLogger,
    ) -> bool:
        if not decision.approved:
            print("\n  [TradeExecutionAgent] Skipped — rebalance not approved")
            return False

        print(
            f"\n  [TradeExecutionAgent] HITL required — dual-control execution gate"
            f"\n    Order: {decision.order.trade_type.value.upper()} "
            f"${decision.order.amount_usd:,.0f} of {decision.order.ticker}"
        )

        hitl_decision = await hitl_registry.request_approval(
            product_line=ProductLine.INVESTMENT,
            agent_id="execution-agent",
            correlation_id=correlation_id,
            action="execute_trade",
            resource="broker-gateway",
            context={
                "account_id": portfolio.account_id,
                "trade_type": decision.order.trade_type.value,
                "ticker": decision.order.ticker,
                "amount_usd": decision.order.amount_usd,
                "risk_band": decision.risk_band.value,
                "portfolio_pct": decision.portfolio_pct,
                "rationale": f"Execution of pre-approved rebalance. {decision.order.rationale}",
            },
            timeout_seconds=30,
        )

        if hitl_decision.approved:
            logger.log(EventType.TOOL_COMPLETED, "execution-agent", correlation_id,
                       payload={"order": decision.order.ticker, "status": "executed"})
            print(f"\n  [TradeExecutionAgent] ✓ Trade executed — approved by {hitl_decision.reviewer_id}")
        else:
            logger.log(EventType.TOOL_COMPLETED, "execution-agent", correlation_id,
                       payload={"order": decision.order.ticker, "status": "blocked"},
                       severity=EventSeverity.WARNING)
            print(f"\n  [TradeExecutionAgent] ✗ Trade blocked — rejected by {hitl_decision.reviewer_id}")

        return hitl_decision.approved


# ─────────────────────────────────────────────────────────────────────────────
# Workflow orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def _simulate_advisor_approval(
    hitl_registry: HITLRegistry,
    approve: bool = True,
    delay: float = 0.15,
) -> None:
    """Simulates a senior advisor resolving all pending/escalated investment approvals."""
    await asyncio.sleep(delay)
    orchestrator = hitl_registry._orchestrators.get(ProductLine.INVESTMENT)
    if not orchestrator:
        return
    # Use _pending directly to catch ESCALATED requests as well.
    for req in list(orchestrator._pending.values()):
        if not req.is_resolved:
            await orchestrator.decide(ApprovalDecision(
                request_id=req.request_id,
                reviewer_id="j.morgan@firm.com",
                approved=approve,
                reason=(
                    "Trade aligns with IPS — approved for execution."
                    if approve else
                    "Exceeds risk tolerance for this account."
                ),
            ))


async def run_investment_workflow(
    portfolio: Portfolio,
    proposed_trade: TradeOrder,
    hitl_registry: HITLRegistry,
    advisor_approves: bool | None = None,  # None = real HITL; True/False = simulate
) -> dict[str, Any]:
    """
    Run a full investment rebalance + execution workflow.

    Both the rebalance recommendation and the final trade execution require
    independent HITL approvals (dual-control model).
    """
    correlation_id = f"inv-run-{uuid.uuid4().hex[:8]}"
    sink = InMemoryAuditSink()
    logger = AuditLogger(sinks=[sink])
    trail = AuditTrail(sink)
    gov_engine = _investment_engine()

    print(f"\n{'═'*60}")
    print(f"  INVESTMENT WORKFLOW  |  Account: {portfolio.account_id}")
    print(f"  Proposed: {proposed_trade.trade_type.value.upper()} ${proposed_trade.amount_usd:,.0f} of {proposed_trade.ticker}")
    print(f"{'═'*60}")

    logger.log(EventType.AGENT_STARTED, "investment-workflow", correlation_id,
               payload={"account": portfolio.account_id})

    # Step 1: Portfolio analysis (autonomous)
    analysis_agent = PortfolioAnalysisAgent()
    analysis = await analysis_agent.analyze(portfolio)
    print(f"\n  [PortfolioAnalysis] Top position: {analysis['top_position']} | "
          f"{analysis['position_count']} holdings | ${analysis['total_value_usd']:,.0f} total")

    # Step 2: Rebalance recommendation (HITL if large/high-risk)
    sim1 = (
        asyncio.create_task(_simulate_advisor_approval(hitl_registry, approve=advisor_approves))
        if advisor_approves is not None else None
    )
    rebalance_agent = RebalanceAgent()
    trade_decision = await rebalance_agent.recommend(
        portfolio, analysis, proposed_trade,
        hitl_registry, correlation_id, logger, gov_engine,
    )
    _print_trade_decision("Rebalance", trade_decision)
    if sim1 is not None:
        await sim1

    if not trade_decision.approved:
        print("\n  [Workflow] Rebalance rejected — workflow complete.")
        return _summary(correlation_id, portfolio, trade_decision, executed=False, trail=trail)

    # Step 3: Trade execution (HITL always — dual control)
    sim2 = (
        asyncio.create_task(_simulate_advisor_approval(hitl_registry, approve=advisor_approves, delay=0.2))
        if advisor_approves is not None else None
    )
    exec_agent = TradeExecutionAgent()
    executed = await exec_agent.execute(
        trade_decision, portfolio, hitl_registry, correlation_id, logger
    )
    if sim2 is not None:
        await sim2

    steps = trail.replay(correlation_id)
    print(f"\n  [Audit] {len(steps)} events recorded for {correlation_id}")

    return _summary(correlation_id, portfolio, trade_decision, executed=executed, trail=trail)


def _print_trade_decision(label: str, d: TradeDecision) -> None:
    status = "✓ APPROVED" if d.approved else "✗ REJECTED"
    hitl_tag = " (HITL)" if d.requires_hitl else " (auto)"
    reviewer = f" by {d.reviewer_id}" if d.reviewer_id else ""
    print(f"\n  [{label}]{hitl_tag} {status}{reviewer}")
    print(f"    Risk: {d.risk_band.value} | Portfolio: {d.portfolio_pct:.1f}%")


def _summary(
    correlation_id: str,
    portfolio: Portfolio,
    decision: TradeDecision,
    executed: bool,
    trail: AuditTrail,
) -> dict[str, Any]:
    steps = trail.replay(correlation_id)
    return {
        "correlation_id": correlation_id,
        "account_id": portfolio.account_id,
        "trade": {
            "type": decision.order.trade_type.value,
            "ticker": decision.order.ticker,
            "amount_usd": decision.order.amount_usd,
        },
        "risk_band": decision.risk_band.value,
        "portfolio_pct": decision.portfolio_pct,
        "rebalance_approved": decision.approved,
        "rebalance_reviewer": decision.reviewer_id,
        "trade_executed": executed,
        "audit_events": len(steps),
    }
