"""Slack HITL notifier — rich Block Kit messages with approve/reject links."""

from __future__ import annotations

import json
from typing import Any

from agentforge.hitl.approvals import ApprovalRequest
from ui.backend.config import get_settings


async def slack_notify(request: ApprovalRequest, reviewer_group: str) -> None:
    """
    Send a rich Slack notification for a pending HITL approval.

    Sends to the configured #agent-approvals channel with a Block Kit
    message that includes the agent, action, context, and a deep link
    back to the AgentForge console.
    """
    settings = get_settings()
    if not settings.slack_webhook_url and not settings.slack_bot_token:
        return  # Slack not configured — silent no-op

    blocks = _build_blocks(request, reviewer_group)

    if settings.slack_webhook_url:
        await _send_via_webhook(settings.slack_webhook_url, blocks)
    elif settings.slack_bot_token:
        await _send_via_api(settings.slack_bot_token, settings.slack_approval_channel, blocks)


def _build_blocks(request: ApprovalRequest, reviewer_group: str) -> list[dict[str, Any]]:
    ctx_lines = "\n".join(f"• *{k}:* {v}" for k, v in (request.context or {}).items())
    import os
    host = os.getenv("RAILWAY_PUBLIC_DOMAIN", os.getenv("APP_URL", "localhost:8000"))
    proto = "https" if "localhost" not in host else "http"
    console_url = f"{proto}://{host}/#hitl"

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⚠️  Agent Approval Required", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Agent:*\n`{request.agent_id}`"},
                {"type": "mrkdwn", "text": f"*Action:*\n`{request.action}`"},
                {"type": "mrkdwn", "text": f"*Resource:*\n`{request.resource}`"},
                {"type": "mrkdwn", "text": f"*Reviewer Group:*\n{reviewer_group}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Context:*\n{ctx_lines or '_No context provided_'}"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Review in Console"},
                    "style": "primary",
                    "url": console_url,
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Run ID: `{request.correlation_id}` · Request: `{request.request_id[:8]}…`"},
            ],
        },
    ]


async def _send_via_webhook(webhook_url: str, blocks: list[dict]) -> None:
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(webhook_url, json={"blocks": blocks}, timeout=5.0)
    except Exception:
        pass  # Notification failure must never block agent execution


async def _send_via_api(token: str, channel: str, blocks: list[dict]) -> None:
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel, "blocks": blocks},
                timeout=5.0,
            )
    except Exception:
        pass
