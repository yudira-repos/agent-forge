"""
Audit log export — CSV and JSON for compliance teams and SIEMs.

GET /api/audit/export?format=csv&since=2024-01-01&until=2024-03-31&agent_id=...
"""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, StreamingResponse

from ui.backend.auth import CurrentUser, require_viewer, get_current_user

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/export")
async def export_audit(
    format: Literal["csv", "json", "jsonl"] = Query("json"),
    since: str | None = Query(None, description="ISO date e.g. 2024-01-01"),
    until: str | None = Query(None, description="ISO date e.g. 2024-03-31"),
    agent_id: str | None = None,
    severity: str | None = None,
    correlation_id: str | None = None,
    limit: int = Query(10_000, le=100_000),
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    """
    Export audit events for compliance reporting and SIEM ingestion.

    Supported formats:
    - json  : JSON array (default)
    - jsonl : newline-delimited JSON (best for streaming to SIEMs)
    - csv   : spreadsheet-friendly

    Example:
        GET /api/audit/export?format=csv&since=2024-01-01&severity=warning
    """
    from ui.backend.main import audit_trail, audit_sink
    from agentforge.audit import AuditQuery, EventSeverity

    since_ts = _parse_date(since) if since else None
    until_ts = _parse_date(until, end_of_day=True) if until else None

    q = AuditQuery(
        agent_id=agent_id,
        correlation_id=correlation_id,
        min_severity=EventSeverity(severity) if severity else EventSeverity.DEBUG,
        since=since_ts,
        until=until_ts,
        limit=limit,
    )
    events = audit_trail.query(q)
    events_sorted = sorted(events, key=lambda e: e.timestamp)

    filename_base = f"agentforge-audit-{current_user.org_id}-{int(time.time())}"

    if format == "csv":
        return _to_csv(events_sorted, filename_base)
    elif format == "jsonl":
        return _to_jsonl(events_sorted, filename_base)
    else:
        return _to_json(events_sorted, filename_base)


def _to_csv(events: list, filename: str) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "timestamp", "event_id", "event_type", "agent_id",
        "correlation_id", "severity", "previous_event_id", "payload",
    ])
    writer.writeheader()
    for e in events:
        writer.writerow({
            "timestamp": datetime.fromtimestamp(e.timestamp).isoformat(),
            "event_id": e.event_id,
            "event_type": e.event_type.value,
            "agent_id": e.agent_id,
            "correlation_id": e.correlation_id,
            "severity": e.severity.value,
            "previous_event_id": e.previous_event_id or "",
            "payload": json.dumps(e.payload),
        })
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


def _to_jsonl(events: list, filename: str) -> StreamingResponse:
    lines = "\n".join(json.dumps(e.to_dict()) for e in events)
    return StreamingResponse(
        iter([lines]),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}.jsonl"'},
    )


def _to_json(events: list, filename: str) -> Response:
    data = json.dumps([e.to_dict() for e in events], indent=2)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
    )


def _parse_date(s: str, end_of_day: bool = False) -> float:
    dt = datetime.fromisoformat(s)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.timestamp()
