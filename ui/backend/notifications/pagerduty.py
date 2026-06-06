"""PagerDuty Events API v2 notifier — for critical / on-call escalations."""

from __future__ import annotations

from agentforge.hitl.approvals import ApprovalRequest
from ui.backend.config import get_settings


async def pagerduty_notify(request: ApprovalRequest, reviewer_group: str) -> None:
    """
    Trigger a PagerDuty incident for a HITL request reaching L3 (emergency) tier.
    Only fires when reviewer_group indicates on-call / emergency escalation.
    """
    settings = get_settings()
    if not settings.pagerduty_routing_key:
        return
    if reviewer_group not in ("on-call", "emergency", "executives"):
        return  # Only page for high-tier escalations

    payload = {
        "routing_key": settings.pagerduty_routing_key,
        "event_action": "trigger",
        "dedup_key": request.request_id,
        "payload": {
            "summary": f"AgentForge HITL: {request.action} on {request.resource} needs approval",
            "severity": "warning",
            "source": request.agent_id,
            "custom_details": {
                "agent_id": request.agent_id,
                "action": request.action,
                "resource": request.resource,
                "context": request.context,
                "correlation_id": request.correlation_id,
                "console_url": "http://localhost:8000/#hitl",
            },
        },
    }

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://events.pagerduty.com/v2/enqueue",
                json=payload,
                timeout=5.0,
            )
    except Exception:
        pass
