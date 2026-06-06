"""
Enterprise Workflow Example
============================
Demonstrates the full AgentForge stack:
  1. Register agents in the registry
  2. Issue identities and credentials (AIAM)
  3. Evaluate governance policies
  4. Gate a high-value action behind HITL
  5. Record a tamper-evident audit trail
  6. Replay the full decision trail

Run with:
    python examples/enterprise_workflow.py
"""

import asyncio
import json
import time

from agentforge.aiam import (
    AgentAuthority,
    AgentCredential,
    AgentIdentity,
    AuthorityScope,
    RBACPolicy,
)
from agentforge.audit import (
    AuditLogger,
    AuditQuery,
    AuditTrail,
    EventSeverity,
    EventType,
)
from agentforge.audit.logger import InMemoryAuditSink
from agentforge.governance import PolicyContext, PolicyEffect, SOC2Profile
from agentforge.hitl import (
    ApprovalDecision,
    EscalationPolicy,
    HITLOrchestrator,
)
from agentforge.registry import AgentCapability, AgentManifest, AgentRegistry


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

def build_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(AgentManifest(
        agent_id="payment-agent-001",
        name="Payment Processing Agent",
        version="2.1.0",
        description="Initiates and validates B2B wire transfers",
        owner="finance-team",
        capabilities=[
            AgentCapability(
                "validate_invoice",
                "Validate invoice data against AP records",
                tags=["finance", "validation"],
            ),
            AgentCapability(
                "initiate_transfer",
                "Initiate wire transfer via payment gateway",
                tags=["finance", "payment"],
                requires_hitl=True,   # <- always needs human sign-off
            ),
        ],
        runtime_adapter="anthropic",
        tags=["finance", "payments"],
        required_roles=["operator"],
    ))
    return registry


def build_identity() -> AgentIdentity:
    return AgentIdentity.create(
        name="payment-agent-001",
        roles=["operator"],
        owner="finance-team",
        metadata={"env": "production", "region": "us-east-1"},
    )


async def simulate_human_approval(
    orchestrator: HITLOrchestrator,
    approve: bool = True,
) -> None:
    """Simulates a human reviewer responding in the background."""
    await asyncio.sleep(0.1)
    pending = orchestrator.pending_requests()
    if not pending:
        return
    req = pending[0]
    await orchestrator.decide(ApprovalDecision(
        request_id=req.request_id,
        reviewer_id="sarah.finance@acme.com",
        approved=approve,
        reason="Verified with finance controller — approved." if approve else "Exceeds budget.",
    ))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WORKFLOW
# ─────────────────────────────────────────────────────────────────────────────

async def run_payment_workflow(transfer_amount: float, human_approves: bool = True) -> None:
    print(f"\n{'='*60}")
    print(f"  Payment Workflow  |  amount=${transfer_amount:,.0f}  |  approve={human_approves}")
    print(f"{'='*60}\n")

    # 1. Registry — discover the payment agent
    registry = build_registry()
    manifest = registry.get("payment-agent-001")
    print(f"[Registry] Found: {manifest.name} v{manifest.version}")

    # 2. AIAM — establish identity and issue credentials
    identity = build_identity()
    SECRET = b"acme-corp-signing-key-2024"
    credential = AgentCredential.issue(
        identity, scopes=["payments:initiate", "invoices:read"], ttl=300, secret=SECRET
    )
    print(f"[AIAM] Identity: {identity.agent_id}")
    print(f"[AIAM] Credential valid: {credential.verify(SECRET)}")

    # RBAC check
    rbac = RBACPolicy.enterprise_baseline()
    from agentforge.aiam import Permission
    can_invoke = rbac.agent_has_permission(identity.roles, Permission("agents", "invoke"))
    print(f"[RBAC]  Can invoke agents: {can_invoke}")

    # 3. Audit setup
    sink = InMemoryAuditSink()
    logger = AuditLogger(sinks=[sink])
    trail = AuditTrail(sink)
    correlation_id = f"payment-run-{int(time.time())}"

    logger.log(EventType.AGENT_STARTED, identity.agent_id, correlation_id,
               payload={"manifest": manifest.name})

    # 4. Governance — evaluate SOC 2 policy
    gov_engine = SOC2Profile.engine()
    context = PolicyContext(
        agent_id=identity.agent_id,
        agent_roles=identity.roles,
        action="initiate",
        resource="payment-gateway",
        environment="production",
        metadata={"amount_usd": transfer_amount},
    )
    decision = gov_engine.evaluate(context)
    print(f"\n[Governance] Effect: {decision.effect.value.upper()}")

    logger.log(EventType.POLICY_EVALUATED, identity.agent_id, correlation_id,
               payload={"effect": decision.effect.value, "rules": decision.matched_rules})

    # 5. HITL — if the capability requires human approval
    cap = manifest.get_capability("initiate_transfer")
    if cap and cap.requires_hitl:
        print("[HITL] Capability requires human approval — submitting request…")

        notifications: list[tuple] = []

        async def notifier(req, group):
            notifications.append((req.action, group))
            print(f"[HITL] → Notified reviewer group '{group}' about '{req.action}'")

        orchestrator = HITLOrchestrator(
            escalation_policy=EscalationPolicy.fast_track(),
            notify=notifier,
            poll_interval=0.05,
        )

        logger.hitl_requested(
            identity.agent_id, correlation_id,
            request_id="TBD", reason="Capability requires_hitl=True",
        )

        # Start the approval request + simulate human response concurrently
        approval_task = asyncio.create_task(
            orchestrator.request_approval(
                agent_id=identity.agent_id,
                correlation_id=correlation_id,
                action="initiate_transfer",
                resource="payment-gateway",
                context={"amount_usd": transfer_amount, "recipient": "Vendor Corp Inc."},
                timeout_seconds=30,
            )
        )
        # Simulate human reviewing
        asyncio.create_task(simulate_human_approval(orchestrator, approve=human_approves))

        hitl_decision = await approval_task
        status = "APPROVED ✓" if hitl_decision.approved else "REJECTED ✗"
        print(f"[HITL] Decision: {status}  —  '{hitl_decision.reason}'")
        print(f"[HITL] Reviewer: {hitl_decision.reviewer_id}")

        logger.log(
            EventType.HITL_APPROVED if hitl_decision.approved else EventType.HITL_REJECTED,
            identity.agent_id, correlation_id,
            payload={"reviewer": hitl_decision.reviewer_id, "reason": hitl_decision.reason},
            severity=EventSeverity.INFO if hitl_decision.approved else EventSeverity.WARNING,
        )

        if not hitl_decision.approved:
            print("\n[Workflow] Transfer blocked. Workflow complete.")
            return

    # 6. Execute (simulated)
    logger.tool_invoked(identity.agent_id, correlation_id, "initiate_transfer",
                        amount_usd=transfer_amount)
    print(f"\n[Runtime] Invoking 'initiate_transfer' for ${transfer_amount:,.0f}…")
    await asyncio.sleep(0.05)  # simulate LLM call
    logger.tool_completed(identity.agent_id, correlation_id, "initiate_transfer",
                          duration_ms=52.4, status="success")
    print("[Runtime] Transfer initiated successfully.")

    # 7. Audit trail replay
    print(f"\n[Audit] Decision trail for correlation_id={correlation_id}:")
    steps = trail.replay(correlation_id)
    for step in steps:
        print(f"  Step {step['step']:02d}  {step['type']:<35}  {step['severity']}")

    stats = trail.stats()
    print(f"\n[Audit] Total events: {stats['total_events']}  |  Violations: {stats['violations']}")


if __name__ == "__main__":
    print("\n★  AgentForge Enterprise Workflow Demo  ★")
    # Scenario 1: large transfer → approved by human
    asyncio.run(run_payment_workflow(transfer_amount=75_000, human_approves=True))
    # Scenario 2: large transfer → rejected by human
    asyncio.run(run_payment_workflow(transfer_amount=120_000, human_approves=False))
