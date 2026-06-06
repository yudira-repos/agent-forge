"""
AgentForge Enterprise UI — FastAPI backend
==========================================
Run:  pip install fastapi uvicorn
      python ui/backend/main.py

Opens at http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── AgentForge imports ────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agentforge.aiam import AgentIdentity, RBACPolicy, Permission
from agentforge.audit import AuditLogger, AuditQuery, AuditTrail, EventSeverity, EventType
from agentforge.audit.logger import InMemoryAuditSink
from agentforge.governance import PolicyContext, SOC2Profile
from agentforge.hitl import ApprovalDecision, ApprovalRequest, ApprovalStatus, EscalationPolicy, HITLOrchestrator
from agentforge.registry import AgentCapability, AgentManifest, AgentRegistry, AgentStatus
from agentforge.registry.registry import SQLiteBackend

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="AgentForge UI", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = Path(__file__).parent.parent / "static"

# ── Global state (in-memory for demo) ────────────────────────────────────────
registry = AgentRegistry()
audit_sink = InMemoryAuditSink()
audit_logger = AuditLogger(sinks=[audit_sink])
audit_trail = AuditTrail(audit_sink)
gov_engine = SOC2Profile.engine()
hitl_orchestrator = HITLOrchestrator(
    escalation_policy=EscalationPolicy.standard(),
    poll_interval=5.0,
)

# WebSocket connections for real-time push
_ws_clients: list[WebSocket] = []


async def broadcast(event: dict[str, Any]) -> None:
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


# ── Demo data seeder ──────────────────────────────────────────────────────────
def seed_demo_data() -> None:
    """Populate the registry and audit log with realistic LifeOS demo data."""

    agents = [
        AgentManifest(
            agent_id="health-agent-001",
            name="Health Tracker",
            version="2.1.0",
            description="Tracks vitals, sleep, HRV and nutrition from wearables (Oura, Apple Health)",
            owner="lifeos-core",
            capabilities=[
                AgentCapability("log_vitals", "Log heart rate, sleep hours, HRV", tags=["health"]),
                AgentCapability("analyze_trends", "Summarize 7-day health trends", tags=["health", "nlp"]),
                AgentCapability("delete_health_record", "Delete a health entry", tags=["health"], requires_hitl=True),
            ],
            runtime_adapter="anthropic",
            tags=["health", "wearables"],
            status=AgentStatus.ACTIVE,
        ),
        AgentManifest(
            agent_id="finance-agent-001",
            name="Finance Manager",
            version="1.8.0",
            description="Tracks spending, categorizes transactions, monitors budgets and investments",
            owner="lifeos-core",
            capabilities=[
                AgentCapability("categorize_transaction", "Auto-categorize a bank transaction", tags=["finance"]),
                AgentCapability("generate_report", "Monthly spend summary", tags=["finance", "report"]),
                AgentCapability("transfer_funds", "Move money between accounts", tags=["finance"], requires_hitl=True),
                AgentCapability("cancel_subscription", "Cancel a recurring charge", tags=["finance"], requires_hitl=True),
            ],
            runtime_adapter="openai",
            tags=["finance", "banking"],
            status=AgentStatus.ACTIVE,
        ),
        AgentManifest(
            agent_id="goals-agent-001",
            name="Goals Coach",
            version="1.2.0",
            description="Tracks OKRs, daily habits, and quarterly goals. Sends nudges.",
            owner="lifeos-core",
            capabilities=[
                AgentCapability("review_goals", "Review progress against this week's goals", tags=["goals"]),
                AgentCapability("suggest_priorities", "Recommend top 3 tasks for today", tags=["goals", "nlp"]),
            ],
            runtime_adapter="anthropic",
            tags=["goals", "productivity"],
            status=AgentStatus.ACTIVE,
        ),
        AgentManifest(
            agent_id="journal-agent-001",
            name="Journal Analyst",
            version="1.0.0",
            description="Analyzes journal entries for mood patterns and recurring themes",
            owner="lifeos-core",
            capabilities=[
                AgentCapability("analyze_mood", "Detect mood trends from journal entries", tags=["journal", "nlp"]),
                AgentCapability("generate_summary", "Weekly journal summary", tags=["journal"]),
            ],
            runtime_adapter="anthropic",
            tags=["journal", "wellbeing"],
            status=AgentStatus.ACTIVE,
        ),
        AgentManifest(
            agent_id="scheduler-agent-001",
            name="Smart Scheduler",
            version="0.9.0",
            description="Manages calendar, meeting prep, and travel logistics",
            owner="lifeos-core",
            capabilities=[
                AgentCapability("suggest_slots", "Find open meeting slots", tags=["calendar"]),
                AgentCapability("book_appointment", "Book a calendar event", tags=["calendar"], requires_hitl=True),
            ],
            runtime_adapter="vertex",
            tags=["calendar", "scheduling"],
            status=AgentStatus.MAINTENANCE,
        ),
    ]

    for m in agents:
        registry.register(m)

    # Seed audit events — realistic LifeOS morning run
    base_time = time.time() - 3600  # 1 hour ago

    events_to_seed = [
        (EventType.AGENT_STARTED, "health-agent-001", "run-mon-001", {"trigger": "morning_routine"}, EventSeverity.INFO, base_time),
        (EventType.TOOL_INVOKED, "health-agent-001", "run-mon-001", {"tool": "log_vitals", "heart_rate": 68, "sleep_hours": 7.4, "hrv": 61}, EventSeverity.INFO, base_time + 2),
        (EventType.TOOL_COMPLETED, "health-agent-001", "run-mon-001", {"tool": "log_vitals", "duration_ms": 312}, EventSeverity.INFO, base_time + 3),
        (EventType.AGENT_STARTED, "finance-agent-001", "run-mon-001", {"trigger": "morning_routine"}, EventSeverity.INFO, base_time + 5),
        (EventType.TOOL_INVOKED, "finance-agent-001", "run-mon-001", {"tool": "categorize_transaction", "merchant": "Whole Foods", "amount": 67.43}, EventSeverity.INFO, base_time + 7),
        (EventType.TOOL_COMPLETED, "finance-agent-001", "run-mon-001", {"tool": "categorize_transaction", "category": "Groceries", "duration_ms": 180}, EventSeverity.INFO, base_time + 8),
        (EventType.POLICY_EVALUATED, "finance-agent-001", "run-mon-001", {"action": "transfer_funds", "amount": 4200, "effect": "require_hitl"}, EventSeverity.WARNING, base_time + 12),
        (EventType.HITL_REQUESTED, "finance-agent-001", "run-mon-001", {"action": "transfer_funds", "amount": 4200, "payee": "Bay Area Properties LLC", "reason": "Amount exceeds $500 threshold"}, EventSeverity.WARNING, base_time + 13),
        (EventType.POLICY_DENIED, "finance-agent-001", "run-mon-001", {"attempted": "delete health records", "reason": "finance-agent lacks permission"}, EventSeverity.ERROR, base_time + 15),
        (EventType.AGENT_STARTED, "goals-agent-001", "run-mon-001", {"trigger": "morning_routine"}, EventSeverity.INFO, base_time + 20),
        (EventType.TOOL_INVOKED, "goals-agent-001", "run-mon-001", {"tool": "suggest_priorities"}, EventSeverity.INFO, base_time + 22),
        (EventType.TOOL_COMPLETED, "goals-agent-001", "run-mon-001", {"tool": "suggest_priorities", "priorities": ["Finish Q3 review", "30min walk", "Call dentist"], "duration_ms": 890}, EventSeverity.INFO, base_time + 23),
        (EventType.AGENT_STARTED, "journal-agent-001", "run-tue-001", {"trigger": "evening_reflection"}, EventSeverity.INFO, base_time + 3600 - 1800),
        (EventType.TOOL_INVOKED, "journal-agent-001", "run-tue-001", {"tool": "analyze_mood"}, EventSeverity.INFO, base_time + 3600 - 1798),
        (EventType.TOOL_COMPLETED, "journal-agent-001", "run-tue-001", {"tool": "analyze_mood", "mood_score": 7.2, "theme": "productive but stressed", "duration_ms": 1240}, EventSeverity.INFO, base_time + 3600 - 1797),
        (EventType.HITL_REQUESTED, "scheduler-agent-001", "run-wed-001", {"action": "book_appointment", "title": "Dentist checkup", "time": "2024-06-07 14:00"}, EventSeverity.WARNING, base_time + 3600 - 900),
    ]

    for ev_type, agent_id, corr_id, payload, severity, ts in events_to_seed:
        event = audit_logger.log(ev_type, agent_id, corr_id, payload, severity)
        event.timestamp = ts  # backdate for realism

    # Seed pending HITL approvals
    req1 = ApprovalRequest.create(
        agent_id="finance-agent-001",
        correlation_id="run-mon-001",
        action="transfer_funds",
        resource="bank-account",
        context={"amount_usd": 4200, "payee": "Bay Area Properties LLC", "memo": "June rent"},
        timeout_seconds=3600,
    )
    req1.created_at = base_time + 13
    req1.expires_at = base_time + 13 + 3600

    req2 = ApprovalRequest.create(
        agent_id="scheduler-agent-001",
        correlation_id="run-wed-001",
        action="book_appointment",
        resource="google-calendar",
        context={"title": "Dentist Checkup", "time": "Fri Jun 7 2:00 PM", "duration_mins": 60},
        timeout_seconds=7200,
    )
    req2.created_at = base_time + 3600 - 900
    req2.expires_at = base_time + 3600 - 900 + 7200

    hitl_orchestrator._pending[req1.request_id] = req1
    hitl_orchestrator._pending[req2.request_id] = req2


seed_demo_data()


# ── Pydantic models ────────────────────────────────────────────────────────────
class ApprovePayload(BaseModel):
    reviewer_id: str = "you@lifeos"
    reason: str = ""


class RejectPayload(BaseModel):
    reviewer_id: str = "you@lifeos"
    reason: str


class RegisterAgentPayload(BaseModel):
    agent_id: str
    name: str
    version: str
    description: str
    owner: str
    tags: list[str] = []
    runtime_adapter: str = "anthropic"


# ── Routes: Dashboard ──────────────────────────────────────────────────────────
@app.get("/api/dashboard")
def get_dashboard() -> dict[str, Any]:
    all_agents = registry.list_all()
    stats = registry.stats()
    pending = hitl_orchestrator.pending_requests()
    trail_stats = audit_trail.stats()

    recent_events = sorted(audit_sink.events, key=lambda e: e.timestamp, reverse=True)[:8]
    violations = audit_trail.violations()
    recent_violations = sorted(violations, key=lambda e: e.timestamp, reverse=True)[:5]

    return {
        "agents": stats,
        "hitl_queue_depth": len(pending),
        "audit": trail_stats,
        "recent_events": [_fmt_event(e) for e in recent_events],
        "recent_violations": [_fmt_event(e) for e in recent_violations],
        "agent_health": [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "status": a.status.value,
                "adapter": a.runtime_adapter,
                "tags": a.tags,
            }
            for a in all_agents
        ],
    }


# ── Routes: Registry ───────────────────────────────────────────────────────────
@app.get("/api/agents")
def list_agents() -> list[dict[str, Any]]:
    return [a.to_dict() for a in registry.list_all()]


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str) -> dict[str, Any]:
    m = registry.get(agent_id)
    if not m:
        raise HTTPException(status_code=404, detail="Agent not found")
    history = registry.version_history(agent_id)
    d = m.to_dict()
    d["version_history"] = [h.to_dict() for h in history]
    return d


@app.post("/api/agents")
async def register_agent(payload: RegisterAgentPayload) -> dict[str, Any]:
    manifest = AgentManifest(
        agent_id=payload.agent_id,
        name=payload.name,
        version=payload.version,
        description=payload.description,
        owner=payload.owner,
        tags=payload.tags,
        runtime_adapter=payload.runtime_adapter,
    )
    registry.register(manifest)
    audit_logger.log(EventType.AGENT_REGISTERED, payload.agent_id, str(uuid.uuid4()),
                     payload={"name": payload.name, "version": payload.version})
    await broadcast({"type": "agent_registered", "agent_id": payload.agent_id, "name": payload.name})
    return manifest.to_dict()


@app.patch("/api/agents/{agent_id}/deprecate")
async def deprecate_agent(agent_id: str) -> dict[str, Any]:
    if not registry.deprecate(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    await broadcast({"type": "agent_deprecated", "agent_id": agent_id})
    return {"status": "deprecated"}


# ── Routes: HITL ───────────────────────────────────────────────────────────────
@app.get("/api/hitl/pending")
def get_pending_hitl() -> list[dict[str, Any]]:
    pending = hitl_orchestrator.pending_requests()
    result = []
    for req in pending:
        age_secs = time.time() - req.created_at
        tier = hitl_orchestrator._policy.current_tier(age_secs)
        result.append({
            **req.to_dict(),
            "age_seconds": round(age_secs),
            "escalation_tier": tier.tier.value if tier else "exhausted",
            "reviewer_group": tier.reviewer_group if tier else "—",
        })
    return sorted(result, key=lambda r: r["created_at"])


@app.post("/api/hitl/{request_id}/approve")
async def approve_hitl(request_id: str, payload: ApprovePayload) -> dict[str, Any]:
    req = hitl_orchestrator.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    decision = ApprovalDecision(
        request_id=request_id,
        reviewer_id=payload.reviewer_id,
        approved=True,
        reason=payload.reason or "Approved via AgentForge UI",
    )
    await hitl_orchestrator.decide(decision)
    audit_logger.log(EventType.HITL_APPROVED, req.agent_id, req.correlation_id,
                     payload={"reviewer": payload.reviewer_id, "reason": decision.reason},
                     severity=EventSeverity.INFO)
    await broadcast({
        "type": "hitl_approved",
        "request_id": request_id,
        "agent_id": req.agent_id,
        "action": req.action,
    })
    return {"status": "approved", "request_id": request_id}


@app.post("/api/hitl/{request_id}/reject")
async def reject_hitl(request_id: str, payload: RejectPayload) -> dict[str, Any]:
    req = hitl_orchestrator.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    decision = ApprovalDecision(
        request_id=request_id,
        reviewer_id=payload.reviewer_id,
        approved=False,
        reason=payload.reason,
    )
    await hitl_orchestrator.decide(decision)
    audit_logger.log(EventType.HITL_REJECTED, req.agent_id, req.correlation_id,
                     payload={"reviewer": payload.reviewer_id, "reason": payload.reason},
                     severity=EventSeverity.WARNING)
    await broadcast({
        "type": "hitl_rejected",
        "request_id": request_id,
        "agent_id": req.agent_id,
        "action": req.action,
        "reason": payload.reason,
    })
    return {"status": "rejected", "request_id": request_id}


# ── Routes: Audit ──────────────────────────────────────────────────────────────
@app.get("/api/audit")
def query_audit(
    agent_id: str | None = None,
    correlation_id: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    q = AuditQuery(
        agent_id=agent_id,
        correlation_id=correlation_id,
        min_severity=EventSeverity(severity) if severity else EventSeverity.DEBUG,
        limit=limit,
    )
    events = audit_trail.query(q)
    events_sorted = sorted(events, key=lambda e: e.timestamp, reverse=True)
    return {
        "events": [_fmt_event(e) for e in events_sorted],
        "total": len(events_sorted),
        "stats": audit_trail.stats(),
    }


@app.get("/api/audit/{correlation_id}/replay")
def replay_run(correlation_id: str) -> dict[str, Any]:
    steps = audit_trail.replay(correlation_id)
    return {"correlation_id": correlation_id, "steps": steps}


@app.get("/api/audit/correlations")
def list_correlations() -> list[str]:
    seen: set[str] = set()
    return list({e.correlation_id for e in audit_sink.events
                 if e.correlation_id not in seen and not seen.add(e.correlation_id)})  # type: ignore


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


# ── Static files + SPA ────────────────────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_ui() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>AgentForge UI</h1><p>Static files not found. Place index.html in ui/static/</p>")


# ── Helpers ────────────────────────────────────────────────────────────────────
def _fmt_event(e: Any) -> dict[str, Any]:
    return {
        "event_id": e.event_id,
        "event_type": e.event_type.value,
        "agent_id": e.agent_id,
        "correlation_id": e.correlation_id,
        "timestamp": e.timestamp,
        "severity": e.severity.value,
        "payload": e.payload,
        "previous_event_id": e.previous_event_id,
    }


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("\n★  AgentForge Enterprise UI  ★")
    print("   http://localhost:8000\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True,
                app_dir=str(Path(__file__).parent))
