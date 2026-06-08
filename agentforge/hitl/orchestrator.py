"""HITL Orchestrator — manages approval queues, callbacks, and escalation."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from .approvals import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
)
from .escalation import EscalationPolicy

# Callback type: receives an ApprovalRequest and sends it to reviewers
NotifyCallback = Callable[[ApprovalRequest, str], Awaitable[None]]


class HITLOrchestrator:
    """
    Async orchestrator for human-in-the-loop approval workflows.

    The orchestrator maintains a queue of pending approval requests
    and surfaces them to human reviewers via pluggable notification
    callbacks (email, Slack, PagerDuty, etc.).

    Agent code suspends on ``await orchestrator.request_approval(...)``
    and resumes only after a human approves/rejects or the request times out.

    Example::

        async def slack_notifier(request, reviewer_group):
            await slack.post(f"#approvals", request.to_dict())

        orchestrator = HITLOrchestrator(
            escalation_policy=EscalationPolicy.standard(),
            notify=slack_notifier,
        )

        # In agent code:
        decision = await orchestrator.request_approval(
            agent_id="payment-agent",
            correlation_id=run_id,
            action="wire_transfer",
            resource="payment-gateway",
            context={"amount_usd": 75_000},
        )
        if not decision.approved:
            raise PermissionError("Transfer rejected by reviewer.")
    """

    def __init__(
        self,
        escalation_policy: EscalationPolicy | None = None,
        notify: NotifyCallback | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._policy = escalation_policy or EscalationPolicy.standard()
        self._notify = notify
        self._poll_interval = poll_interval
        self._pending: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, ApprovalDecision] = {}
        self._futures: dict[str, asyncio.Future[ApprovalDecision]] = {}

    async def request_approval(
        self,
        agent_id: str,
        correlation_id: str,
        action: str,
        resource: str,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 3600,
    ) -> ApprovalDecision:
        """
        Submit an approval request and suspend until it is resolved.

        Returns the ``ApprovalDecision`` made by the reviewer (or the
        system if the request times out).
        """
        request = ApprovalRequest.create(
            agent_id=agent_id,
            correlation_id=correlation_id,
            action=action,
            resource=resource,
            context=context,
            timeout_seconds=timeout_seconds,
        )
        self._pending[request.request_id] = request

        loop = asyncio.get_event_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._futures[request.request_id] = future

        # Notify the initial tier
        await self._notify_tier(request)

        # Start escalation/timeout monitor
        asyncio.create_task(self._monitor(request.request_id))

        return await future

    async def decide(self, decision: ApprovalDecision) -> None:
        """
        Called by a reviewer (human or downstream system) to resolve a request.
        """
        request = self._pending.get(decision.request_id)
        if request is None or request.is_resolved:
            return

        request.status = ApprovalStatus.APPROVED if decision.approved else ApprovalStatus.REJECTED
        request.reviewer_id = decision.reviewer_id
        request.resolved_at = decision.decided_at
        request.reason = decision.reason

        self._decisions[decision.request_id] = decision
        future = self._futures.pop(decision.request_id, None)
        if future and not future.done():
            future.set_result(decision)

    async def _monitor(self, request_id: str) -> None:
        """Background task: escalate or time out unresolved requests."""
        while True:
            await asyncio.sleep(self._poll_interval)
            request = self._pending.get(request_id)
            if request is None or request.is_resolved:
                return

            age = time.time() - request.created_at

            # Check timeout
            if request.is_expired:
                await self._handle_timeout(request)
                return

            # Check escalation
            tier = self._policy.current_tier(age)
            if tier and tier.tier.value != f"l{request.escalation_level}":
                request.escalation_level += 1
                request.status = ApprovalStatus.ESCALATED
                await self._notify_tier(request)

    async def _handle_timeout(self, request: ApprovalRequest) -> None:
        on_timeout = self._policy.on_final_timeout
        approved = on_timeout == "auto_approve"
        request.status = ApprovalStatus.TIMEOUT

        decision = ApprovalDecision(
            request_id=request.request_id,
            reviewer_id="system",
            approved=approved,
            reason=f"Auto-{on_timeout} after timeout",
        )
        self._decisions[request.request_id] = decision
        future = self._futures.pop(request.request_id, None)
        if future and not future.done():
            future.set_result(decision)

    async def _notify_tier(self, request: ApprovalRequest) -> None:
        if self._notify is None:
            return
        tier = self._policy.current_tier(time.time() - request.created_at)
        reviewer_group = tier.reviewer_group if tier else "admin"
        try:
            await self._notify(request, reviewer_group)
        except Exception:
            pass  # notification failure must never block agent execution

    def pending_requests(self) -> list[ApprovalRequest]:
        return [r for r in self._pending.values() if r.is_pending]

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        return self._pending.get(request_id)

    def queue_depth(self) -> int:
        return len(self.pending_requests())
