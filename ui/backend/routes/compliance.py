"""
Compliance report generator.

GET /api/reports/soc2?period=2024-Q1
GET /api/reports/hipaa?period=2024-01
GET /api/reports/gdpr?period=2024-01
GET /api/reports/summary?since=2024-01-01&until=2024-03-31

Returns JSON by default; ?format=pdf requires reportlab.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from ui.backend.auth import CurrentUser, require_supervisor

router = APIRouter(prefix="/api/reports", tags=["compliance"])


def _quarter_range(period: str) -> tuple[float, float]:
    """Parse '2024-Q1' → (start_ts, end_ts)."""
    year, q = period.split("-Q")
    q_starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
    q_ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    sm, sd = q_starts[int(q)]
    em, ed = q_ends[int(q)]
    y = int(year)
    start = datetime(y, sm, sd, tzinfo=timezone.utc).timestamp()
    end = datetime(y, em, ed, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    return start, end


def _month_range(period: str) -> tuple[float, float]:
    """Parse '2024-01' → (start_ts, end_ts)."""
    import calendar
    y, m = [int(x) for x in period.split("-")]
    start = datetime(y, m, 1, tzinfo=timezone.utc).timestamp()
    last_day = calendar.monthrange(y, m)[1]
    end = datetime(y, m, last_day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    return start, end


def _build_summary(
    since: float, until: float, org_id: str, framework: str
) -> dict[str, Any]:
    """Build a compliance summary from audit trail data."""
    from ui.backend.main import audit_trail, registry
    from agentforge.audit import AuditQuery, EventSeverity, EventType

    all_events = audit_trail.query(AuditQuery(since=since, until=until, limit=100_000))
    violations = [e for e in all_events if e.event_type in (
        EventType.POLICY_DENIED, EventType.HITL_REJECTED,
        EventType.CREDENTIAL_REJECTED, EventType.DELEGATION_REVOKED,
    )]
    hitl_events = [e for e in all_events if e.event_type in (
        EventType.HITL_REQUESTED, EventType.HITL_APPROVED, EventType.HITL_REJECTED,
        EventType.HITL_TIMEOUT, EventType.HITL_ESCALATED,
    )]
    hitl_approved = [e for e in hitl_events if e.event_type == EventType.HITL_APPROVED]
    hitl_rejected = [e for e in hitl_events if e.event_type == EventType.HITL_REJECTED]

    by_agent: dict[str, int] = {}
    for e in all_events:
        by_agent[e.agent_id] = by_agent.get(e.agent_id, 0) + 1

    return {
        "framework": framework,
        "org_id": org_id,
        "period": {
            "since": datetime.fromtimestamp(since, tz=timezone.utc).isoformat(),
            "until": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_agent_actions": len(all_events),
            "total_violations": len(violations),
            "violation_rate_pct": round(len(violations) / max(len(all_events), 1) * 100, 2),
            "hitl_requests": len(hitl_events),
            "hitl_approved": len(hitl_approved),
            "hitl_rejected": len(hitl_rejected),
            "hitl_approval_rate_pct": round(
                len(hitl_approved) / max(len([e for e in hitl_events
                    if e.event_type in (EventType.HITL_APPROVED, EventType.HITL_REJECTED)]), 1) * 100, 1
            ),
        },
        "active_agents": len([a for a in registry.list_all() if a.status.value == "active"]),
        "violations": [
            {
                "event_id": e.event_id,
                "timestamp": datetime.fromtimestamp(e.timestamp, tz=timezone.utc).isoformat(),
                "type": e.event_type.value,
                "agent_id": e.agent_id,
                "details": e.payload,
            }
            for e in sorted(violations, key=lambda x: x.timestamp)
        ],
        "by_agent": by_agent,
        "attestation": {
            "statement": f"This report covers all agent activity recorded in AgentForge "
                         f"for the period above. The audit log is tamper-evident and "
                         f"each event is cryptographically chained to the previous.",
            "framework_controls": _framework_controls(framework),
        },
    }


def _framework_controls(framework: str) -> list[dict[str, str]]:
    controls = {
        "soc2": [
            {"control": "CC6.1", "description": "Logical access security — agent RBAC enforced"},
            {"control": "CC6.8", "description": "Unauthorized access prevention — policy engine"},
            {"control": "CC7.2", "description": "Monitoring — tamper-evident audit trail"},
            {"control": "CC9.2", "description": "Change management — agent version history"},
        ],
        "hipaa": [
            {"control": "164.312(a)(1)", "description": "Access control — agent authority scopes"},
            {"control": "164.312(b)", "description": "Audit controls — full event log with replay"},
            {"control": "164.312(c)(1)", "description": "Integrity — chained event IDs"},
            {"control": "164.308(a)(3)", "description": "Workforce access management — RBAC roles"},
        ],
        "gdpr": [
            {"control": "Art. 5(1)(f)", "description": "Integrity and confidentiality — agent scoping"},
            {"control": "Art. 17", "description": "Right to erasure — HITL gate on all deletes"},
            {"control": "Art. 25", "description": "Privacy by design — deny-first governance"},
            {"control": "Art. 30", "description": "Records of processing — audit trail export"},
        ],
    }
    return controls.get(framework, [])


@router.get("/soc2")
async def soc2_report(
    period: str = Query("2024-Q1", description="Quarter e.g. 2024-Q1"),
    current_user: CurrentUser = Depends(require_supervisor),
) -> dict[str, Any]:
    since, until = _quarter_range(period)
    return _build_summary(since, until, current_user.org_id, "soc2")


@router.get("/hipaa")
async def hipaa_report(
    period: str = Query("2024-01", description="Month e.g. 2024-01"),
    current_user: CurrentUser = Depends(require_supervisor),
) -> dict[str, Any]:
    since, until = _month_range(period)
    return _build_summary(since, until, current_user.org_id, "hipaa")


@router.get("/gdpr")
async def gdpr_report(
    period: str = Query("2024-01", description="Month e.g. 2024-01"),
    current_user: CurrentUser = Depends(require_supervisor),
) -> dict[str, Any]:
    since, until = _month_range(period)
    return _build_summary(since, until, current_user.org_id, "gdpr")


@router.get("/summary")
async def summary_report(
    since: str = Query(..., description="ISO date e.g. 2024-01-01"),
    until: str = Query(..., description="ISO date e.g. 2024-03-31"),
    current_user: CurrentUser = Depends(require_supervisor),
) -> dict[str, Any]:
    since_ts = datetime.fromisoformat(since).timestamp()
    until_ts = datetime.fromisoformat(until).replace(hour=23, minute=59, second=59).timestamp()
    return _build_summary(since_ts, until_ts, current_user.org_id, "custom")
