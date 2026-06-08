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

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── AgentForge imports ────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "ui"))

from agentforge.aiam import AgentIdentity, RBACPolicy, Permission
from agentforge.audit import AuditLogger, AuditQuery, AuditTrail, EventSeverity, EventType
from agentforge.audit.logger import InMemoryAuditSink
from ui.backend.notifications.slack import slack_notify
from agentforge.governance import PolicyContext, SOC2Profile
from agentforge.hitl import ApprovalDecision, ApprovalRequest, ApprovalStatus, EscalationPolicy, HITLOrchestrator
from agentforge.registry import AgentCapability, AgentManifest, AgentRegistry, AgentStatus
from agentforge.registry.registry import SQLiteBackend

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="AgentForge UI", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Routers ───────────────────────────────────────────────────────────────────
from ui.backend.routes.workflows import router as workflows_router  # noqa: E402
from ui.backend.db import sqlite_store  # noqa: E402

# IMPORTANT: register /api/workflows/runs* routes BEFORE including the
# workflows router.  The router has GET /api/workflows/{workflow_id} which
# Starlette matches in registration order — it would swallow "runs" as a
# workflow_id if registered first.
@app.get("/api/workflows/runs")
def list_workflow_runs(workflow_id: str | None = None) -> list[dict[str, Any]]:
    runs = list(_workflow_runs.values())
    if workflow_id:
        runs = [r for r in runs if r.workflow_id == workflow_id]
    return [r.model_dump() for r in sorted(runs, key=lambda r: r.started_at, reverse=True)]


@app.get("/api/workflows/runs/{run_id}")
def get_workflow_run(run_id: str) -> dict[str, Any]:
    run = _workflow_runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run.model_dump()


@app.get("/api/workflows/runs/{run_id}/audit")
def get_run_audit(run_id: str) -> list[dict[str, Any]]:
    """All audit events for a specific workflow run (matched by correlation_id = run_id)."""
    from agentforge.audit import AuditQuery
    q = AuditQuery(correlation_id=run_id, limit=200)
    events = audit_trail.query(q)
    return [_fmt_event(e) for e in sorted(events, key=lambda e: e.timestamp)]


app.include_router(workflows_router)

# ── Health endpoints (liveness + readiness) ───────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}

@app.get("/ready")
def ready() -> dict:
    return {"status": "ready"}

STATIC_DIR = Path(__file__).parent.parent / "static"

# ── Global state (in-memory for demo) ────────────────────────────────────────
registry = AgentRegistry()
audit_sink = InMemoryAuditSink()
audit_logger = AuditLogger(sinks=[audit_sink])
audit_trail = AuditTrail(audit_sink)
gov_engine = SOC2Profile.engine()
hitl_orchestrator = HITLOrchestrator(
    escalation_policy=EscalationPolicy.standard(),
    notify=slack_notify,   # real Slack notifications
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


# ── Auth — demo user store ─────────────────────────────────────────────────────
import hashlib, hmac as _hmac

# In-memory user store for demo (replace with DB in production)
# Format: email → {name, role, password_hash}
_USERS: dict[str, dict] = {}

def _hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def _seed_users() -> None:
    """Pre-seed demo accounts shown on the login screen."""
    _USERS["admin@agentforge.io"]    = {"name": "Ravi (Admin)",      "role": "admin",      "hash": _hash_pw("demo1234")}
    _USERS["cto@acme.io"]            = {"name": "CTO Demo Account",  "role": "admin",      "hash": _hash_pw("demo1234")}
    _USERS["auditor@acme.io"]        = {"name": "Compliance Auditor","role": "supervisor", "hash": _hash_pw("demo1234")}
    _USERS["viewer@acme.io"]         = {"name": "Read-Only Viewer",  "role": "viewer",     "hash": _hash_pw("demo1234")}

_seed_users()

class LoginPayload(BaseModel):
    email: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict

import base64 as _b64, json as _json, time as _time

def _make_token(email: str, role: str, name: str) -> str:
    """Lightweight signed token (no python-jose dependency needed for demo)."""
    import hmac as _h, hashlib as _hs
    payload = {"sub": email, "role": role, "name": name, "exp": _time.time() + 86400 * 7}
    data = _b64.urlsafe_b64encode(_json.dumps(payload).encode()).decode()
    sig  = _h.new(b"agentforge-demo-secret", data.encode(), _hs.sha256).hexdigest()[:16]
    return f"{data}.{sig}"

def _verify_token(token: str) -> dict | None:
    import hmac as _h, hashlib as _hs
    try:
        data, sig = token.rsplit(".", 1)
        expected = _h.new(b"agentforge-demo-secret", data.encode(), _hs.sha256).hexdigest()[:16]
        if not _h.compare_digest(sig, expected):
            return None
        payload = _json.loads(_b64.urlsafe_b64decode(data + "=="))
        if payload.get("exp", 0) < _time.time():
            return None
        return payload
    except Exception:
        return None

@app.post("/api/auth/login")
def login(payload: LoginPayload) -> dict:
    user = _USERS.get(payload.email.lower())
    if not user or user["hash"] != _hash_pw(payload.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = _make_token(payload.email, user["role"], user["name"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"email": payload.email, "name": user["name"], "role": user["role"]},
    }

@app.get("/api/auth/me")
def me(authorization: str | None = None) -> dict:
    from fastapi import Header
    return {"status": "ok"}

@app.post("/api/auth/logout")
def logout() -> dict:
    return {"status": "logged_out"}


# ── Dev-only seed endpoints ────────────────────────────────────────────────────
class SeedEventsPayload(BaseModel):
    events: list[dict[str, Any]]


class SeedHITLPayload(BaseModel):
    requests: list[dict[str, Any]]


@app.post("/api/dev/seed-events")
async def seed_events(payload: SeedEventsPayload) -> dict[str, Any]:
    """Load audit events from the seed script. Dev/demo only."""
    created = 0
    for e in payload.events:
        try:
            evt_type = EventType(e["event_type"])
            severity = EventSeverity(e.get("severity", "info"))
            audit_logger.log(
                evt_type,
                e["agent_id"],
                e["correlation_id"],
                payload=e.get("payload", {}),
                severity=severity,
            )
            created += 1
        except Exception:
            pass
    await broadcast({"type": "audit_seeded", "count": created})
    return {"created": created}


@app.post("/api/dev/seed-hitl")
async def seed_hitl(payload: SeedHITLPayload) -> dict[str, Any]:
    """Create pending HITL approval requests. Dev/demo only."""
    created = 0
    for r in payload.requests:
        req = ApprovalRequest.create(
            agent_id=r["agent_id"],
            correlation_id=r["correlation_id"],
            action=r["action"],
            resource=r["resource"],
            context=r.get("context", {}),
            timeout_seconds=7200,
        )
        hitl_orchestrator._pending[req.request_id] = req
        created += 1
    await broadcast({"type": "hitl_seeded", "count": created})
    return {"created": created}


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
    reason = payload.reason or "Approved via AgentForge UI"

    # Write to DB FIRST — the workflow polling loop will see this within 1s
    await sqlite_store.resolve_hitl(request_id, True, payload.reviewer_id, reason)
    # Also call decide() so the HITL panel removes the request (safe without a Future)
    decision = ApprovalDecision(
        request_id=request_id, reviewer_id=payload.reviewer_id,
        approved=True, reason=reason,
    )
    await hitl_orchestrator.decide(decision)

    audit_logger.log(EventType.HITL_APPROVED, req.agent_id, req.correlation_id,
                     payload={"reviewer": payload.reviewer_id, "reason": reason},
                     severity=EventSeverity.INFO)
    await broadcast({
        "type": "hitl_approved", "request_id": request_id,
        "agent_id": req.agent_id, "action": req.action,
    })
    return {"status": "approved", "request_id": request_id}


@app.post("/api/hitl/{request_id}/reject")
async def reject_hitl(request_id: str, payload: RejectPayload) -> dict[str, Any]:
    req = hitl_orchestrator.get_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    # Write to DB FIRST — the workflow polling loop will see this within 1s
    await sqlite_store.resolve_hitl(request_id, False, payload.reviewer_id, payload.reason)
    decision = ApprovalDecision(
        request_id=request_id, reviewer_id=payload.reviewer_id,
        approved=False, reason=payload.reason,
    )
    await hitl_orchestrator.decide(decision)

    audit_logger.log(EventType.HITL_REJECTED, req.agent_id, req.correlation_id,
                     payload={"reviewer": payload.reviewer_id, "reason": payload.reason},
                     severity=EventSeverity.WARNING)
    await broadcast({
        "type": "hitl_rejected", "request_id": request_id,
        "agent_id": req.agent_id, "action": req.action, "reason": payload.reason,
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


@app.get("/workflow")
def serve_workflow() -> FileResponse:
    page = STATIC_DIR / "workflow.html"
    if page.exists():
        return FileResponse(str(page))
    return HTMLResponse("<h1>Workflow Designer</h1><p>workflow.html not found in ui/static/</p>")


# ── Workflow Execution Engine ─────────────────────────────────────────────────

class StepResult(BaseModel):
    node_id: str
    node_type: str
    node_name: str
    status: str = "pending"          # pending | running | waiting_hitl | completed | failed | skipped
    started_at: float = 0.0
    completed_at: float | None = None
    duration_ms: float | None = None
    output: dict[str, Any] = {}


class WorkflowRun(BaseModel):
    run_id: str
    workflow_id: str
    workflow_name: str
    status: str = "running"          # running | waiting_hitl | completed | failed
    steps: list[StepResult] = []
    current_node_id: str | None = None
    started_at: float = 0.0
    completed_at: float | None = None
    error: str | None = None
    # Execution context snapshot — updated after every node so the UI can show
    # the live data flowing through the workflow (e.g. extracted invoice fields)
    context_snapshot: dict[str, Any] = {}
    # Initial input data supplied to the /execute endpoint
    input_data: dict[str, Any] = {}


_workflow_runs: dict[str, WorkflowRun] = {}


@app.on_event("startup")
async def _startup() -> None:
    """
    Async startup handler — runs before the first request is served.

    1. Creates DB tables (PostgreSQL on Railway, SQLite locally).
    2. Loads persisted runs from the DB into the in-memory cache.
    3. Marks any run that was still executing when the server last stopped
       as failed, so the UI doesn't show a stuck spinner.
    """
    await sqlite_store.init_db()
    for _saved in await sqlite_store.list_runs():
        try:
            _r = WorkflowRun(**_saved)
            if _r.status in ("running", "waiting_hitl"):
                _r.status = "failed"
                _r.error = "Server restarted — run was interrupted"
                _r.completed_at = _r.completed_at or time.time()
                await sqlite_store.upsert_run(_r.run_id, _r.workflow_id, _r.model_dump())
            _workflow_runs[_r.run_id] = _r
        except Exception:
            pass

async def _run_workflow(run_id: str, input_data: dict[str, Any] | None = None) -> None:
    """Background task: walks the workflow DAG and executes each node."""
    from ui.backend.routes.workflows import _store as _wf_store
    from ui.backend.engine import ExecutionContext, run_node as _engine_run_node

    run = _workflow_runs.get(run_id)
    if not run:
        return

    wf = _wf_store.get(run.workflow_id)
    if not wf:
        run.status = "failed"
        run.error = "Workflow definition not found"
        return

    # ── Execution context — shared mutable state flowing through the DAG ──────
    ctx = ExecutionContext(
        run_id=run_id,
        workflow_id=run.workflow_id,
        workflow_name=run.workflow_name,
        data=dict(input_data or run.input_data or {}),
    )

    # Build adjacency: node_id → outgoing edges
    edges_from: dict[str, list[Any]] = {}
    for e in wf.edges:
        edges_from.setdefault(e.from_node, []).append(e)

    # Find entry nodes (no incoming edges)
    all_targets = {e.to_node for e in wf.edges}
    entry_nodes = [n.node_id for n in wf.nodes if n.node_id not in all_targets]
    if not entry_nodes:
        entry_nodes = [wf.nodes[0].node_id] if wf.nodes else []

    node_map = {n.node_id: n for n in wf.nodes}
    visited: set[str] = set()

    async def execute_node(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)

        node = node_map.get(node_id)
        if not node:
            return

        run.current_node_id = node_id
        step = StepResult(
            node_id=node_id, node_type=node.node_type,
            node_name=node.name, status="running", started_at=time.time(),
        )
        run.steps.append(step)

        # Log start to audit trail
        audit_logger.log(
            EventType.AGENT_STARTED,
            f"wf:{run.workflow_id}",
            run_id,
            payload={"node": node.name, "node_type": node.node_type, "workflow": run.workflow_name},
        )
        await broadcast({"type": "wf_step", "run_id": run_id,
                         "node_id": node_id, "node_name": node.name,
                         "node_type": node.node_type, "status": "running"})

        next_port: str | None = None

        try:
            if node.node_type == "hitl":
                # ── HITL — DB-polling approach (survives Railway restarts) ──────
                step.status = "waiting_hitl"
                run.status = "waiting_hitl"

                # 1. Create the approval request manually (no asyncio.Future).
                #    Register in _pending so it shows in the HITL Approvals panel.
                req = ApprovalRequest.create(
                    agent_id=f"wf:{run.workflow_id}",
                    correlation_id=run_id,
                    action=node.name,
                    resource=f"workflow/{run.workflow_name}",
                    context={"workflow": run.workflow_name, "node": node.name,
                             "run_id": run_id, **node.config},
                    timeout_seconds=3600,
                )
                hitl_orchestrator._pending[req.request_id] = req

                # 2. Persist the pending request to the DB immediately.
                #    The workflow background task will poll this table.
                await sqlite_store.create_hitl_pending(req.request_id, run_id, node_id)

                # 3. Store request_id in step output NOW (before the await) so
                #    the frontend can show inline Approve/Reject buttons.
                step.output = {
                    "hitl_request_id": req.request_id,
                    "action": node.name,
                    "node": node.name,
                }

                # 4. Persist run state (includes hitl_request_id in step.output).
                await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())

                # 5. Log the HITL request to the audit trail.
                audit_logger.log(
                    EventType.HITL_REQUESTED,
                    f"wf:{run.workflow_id}",
                    run_id,
                    payload={
                        "node": node.name,
                        "action": node.name,
                        "workflow": run.workflow_name,
                        "request_id": req.request_id,
                        **{k: v for k, v in node.config.items() if k != "Expression"},
                    },
                    severity=EventSeverity.WARNING,
                )
                # Broadcast includes request_id so WS clients can act on it
                await broadcast({
                    "type": "wf_step", "run_id": run_id, "node_id": node_id,
                    "status": "waiting_hitl", "hitl_request_id": req.request_id,
                })

                # 6. Poll SQLite until a decision is recorded (1s interval).
                #    This loop survives server restarts unlike asyncio.Future.
                decision = await sqlite_store.wait_for_decision(
                    req.request_id, timeout_seconds=3600
                )

                # 7. Clean up from HITL panel.
                hitl_orchestrator._pending.pop(req.request_id, None)

                run.status = "running"
                step.output.update({
                    "approved": decision["approved"],
                    "reviewer": decision["reviewer_id"],
                    "reason": decision["reason"],
                })

                # 8. Log resumption — explicit event so audit trail shows continuation.
                audit_logger.log(
                    EventType.AGENT_STARTED,
                    f"wf:{run.workflow_id}",
                    run_id,
                    payload={"event": "hitl_resumed", "node": node.name,
                             "reviewer": decision["reviewer_id"],
                             "approved": decision["approved"]},
                )
                await broadcast({
                    "type": "wf_hitl_resolved", "run_id": run_id, "node_id": node_id,
                    "approved": decision["approved"], "reviewer": decision["reviewer_id"],
                })

                # 9. Only log APPROVED/REJECTED here for system timeouts.
                #    Human decisions are already logged by the /approve and /reject endpoints.
                is_system = decision["reviewer_id"] == "system"

                if not decision["approved"]:
                    step.status = "failed"
                    step.completed_at = time.time()
                    run.status = "failed"
                    run.error = f"HITL rejected at '{node.name}': {decision['reason']}"
                    if is_system:
                        audit_logger.log(
                            EventType.HITL_REJECTED,
                            f"wf:{run.workflow_id}",
                            run_id,
                            payload={"node": node.name, "reason": decision["reason"],
                                     "via": "timeout"},
                            severity=EventSeverity.WARNING,
                        )
                    await broadcast({"type": "wf_step", "run_id": run_id,
                                     "node_id": node_id, "status": "failed"})
                    return

                if is_system:
                    audit_logger.log(
                        EventType.HITL_APPROVED,
                        f"wf:{run.workflow_id}",
                        run_id,
                        payload={"node": node.name, "via": "timeout_auto_approve"},
                    )

                # Annotate context with the HITL decision so downstream nodes
                # can branch on it (e.g. ctx.hitl_approved == True)
                ctx.set("hitl_approved", decision["approved"])
                ctx.set("hitl_reviewer", decision["reviewer_id"])
                run.context_snapshot = ctx.snapshot()

            else:
                # ── Real execution via engine ──────────────────────────────
                result = await _engine_run_node(node, ctx, run_id)

                if result.error:
                    raise RuntimeError(result.error)

                step.output = result.output
                next_port = result.next_port
                # Update live context snapshot — persisted by upsert_run below
                run.context_snapshot = ctx.snapshot()

        except Exception as exc:
            step.status = "failed"
            step.completed_at = time.time()
            run.status = "failed"
            run.error = str(exc)
            await broadcast({"type": "wf_step", "run_id": run_id,
                             "node_id": node_id, "status": "failed"})
            return

        # Mark step complete
        step.status = "completed"
        step.completed_at = time.time()
        step.duration_ms = round((step.completed_at - step.started_at) * 1000)

        # Truncate large outputs to keep audit events compact
        _audit_output = {k: v for k, v in step.output.items() if not k.startswith("_")}
        audit_logger.log(
            EventType.TOOL_COMPLETED,
            f"wf:{run.workflow_id}",
            run_id,
            payload={"node": node.name, "node_type": node.node_type,
                     "duration_ms": step.duration_ms, **_audit_output},
        )
        await broadcast({"type": "wf_step", "run_id": run_id,
                         "node_id": node_id, "status": "completed",
                         "duration_ms": step.duration_ms})

        # Persist step completion to DB
        await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())

        # Follow outgoing edges
        out_edges = edges_from.get(node_id, [])
        if node.node_type == "condition":
            out_edges = [e for e in out_edges if e.port == next_port]

        for edge in out_edges:
            if run.status == "failed":
                break
            await execute_node(edge.to_node)

    try:
        for entry in entry_nodes:
            await execute_node(entry)

        if run.status not in ("failed",):
            run.status = "completed"
        run.completed_at = time.time()

    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        run.completed_at = time.time()

    # Persist final run state to DB
    await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())
    await broadcast({"type": "wf_run_done", "run_id": run_id,
                     "status": run.status, "workflow_name": run.workflow_name})


class ExecuteWorkflowPayload(BaseModel):
    """Optional body for POST /api/workflows/{id}/execute.

    ``input_data`` seeds the ExecutionContext so nodes can reference upstream
    values immediately.  Example::

        {"input_data": {"invoice_id": "INV-001", "amount_usd": 25000}}
    """
    input_data: dict[str, Any] = {}


@app.post("/api/workflows/{workflow_id}/execute")
async def execute_workflow(
    workflow_id: str,
    background_tasks: BackgroundTasks,
    payload: ExecuteWorkflowPayload | None = None,
) -> dict[str, Any]:
    if payload is None:
        payload = ExecuteWorkflowPayload()
    from ui.backend.routes.workflows import _store as _wf_store

    wf = _wf_store.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run = WorkflowRun(
        run_id=run_id,
        workflow_id=workflow_id,
        workflow_name=wf.name,
        status="running",
        started_at=time.time(),
        input_data=payload.input_data,
    )
    _workflow_runs[run_id] = run

    # Persist the initial run state to the DB
    await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())

    # Log workflow start to audit trail
    audit_logger.log(
        EventType.AGENT_STARTED,
        f"wf:{workflow_id}",
        run_id,
        payload={"workflow": wf.name, "version": wf.version, "nodes": len(wf.nodes),
                 "input_keys": list(payload.input_data.keys())},
    )

    background_tasks.add_task(_run_workflow, run_id, payload.input_data)
    return {"run_id": run_id, "status": "started"}


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
    _root = Path(__file__).parent.parent.parent
    print("\n★  AgentForge Enterprise UI  ★")
    print("   http://localhost:8000\n")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        app_dir=str(Path(__file__).parent),
        # Only watch source dirs — never .venv
        reload_dirs=[
            str(_root / "agentforge"),
            str(_root / "ui" / "backend"),
            str(_root / "ui" / "static"),
        ],
        reload_excludes=["*.pyc", "__pycache__", ".venv", "*.egg-info"],
    )
