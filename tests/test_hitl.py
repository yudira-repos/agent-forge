"""Tests for HITL orchestration."""

import asyncio

import pytest

from agentforge.hitl import (
    ApprovalDecision,
    ApprovalRequest,
    EscalationPolicy,
    EscalationTier,
    HITLOrchestrator,
)
from agentforge.hitl.escalation import EscalationLevel


class TestApprovalRequest:
    def test_create_is_pending(self):
        req = ApprovalRequest.create("agent-1", "run-1", "delete", "invoices")
        assert req.is_pending
        assert not req.is_resolved

    def test_roundtrip_dict(self):
        req = ApprovalRequest.create("agent-1", "run-1", "delete", "invoices")
        d = req.to_dict()
        assert d["action"] == "delete"
        assert d["status"] == "pending"


class TestEscalationPolicy:
    def test_standard_tiers(self):
        policy = EscalationPolicy.standard()
        # 0 seconds → L1
        tier = policy.current_tier(0)
        assert tier.tier == EscalationTier.L1

        # Just past L1 timeout (900s) → L2
        tier = policy.current_tier(901)
        assert tier.tier == EscalationTier.L2

        # Past all tiers → None
        assert policy.current_tier(99999) is None
        assert policy.is_exhausted(99999)

    def test_fast_track(self):
        policy = EscalationPolicy.fast_track()
        assert policy.levels[0].timeout_seconds == 300


class TestHITLOrchestrator:
    @pytest.mark.asyncio
    async def test_approve_resolves_future(self):
        notifications = []

        async def notifier(req, group):
            notifications.append((req.request_id, group))

        orchestrator = HITLOrchestrator(notify=notifier)

        # Start the approval request in the background
        task = asyncio.create_task(
            orchestrator.request_approval(
                agent_id="agent-1",
                correlation_id="run-1",
                action="wire_transfer",
                resource="payments",
                context={"amount_usd": 50_000},
                timeout_seconds=10,
            )
        )

        # Give the event loop a tick so the task starts and notification fires
        await asyncio.sleep(0)

        assert len(notifications) == 1

        # Human makes a decision
        pending = orchestrator.pending_requests()
        assert len(pending) == 1
        req = pending[0]

        await orchestrator.decide(
            ApprovalDecision(
                request_id=req.request_id,
                reviewer_id="human-reviewer",
                approved=True,
                reason="Looks good",
            )
        )

        decision = await asyncio.wait_for(task, timeout=2.0)
        assert decision.approved
        assert decision.reviewer_id == "human-reviewer"

    @pytest.mark.asyncio
    async def test_reject_resolves_future(self):
        orchestrator = HITLOrchestrator()
        task = asyncio.create_task(
            orchestrator.request_approval("agent-1", "run-2", "delete", "users", timeout_seconds=10)
        )
        await asyncio.sleep(0)
        pending = orchestrator.pending_requests()
        await orchestrator.decide(
            ApprovalDecision(
                request_id=pending[0].request_id,
                reviewer_id="auditor",
                approved=False,
                reason="Not authorised",
            )
        )
        decision = await asyncio.wait_for(task, timeout=2.0)
        assert not decision.approved

    @pytest.mark.asyncio
    async def test_timeout_auto_rejects(self):
        """Verify that ApprovalRequest.is_expired drives auto-reject correctly."""
        policy = EscalationPolicy(
            name="fast",
            levels=[EscalationLevel(EscalationTier.L1, "leads", 1)],
            on_final_timeout="auto_reject",
        )
        orchestrator = HITLOrchestrator(escalation_policy=policy, poll_interval=0.05)

        # Create an already-expired request directly to avoid wall-clock wait
        import time as _time

        req = ApprovalRequest.create("a", "r", "act", "res", timeout_seconds=0)
        req.expires_at = _time.time() - 1  # force expired
        orchestrator._pending[req.request_id] = req

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        orchestrator._futures[req.request_id] = future

        # Trigger timeout handling directly
        await orchestrator._handle_timeout(req)

        decision = future.result()
        assert not decision.approved
        assert "auto_reject" in decision.reason

    def test_queue_depth(self):
        orchestrator = HITLOrchestrator()
        assert orchestrator.queue_depth() == 0
