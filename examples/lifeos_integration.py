"""
LifeOS Integration Example
==========================
Shows how a multi-agent application (LifeOS or any other app) integrates
with AgentForge using two patterns:

  1. Embedded SDK  — governance, audit, and HITL run inside your app process
  2. Remote API    — register agents and surface HITL to the AgentForge console

Run embedded workflow (requires: pip install -e .):
    python examples/lifeos_integration.py

Sync agents to a running AgentForge console (stdlib only — no pip install needed):
    python examples/lifeos_integration.py --api-url http://localhost:8000
    python examples/lifeos_integration.py --api-url https://your-app.railway.app

Then open the console → HITL Approvals to approve the pending rent transfer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── LifeOS agent catalogue (used by REST API mode — no SDK import needed) ─────
LIFEOS_AGENTS = [
    {
        "agent_id": "health-agent-001",
        "name": "Health Tracker",
        "version": "2.1.0",
        "description": "Tracks vitals, sleep, and HRV from wearables",
        "owner": "lifeos-core",
        "runtime_adapter": "anthropic",
        "tags": ["health", "wearables"],
    },
    {
        "agent_id": "finance-agent-001",
        "name": "Finance Manager",
        "version": "1.8.0",
        "description": "Categorizes transactions and manages budgets",
        "owner": "lifeos-core",
        "runtime_adapter": "openai",
        "tags": ["finance", "banking"],
    },
    {
        "agent_id": "scheduler-agent-001",
        "name": "Smart Scheduler",
        "version": "0.9.0",
        "description": "Books calendar appointments and manages travel",
        "owner": "lifeos-core",
        "runtime_adapter": "vertex",
        "tags": ["calendar", "scheduling"],
    },
]


def _import_agentforge():
    """Import SDK — only needed for embedded mode."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from agentforge.audit import AuditLogger, AuditTrail, EventSeverity, EventType
        from agentforge.audit.logger import InMemoryAuditSink
        from agentforge.governance import PolicyContext, SOC2Profile
        from agentforge.hitl import ApprovalDecision, EscalationPolicy, HITLOrchestrator
        from agentforge.registry import AgentCapability, AgentManifest, AgentRegistry
    except ModuleNotFoundError as exc:
        print("❌  agentforge is not installed.")
        print("    Embedded SDK mode requires the package. Run once from the repo root:\n")
        print("        pip install -e .\n")
        print("    Or use REST API mode (no install needed):\n")
        print("        python examples/lifeos_integration.py --api-url https://your-app.railway.app\n")
        raise SystemExit(1) from exc
    return {
        "AuditLogger": AuditLogger,
        "AuditTrail": AuditTrail,
        "EventSeverity": EventSeverity,
        "EventType": EventType,
        "InMemoryAuditSink": InMemoryAuditSink,
        "PolicyContext": PolicyContext,
        "SOC2Profile": SOC2Profile,
        "ApprovalDecision": ApprovalDecision,
        "EscalationPolicy": EscalationPolicy,
        "HITLOrchestrator": HITLOrchestrator,
        "AgentCapability": AgentCapability,
        "AgentManifest": AgentManifest,
        "AgentRegistry": AgentRegistry,
    }


# ── Pattern 1: Embedded SDK ─────────────────────────────────────────────────
def build_lifeos_registry(af: dict) -> Any:
    AgentRegistry = af["AgentRegistry"]
    AgentManifest = af["AgentManifest"]
    AgentCapability = af["AgentCapability"]

    registry = AgentRegistry()
    registry.register(AgentManifest(
        agent_id="finance-agent-001",
        name="Finance Manager",
        version="1.8.0",
        description="LifeOS finance agent — spending, budgets, transfers",
        owner="lifeos-core",
        capabilities=[
            AgentCapability("categorize_transaction", "Auto-categorize a bank transaction", tags=["finance"]),
            AgentCapability(
                "transfer_funds",
                "Move money between accounts",
                tags=["finance"],
                requires_hitl=True,
            ),
        ],
        runtime_adapter="openai",
        tags=["finance", "banking"],
    ))
    return registry


async def simulate_reviewer(orchestrator: Any, approve: bool, af: dict) -> None:
    """Simulates a human approving in the AgentForge console."""
    ApprovalDecision = af["ApprovalDecision"]
    await asyncio.sleep(0.15)
    unresolved = [r for r in orchestrator._pending.values() if not r.is_resolved]
    if not unresolved:
        return
    req = unresolved[0]
    await orchestrator.decide(ApprovalDecision(
        request_id=req.request_id,
        reviewer_id="you@lifeos.com",
        approved=approve,
        reason="Verified payee — June rent" if approve else "Amount exceeds daily limit",
    ))


async def lifeos_morning_routine(transfer_amount: float, human_approves: bool = True) -> None:
    """LifeOS morning routine: categorize spending, then HITL-gate rent transfer."""
    af = _import_agentforge()
    EventType = af["EventType"]
    EventSeverity = af["EventSeverity"]

    print(f"\n{'='*60}")
    print(f"  LifeOS Morning Routine  |  rent=${transfer_amount:,.0f}")
    print(f"{'='*60}\n")

    registry = build_lifeos_registry(af)
    agent_id = "finance-agent-001"
    correlation_id = f"lifeos-run-{int(time.time())}"

    sink = af["InMemoryAuditSink"]()
    audit = af["AuditLogger"](sinks=[sink])
    trail = af["AuditTrail"](sink)
    gov = af["SOC2Profile"].engine()

    manifest = registry.get(agent_id)
    print(f"[LifeOS] Agent: {manifest.name} v{manifest.version}")

    audit.log(EventType.AGENT_STARTED, agent_id, correlation_id,
              payload={"trigger": "morning_routine"})

    audit.tool_invoked(agent_id, correlation_id, "categorize_transaction",
                       merchant="Whole Foods", amount=67.43)
    audit.tool_completed(agent_id, correlation_id, "categorize_transaction",
                         category="Groceries", duration_ms=180)
    print("[LifeOS] Categorized transaction: Whole Foods → Groceries")

    decision = gov.evaluate(af["PolicyContext"](
        agent_id=agent_id,
        agent_roles=["operator"],
        action="transfer_funds",
        resource="bank-account",
        environment="production",
        metadata={"amount_usd": transfer_amount},
    ))
    audit.log(EventType.POLICY_EVALUATED, agent_id, correlation_id,
              payload={"effect": decision.effect.value})
    print(f"[Governance] Policy effect: {decision.effect.value}")

    cap = manifest.get_capability("transfer_funds")
    if cap and cap.requires_hitl:
        async def notifier(req, group: str) -> None:
            print(f"[HITL] Notified '{group}' — action '{req.action}' pending approval")

        orchestrator = af["HITLOrchestrator"](
            escalation_policy=af["EscalationPolicy"].fast_track(),
            notify=notifier,
            poll_interval=0.05,
        )

        audit.log(EventType.HITL_REQUESTED, agent_id, correlation_id,
                  payload={"action": "transfer_funds", "amount_usd": transfer_amount},
                  severity=EventSeverity.WARNING)

        approval_task = asyncio.create_task(
            orchestrator.request_approval(
                agent_id=agent_id,
                correlation_id=correlation_id,
                action="transfer_funds",
                resource="bank-account",
                context={
                    "amount_usd": transfer_amount,
                    "payee": "Bay Area Properties LLC",
                    "memo": "June rent",
                },
                timeout_seconds=30,
            )
        )
        asyncio.create_task(simulate_reviewer(orchestrator, human_approves, af))
        hitl = await approval_task

        status = "APPROVED" if hitl.approved else "REJECTED"
        print(f"[HITL] {status} by {hitl.reviewer_id}: {hitl.reason}")

        audit.log(
            EventType.HITL_APPROVED if hitl.approved else EventType.HITL_REJECTED,
            agent_id, correlation_id,
            payload={"reviewer": hitl.reviewer_id, "reason": hitl.reason},
        )

        if not hitl.approved:
            print("\n[LifeOS] Rent transfer blocked. Routine complete.")
            _print_audit_trail(trail, correlation_id)
            return

    audit.tool_invoked(agent_id, correlation_id, "transfer_funds", amount_usd=transfer_amount)
    await asyncio.sleep(0.05)
    audit.tool_completed(agent_id, correlation_id, "transfer_funds", duration_ms=95, status="success")
    print(f"[LifeOS] Transfer of ${transfer_amount:,.0f} initiated.")

    _print_audit_trail(trail, correlation_id)


def _print_audit_trail(trail: Any, correlation_id: str) -> None:
    print(f"\n[Audit] Trail for {correlation_id}:")
    for step in trail.replay(correlation_id):
        print(f"  {step['step']:02d}  {step['type']:<35}  {step['severity']}")


# ── Pattern 2: Remote API ───────────────────────────────────────────────────
def _api_request(base: str, method: str, path: str, body: dict | None = None) -> Any:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        print(f"  ⚠  {method} {path} → {exc.code}: {err[:120]}")
        return None


def sync_lifeos_to_console(api_url: str) -> None:
    """Register LifeOS agents and create a HITL request via the REST API."""
    print(f"\n{'='*60}")
    print(f"  LifeOS → AgentForge API  |  {api_url}")
    print(f"{'='*60}\n")

    health = _api_request(api_url, "GET", "/health")
    if not health:
        print("❌  Cannot reach AgentForge. Start the console or check the URL.")
        sys.exit(1)
    print(f"✓  Connected — AgentForge v{health.get('version', '?')}\n")

    print("── Registering LifeOS agents ─────────────────────────")
    for agent in LIFEOS_AGENTS:
        result = _api_request(api_url, "POST", "/api/agents", agent)
        status = "✓" if result else "⚠ (may already exist)"
        print(f"  {status}  {agent['name']}")

    print("\n── Submitting HITL request (rent transfer) ───────────")
    hitl = _api_request(api_url, "POST", "/api/dev/seed-hitl", {
        "requests": [{
            "agent_id": "finance-agent-001",
            "correlation_id": f"lifeos-api-{int(time.time())}",
            "action": "transfer_funds",
            "resource": "bank-account",
            "context": {
                "amount_usd": 4200,
                "payee": "Bay Area Properties LLC",
                "memo": "June rent",
                "source": "lifeos_integration.py",
            },
        }],
    })
    if hitl:
        print(f"  ✓  {hitl.get('created', 0)} HITL request(s) created")

    print("\n── Console state ─────────────────────────────────────")
    dashboard = _api_request(api_url, "GET", "/api/dashboard")
    if dashboard:
        print(f"  Agents:       {dashboard['agents']['total']}")
        print(f"  HITL pending: {dashboard['hitl_queue_depth']}")
        print(f"  Audit events: {dashboard['audit']['total_events']}")

    print(f"\n🎉  Open {api_url} → log in as admin@agentforge.io / demo1234")
    print("    Go to HITL Approvals to approve the rent transfer.\n")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="LifeOS ↔ AgentForge integration demo")
    parser.add_argument(
        "--api-url",
        help="AgentForge console URL — REST API sync (no pip install needed)",
    )
    parser.add_argument(
        "--reject",
        action="store_true",
        help="Simulate human rejection (embedded SDK mode only)",
    )
    args = parser.parse_args()

    if args.api_url:
        sync_lifeos_to_console(args.api_url)
    else:
        print("\n★  LifeOS × AgentForge — Embedded SDK Demo  ★")
        asyncio.run(lifeos_morning_routine(transfer_amount=4_200, human_approves=True))
        if args.reject:
            asyncio.run(lifeos_morning_routine(transfer_amount=4_200, human_approves=False))


if __name__ == "__main__":
    main()
