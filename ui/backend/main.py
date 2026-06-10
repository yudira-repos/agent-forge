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
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
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
    from ui.backend.observability.otel import otel_status
    return {"status": "ok", "version": "0.1.0", "otel": otel_status()}

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

# Merge workflow-specific agents (Claude + OpenAI) into the main registry
# so they appear in /api/agents and the dashboard.
try:
    from ui.backend.agent_registry import registry as _wf_registry
    for _wf_m in _wf_registry.list_all():
        registry.register(_wf_m)
except Exception:
    pass


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
    runtime_adapter: str = "anthropic"   # "anthropic" | "openai" | "vertex"
    # Agent-specific config stored in manifest.metadata
    # For Anthropic: model, system_prompt, output_fields, max_tokens
    # For OpenAI:    model, system_prompt, output_fields, max_tokens
    config: dict[str, Any] = {}
    # Optional capability declarations
    capabilities: list[dict[str, Any]] = []


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
    """
    Register a new agent manifest.

    For Anthropic agents supply ``config`` with::

        {"model": "claude-haiku-4-5-20251001", "system_prompt": "...",
         "output_fields": "field_a, field_b", "max_tokens": 1024,
         "cost_per_1k_input": 0.00025, "cost_per_1k_output": 0.00125}

    For OpenAI agents::

        {"model": "gpt-4o-mini", "system_prompt": "...",
         "output_fields": "field_a, field_b",
         "cost_per_1k_input": 0.00015, "cost_per_1k_output": 0.00060}

    The runner looks up the manifest by ``Agent ID`` from the workflow node
    config and uses these values automatically — no API-key sniffing needed.
    """
    caps = [
        AgentCapability(
            name=c.get("name", "unnamed"),
            description=c.get("description", ""),
            tags=c.get("tags", []),
            requires_hitl=bool(c.get("requires_hitl", False)),
            idempotent=bool(c.get("idempotent", True)),
        )
        for c in payload.capabilities
    ]
    manifest = AgentManifest(
        agent_id=payload.agent_id,
        name=payload.name,
        version=payload.version,
        description=payload.description,
        owner=payload.owner,
        tags=payload.tags,
        runtime_adapter=payload.runtime_adapter,
        capabilities=caps,
        metadata=dict(payload.config),
    )
    # Register in both the main UI registry and the workflow runner registry
    registry.register(manifest)
    try:
        from ui.backend.agent_registry import registry as _wf_registry
        _wf_registry.register(manifest)
    except Exception:
        pass
    audit_logger.log(EventType.AGENT_REGISTERED, payload.agent_id, str(uuid.uuid4()),
                     payload={"name": payload.name, "version": payload.version,
                              "runtime_adapter": payload.runtime_adapter})
    await broadcast({"type": "agent_registered", "agent_id": payload.agent_id, "name": payload.name,
                     "runtime_adapter": payload.runtime_adapter})
    return manifest.to_dict()


@app.patch("/api/agents/{agent_id}/deprecate")
async def deprecate_agent(agent_id: str) -> dict[str, Any]:
    if not registry.deprecate(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    await broadcast({"type": "agent_deprecated", "agent_id": agent_id})
    return {"status": "deprecated"}


@app.get("/api/agents/{agent_id}/metrics")
def get_agent_metrics_by_id(agent_id: str) -> dict[str, Any]:
    """
    Return OTEL-derived execution metrics for a specific agent.

    Includes: invocation count, avg latency, token usage, estimated cost,
    error rate, and the 20 most recent spans.
    """
    from ui.backend.observability.otel import get_agent_metrics, get_run_spans
    manifest = registry.get(agent_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Agent not found")
    all_metrics = get_agent_metrics()
    agent_met = next((m for m in all_metrics if m["agent_id"] == agent_id), {
        "agent_id": agent_id, "agent_name": manifest.name,
        "backend": manifest.runtime_adapter, "model": manifest.metadata.get("model", ""),
        "invocations": 0, "successes": 0, "failures": 0,
        "avg_latency_ms": 0, "total_tokens_in": 0, "total_tokens_out": 0,
        "total_cost_usd": 0, "error_rate": 0,
    })
    return {
        **agent_met,
        "manifest": manifest.to_dict(),
    }


# ── Routes: OTEL Metrics ───────────────────────────────────────────────────────

@app.get("/api/metrics/agents")
async def get_all_agent_metrics() -> dict[str, Any]:
    """
    Aggregated execution metrics for every agent that has run.

    Merges in-memory (current session, real-time) with DB (historical,
    survives restarts).  DB rows take precedence for counts — in-memory
    only adds agents not yet flushed.
    """
    from ui.backend.observability.otel import get_agent_metrics, otel_status
    from ui.backend.db import sqlite_store

    # DB aggregates (persistent — full history)
    try:
        db_metrics = await sqlite_store.get_agent_metrics_from_db()
    except Exception:
        db_metrics = []

    # In-memory aggregates (current session only — real-time)
    mem_metrics = get_agent_metrics()

    # Merge: DB is authoritative; in-memory fills gaps for newly started agents
    db_ids = {m["agent_id"] for m in db_metrics}
    merged = db_metrics + [m for m in mem_metrics if m["agent_id"] not in db_ids]

    return {
        "agents": merged,
        "otel": otel_status(),
    }


@app.get("/api/metrics/recent")
async def get_recent_spans() -> list[dict[str, Any]]:
    """Return the 50 most recent agent spans across all runs (DB-backed)."""
    from ui.backend.db import sqlite_store
    try:
        return await sqlite_store.get_recent_spans_from_db(limit=50)
    except Exception:
        from ui.backend.observability.otel import get_recent_spans as _mem
        return _mem(limit=50)


@app.get("/api/traces/{run_id}")
async def get_run_trace(run_id: str) -> dict[str, Any]:
    """
    Return the full OTEL trace for a workflow run.

    Reads from the DB (persistent) and falls back to in-memory for the
    current session if the DB has no rows yet.
    """
    from ui.backend.db import sqlite_store
    from ui.backend.observability.otel import get_run_spans as _mem_spans

    try:
        db_spans = await sqlite_store.get_spans_for_run(run_id)
    except Exception:
        db_spans = []

    # Fall back to in-memory if DB has nothing (e.g. span not yet flushed)
    spans = db_spans if db_spans else _mem_spans(run_id)

    run = _workflow_runs.get(run_id)
    return {
        "run_id":        run_id,
        "workflow_id":   run.workflow_id   if run else "unknown",
        "workflow_name": run.workflow_name if run else "unknown",
        "status":        run.status        if run else "unknown",
        "spans":         spans,
        "span_count":    len(spans),
        "total_tokens":  sum((s.get("tokens_in", 0) + s.get("tokens_out", 0)) for s in spans),
        "total_cost_usd": round(sum(s.get("cost_usd", 0) for s in spans), 6),
        "total_latency_ms": round(sum(s.get("latency_ms", 0) for s in spans), 1),
    }


# ── Routes: Live Orchestration (SSE) ──────────────────────────────────────────

@app.get("/api/orchestration/live")
async def orchestration_live_sse() -> StreamingResponse:
    """
    Server-Sent Events stream of live orchestration state.

    The client receives one JSON event every 2 seconds containing:
      - Active workflow runs (started in the last 60 seconds)
      - Per-agent execution data for each run
      - Current aggregated metrics

    Connect from JS:
        const es = new EventSource('/api/orchestration/live');
        es.onmessage = e => console.log(JSON.parse(e.data));
    """
    import asyncio

    async def event_generator():
        while True:
            from ui.backend.observability.otel import get_live_runs, get_agent_metrics
            active_runs = []
            for run in sorted(
                _workflow_runs.values(),
                key=lambda r: r.started_at,
                reverse=True,
            )[:10]:
                from ui.backend.observability.otel import get_run_spans
                spans = get_run_spans(run.run_id)
                active_runs.append({
                    "run_id":       run.run_id,
                    "workflow_id":  run.workflow_id,
                    "workflow_name": run.workflow_name,
                    "status":       run.status,
                    "engine":       run.engine,
                    "started_at":   run.started_at,
                    "current_node": run.current_node_id,
                    "step_count":   len(run.steps),
                    "span_count":   len(spans),
                    "total_tokens": sum(s["tokens_in"] + s["tokens_out"] for s in spans),
                    "total_cost_usd": round(sum(s["cost_usd"] for s in spans), 6),
                    "agents_used":  list({s["agent_id"] for s in spans}),
                    "recent_spans": spans[-5:],  # last 5 spans for live timeline
                })
            payload = json.dumps({
                "ts":          time.time(),
                "active_runs": active_runs,
                "agent_metrics": get_agent_metrics(),
            })
            yield f"data: {payload}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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


def _hitl_audit_ctx(request_id: str) -> tuple[str, str, str]:
    """
    Derive agent_id / correlation_id / action for audit logging when the
    ApprovalRequest is not in _pending (e.g. after a server restart).

    Scans the in-memory run cache looking for a step whose output contains
    the matching hitl_request_id.
    """
    for run in _workflow_runs.values():
        for step in run.steps:
            if step.output.get("hitl_request_id") == request_id:
                return f"wf:{run.workflow_id}", run.run_id, step.node_name
    return "wf:unknown", request_id, "approval"


@app.post("/api/hitl/{request_id}/approve")
async def approve_hitl(
    request_id: str, payload: ApprovePayload, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    # req may be None after a server restart — proceed regardless.
    req = hitl_orchestrator.get_request(request_id)
    reason = payload.reason or "Approved via AgentForge UI"
    decision_dict = {"approved": True, "reviewer_id": payload.reviewer_id, "reason": reason}

    # Always write to DB (custom engine polls this; also used for audit recovery)
    await sqlite_store.resolve_hitl(request_id, True, payload.reviewer_id, reason)

    # Update in-memory orchestrator (removes from HITL panel)
    orc_decision = ApprovalDecision(
        request_id=request_id, reviewer_id=payload.reviewer_id,
        approved=True, reason=reason,
    )
    if req:
        await hitl_orchestrator.decide(orc_decision)
        agent_id, correlation_id, action = req.agent_id, req.correlation_id, req.action
    else:
        agent_id, correlation_id, action = _hitl_audit_ctx(request_id)

    # LangGraph path: resume the paused StateGraph in a background task
    run = _get_run_by_hitl_request(request_id)
    if run and run.engine == "langgraph":
        hitl_orchestrator._pending.pop(request_id, None)
        background_tasks.add_task(_resume_lg_run, run, decision_dict)

    audit_logger.log(EventType.HITL_APPROVED, agent_id, correlation_id,
                     payload={"reviewer": payload.reviewer_id, "reason": reason},
                     severity=EventSeverity.INFO)
    await broadcast({
        "type": "hitl_approved", "request_id": request_id,
        "agent_id": agent_id, "action": action,
    })
    return {"status": "approved", "request_id": request_id}


@app.post("/api/hitl/{request_id}/reject")
async def reject_hitl(
    request_id: str, payload: RejectPayload, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    req = hitl_orchestrator.get_request(request_id)
    decision_dict = {"approved": False, "reviewer_id": payload.reviewer_id, "reason": payload.reason}

    await sqlite_store.resolve_hitl(request_id, False, payload.reviewer_id, payload.reason)

    orc_decision = ApprovalDecision(
        request_id=request_id, reviewer_id=payload.reviewer_id,
        approved=False, reason=payload.reason,
    )
    if req:
        await hitl_orchestrator.decide(orc_decision)
        agent_id, correlation_id, action = req.agent_id, req.correlation_id, req.action
    else:
        agent_id, correlation_id, action = _hitl_audit_ctx(request_id)

    # LangGraph path: resume with rejection decision
    run = _get_run_by_hitl_request(request_id)
    if run and run.engine == "langgraph":
        hitl_orchestrator._pending.pop(request_id, None)
        background_tasks.add_task(_resume_lg_run, run, decision_dict)

    audit_logger.log(EventType.HITL_REJECTED, agent_id, correlation_id,
                     payload={"reviewer": payload.reviewer_id, "reason": payload.reason},
                     severity=EventSeverity.WARNING)
    await broadcast({
        "type": "hitl_rejected", "request_id": request_id,
        "agent_id": agent_id, "action": action, "reason": payload.reason,
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


@app.get("/orchestration")
def serve_orchestration() -> FileResponse:
    """Live orchestration dashboard — agent metrics + real-time execution timeline."""
    page = STATIC_DIR / "orchestration.html"
    if page.exists():
        return FileResponse(str(page))
    return HTMLResponse("<h1>Orchestration Dashboard</h1><p>orchestration.html not found.</p>")


@app.get("/escalation")
def serve_escalation() -> FileResponse:
    """Agent escalation review queue — human-in-the-loop classification."""
    page = STATIC_DIR / "escalation.html"
    if page.exists():
        return FileResponse(str(page))
    return HTMLResponse("<h1>Escalations</h1><p>escalation.html not found.</p>")


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
    # Which execution engine ran this workflow
    engine: str = "custom"           # "custom" | "langgraph"


_workflow_runs: dict[str, WorkflowRun] = {}


# ── HITL restart-recovery helpers ────────────────────────────────────────────

def _apply_saved_hitl_decision(
    run: "WorkflowRun", step: "StepResult", decision: dict[str, Any]
) -> None:
    """Apply an already-decided HITL outcome loaded from DB during startup."""
    step.output.update({
        "approved":  decision["approved"],
        "reviewer":  decision.get("reviewer_id", "unknown"),
        "reason":    decision.get("reason", ""),
    })
    step.status = "completed" if decision["approved"] else "failed"
    step.completed_at = time.time()
    if decision["approved"]:
        run.status = "completed"
        run.completed_at = time.time()
    else:
        run.status = "failed"
        run.error = f"HITL rejected: {decision.get('reason', '')}"
        run.completed_at = time.time()


def _restore_hitl_request(
    run: "WorkflowRun", step: "StepResult", request_id: str
) -> None:
    """
    Re-add a HITL request to the in-memory orchestrator after a server restart.

    ApprovalRequest.create() generates a new UUID internally, but we store it
    in _pending under the ORIGINAL request_id (from the DB) so that
    ``hitl_orchestrator.get_request(request_id)`` still finds it.
    """
    req = ApprovalRequest.create(
        agent_id=f"wf:{run.workflow_id}",
        correlation_id=run.run_id,
        action=step.node_name,
        resource=f"workflow/{run.workflow_name}",
        context={
            "run_id": run.run_id,
            "node": step.node_name,
            "workflow": run.workflow_name,
            "_restored_after_restart": True,
        },
        timeout_seconds=3600,
    )
    hitl_orchestrator._pending[request_id] = req


async def _wait_and_apply_hitl(run_id: str, request_id: str) -> None:
    """
    Post-restart HITL recovery task.

    The original ``execute_node`` background task is gone after a restart.
    This lightweight coroutine polls the DB for the decision and applies it
    to the in-memory run state when a human approves or rejects.

    Limitation: post-HITL DAG nodes are NOT re-executed after restart — the
    run is marked completed/failed at the HITL boundary.  Full DAG resumption
    requires persisted execution-graph state and is planned as a future
    improvement.
    """
    run = _workflow_runs.get(run_id)
    if not run:
        return

    try:
        decision = await sqlite_store.wait_for_decision(request_id, timeout_seconds=3600)
    except Exception as exc:
        decision = {
            "approved": False,
            "reviewer_id": "system",
            "reason": f"Recovery error: {exc}",
        }

    # Remove from HITL panel
    hitl_orchestrator._pending.pop(request_id, None)

    hitl_step = next((s for s in run.steps if s.status == "waiting_hitl"), None)
    if hitl_step:
        _apply_saved_hitl_decision(run, hitl_step, decision)
    else:
        # Fallback — no waiting step found
        run.status = "completed" if decision["approved"] else "failed"
        run.completed_at = time.time()

    audit_event = EventType.HITL_APPROVED if decision["approved"] else EventType.HITL_REJECTED
    audit_logger.log(
        audit_event,
        f"wf:{run.workflow_id}",
        run_id,
        payload={"reviewer": decision["reviewer_id"], "via": "restart_recovery"},
        severity=EventSeverity.INFO if decision["approved"] else EventSeverity.WARNING,
    )
    await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())
    await broadcast({
        "type": "wf_hitl_resolved",
        "run_id": run_id,
        "approved": decision["approved"],
        "reviewer": decision["reviewer_id"],
    })


@app.on_event("startup")
async def _startup() -> None:
    """
    Async startup handler — runs before the first request is served.

    1. Creates DB tables (PostgreSQL on Railway, SQLite locally).
    2. Initialises the LangGraph checkpointer (if langgraph is installed).
    3. Loads persisted runs from the DB into the in-memory cache.
    4. ``running`` runs cannot be resumed — marked failed.
    5. ``waiting_hitl`` runs are recovered:
         - Already decided in DB  → apply decision, mark completed/failed.
         - Still pending           → restore to _pending, start recovery task
                                     so Approve/Reject keeps working.
    """
    await sqlite_store.init_db()

    # Initialise LangGraph checkpointer (no-op if langgraph not installed)
    from ui.backend.engine import lg_engine as _lg
    await _lg.setup_checkpointer()
    for _saved in await sqlite_store.list_runs():
        try:
            _r = WorkflowRun(**_saved)

            if _r.status == "running":
                # Can't resume mid-execution — mark failed
                _r.status = "failed"
                _r.error = "Server restarted — run was interrupted"
                _r.completed_at = _r.completed_at or time.time()
                await sqlite_store.upsert_run(_r.run_id, _r.workflow_id, _r.model_dump())

            elif _r.status == "waiting_hitl":
                # Try to recover the HITL run
                _hitl_step = next(
                    (s for s in _r.steps if s.status == "waiting_hitl"), None
                )
                _request_id = (
                    _hitl_step.output.get("hitl_request_id") if _hitl_step else None
                )

                if _request_id:
                    _decision = await sqlite_store.get_hitl_decision(_request_id)
                    if _decision is not None:
                        # Decision was already made before the restart — apply immediately
                        _apply_saved_hitl_decision(_r, _hitl_step, _decision)
                        await sqlite_store.upsert_run(_r.run_id, _r.workflow_id, _r.model_dump())
                    else:
                        # Still waiting for a human — restore _pending and start
                        # a recovery task that will resume once the user clicks Approve/Reject
                        _restore_hitl_request(_r, _hitl_step, _request_id)
                        asyncio.create_task(_wait_and_apply_hitl(_r.run_id, _request_id))
                else:
                    # Can't identify the HITL request — mark failed
                    _r.status = "failed"
                    _r.error = "Server restarted — HITL request details unavailable"
                    _r.completed_at = time.time()
                    await sqlite_store.upsert_run(_r.run_id, _r.workflow_id, _r.model_dump())

            _workflow_runs[_r.run_id] = _r
        except Exception:
            pass

# ── LangGraph execution helpers ───────────────────────────────────────────────

def _get_run_by_hitl_request(request_id: str) -> "WorkflowRun | None":
    """Find the WorkflowRun that has this HITL request_id in a step's output."""
    for run in _workflow_runs.values():
        for step in run.steps:
            if step.output.get("hitl_request_id") == request_id:
                return run
    return None


def _apply_lg_completion(run: "WorkflowRun", wf: Any, lg_state: dict[str, Any]) -> None:
    """Sync a WorkflowRun from a completed (non-interrupted) LangGraph result."""
    node_map = {n.node_id: n for n in wf.nodes}
    ts = time.time()
    for nid in lg_state.get("completed_nodes", []):
        wf_node = node_map.get(nid)
        if not wf_node:
            continue
        run.steps.append(StepResult(
            node_id=nid, node_type=wf_node.node_type, node_name=wf_node.name,
            status="completed", started_at=run.started_at, completed_at=ts,
            duration_ms=round((ts - run.started_at) * 1000),
        ))
    run.context_snapshot = dict(lg_state.get("data") or {})
    run.error = lg_state.get("error") or None
    run.status = "failed" if run.error else "completed"
    run.completed_at = ts


def _apply_lg_hitl_pause(
    run: "WorkflowRun", wf: Any,
    interrupt_info: dict[str, Any],
    lg_state: dict[str, Any],
) -> None:
    """Sync a WorkflowRun from a LangGraph run paused at a HITL interrupt."""
    node_map   = {n.node_id: n for n in wf.nodes}
    completed  = set(lg_state.get("completed_nodes") or [])
    ts         = time.time()
    request_id = interrupt_info.get("request_id", f"req-{uuid.uuid4().hex[:8]}")

    # Build steps for all nodes that ran before the interrupt
    for nid in (lg_state.get("completed_nodes") or []):
        wf_node = node_map.get(nid)
        if not wf_node:
            continue
        run.steps.append(StepResult(
            node_id=nid, node_type=wf_node.node_type, node_name=wf_node.name,
            status="completed", started_at=run.started_at, completed_at=ts,
        ))

    # Add the paused HITL step
    hitl_node = next(
        (n for n in wf.nodes if n.node_type == "hitl" and n.node_id not in completed),
        None,
    )
    if hitl_node:
        run.steps.append(StepResult(
            node_id=hitl_node.node_id, node_type="hitl", node_name=hitl_node.name,
            status="waiting_hitl", started_at=ts,
            output={
                "hitl_request_id": request_id,
                "action": hitl_node.name,
                "node":   hitl_node.name,
            },
        ))

    run.context_snapshot = dict(lg_state.get("data") or {})
    run.status = "waiting_hitl"

    # Register in HITL panel so the approvals page shows it
    req = ApprovalRequest.create(
        agent_id=f"wf:{run.workflow_id}",
        correlation_id=run.run_id,
        action=interrupt_info.get("action", hitl_node.name if hitl_node else "approval"),
        resource=f"workflow/{run.workflow_name}",
        context={**interrupt_info.get("context", {}), "run_id": run.run_id},
        timeout_seconds=3600,
    )
    hitl_orchestrator._pending[request_id] = req

    # Also persist to DB so approve/reject works even if the process restarts
    # (non-blocking — fire-and-forget via asyncio)
    asyncio.create_task(
        sqlite_store.create_hitl_pending(request_id, run.run_id,
                                         hitl_node.node_id if hitl_node else "")
    )

    audit_logger.log(
        EventType.HITL_REQUESTED, f"wf:{run.workflow_id}", run.run_id,
        payload={"request_id": request_id, "action": req.action,
                 "workflow": run.workflow_name},
        severity=EventSeverity.WARNING,
    )


async def _run_workflow_lg(run_id: str, input_data: dict[str, Any] | None = None) -> None:
    """
    LangGraph execution path.

    Builds/retrieves a compiled StateGraph from the workflow definition,
    invokes it, and syncs the result back into the in-memory WorkflowRun.
    If the graph pauses at a HITL node, the run is marked ``waiting_hitl``
    and the approve/reject endpoint will call ``resume_workflow`` to continue.
    """
    from ui.backend.routes.workflows import _store as _wf_store
    from ui.backend.engine import lg_engine as _lg

    run = _workflow_runs.get(run_id)
    if not run:
        return

    wf = _wf_store.get(run.workflow_id)
    if not wf:
        run.status = "failed"
        run.error = "Workflow definition not found"
        await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())
        return

    run.engine = "langgraph"

    try:
        lg_state = await _lg.run_workflow(
            run_id, wf, dict(input_data or run.input_data or {})
        )
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        run.completed_at = time.time()
        await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())
        await broadcast({"type": "wf_run_done", "run_id": run_id,
                         "status": run.status, "workflow_name": run.workflow_name})
        return

    # Check if the graph paused at any interrupt (HITL or agent escalation)
    interrupt_info = _lg.get_interrupt_info(run_id, wf)
    if interrupt_info:
        interrupt_type = interrupt_info.get("type", "hitl")
        if interrupt_type == "agent_escalation":
            # Agent-level escalation: pause run, show in escalation review UI
            run.status = "waiting_agent_escalation"
            run.context_snapshot = dict(lg_state.get("data") or {})
            run.error = None
            await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())
            await broadcast({
                "type": "wf_step", "run_id": run_id,
                "status": "waiting_agent_escalation",
                "escalation_id": interrupt_info.get("escalation_id"),
                "agent_name":    interrupt_info.get("agent_name"),
                "reason":        interrupt_info.get("reason"),
            })
        else:
            _apply_lg_hitl_pause(run, wf, interrupt_info, lg_state)
            await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())
            await broadcast({
                "type": "wf_step", "run_id": run_id,
                "status": "waiting_hitl",
                "hitl_request_id": interrupt_info.get("request_id"),
            })
    else:
        _apply_lg_completion(run, wf, lg_state)
        await sqlite_store.upsert_run(run_id, run.workflow_id, run.model_dump())
        await broadcast({"type": "wf_run_done", "run_id": run_id,
                         "status": run.status, "workflow_name": run.workflow_name})


async def _resume_lg_run(run: "WorkflowRun", decision: dict[str, Any]) -> None:
    """
    Resume a LangGraph run paused at HITL after the user approves or rejects.

    Called from approve_hitl / reject_hitl when run.engine == "langgraph".
    """
    from ui.backend.routes.workflows import _store as _wf_store
    from ui.backend.engine import lg_engine as _lg

    wf = _wf_store.get(run.workflow_id)
    if not wf:
        return

    try:
        lg_state = await _lg.resume_workflow(run.run_id, wf, decision)
    except Exception as exc:
        run.status = "failed"
        run.error = f"LangGraph resume failed: {exc}"
        run.completed_at = time.time()
        await sqlite_store.upsert_run(run.run_id, run.workflow_id, run.model_dump())
        return

    # Check if paused again (unlikely for a single HITL node, but possible in multi-HITL graphs)
    interrupt_info = _lg.get_interrupt_info(run.run_id, wf)
    if interrupt_info:
        _apply_lg_hitl_pause(run, wf, interrupt_info, lg_state)
    else:
        # Merge post-HITL steps with existing steps
        pre_steps = [s for s in run.steps if s.status != "waiting_hitl"]
        run.steps = pre_steps
        _apply_lg_completion(run, wf, lg_state)
        # Mark the old HITL step as completed
        hitl_step = next((s for s in run.steps if s.node_type == "hitl"), None)
        if hitl_step:
            hitl_step.status = "completed"
            hitl_step.completed_at = time.time()

    await sqlite_store.upsert_run(run.run_id, run.workflow_id, run.model_dump())
    await broadcast({"type": "wf_run_done", "run_id": run.run_id,
                     "status": run.status, "workflow_name": run.workflow_name})


# ── Main workflow dispatcher ──────────────────────────────────────────────────

async def _run_workflow(run_id: str, input_data: dict[str, Any] | None = None) -> None:
    """
    Dispatch to LangGraph engine (preferred) or custom DAG walker (fallback).

    LangGraph is used when the ``langgraph`` package is installed.
    Install with:  pip install langgraph langchain-anthropic
    """
    from ui.backend.engine import lg_engine as _lg
    if _lg.is_available():
        await _run_workflow_lg(run_id, input_data)
    else:
        await _run_workflow_custom(run_id, input_data)


async def _run_workflow_custom(run_id: str, input_data: dict[str, Any] | None = None) -> None:
    """Custom DAG-walker fallback (used when langgraph is not installed)."""
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


# ── Agent Escalation endpoints ────────────────────────────────────────────────

@app.get("/api/escalations")
async def list_escalations() -> list[dict[str, Any]]:
    """Return all pending agent escalations (newest first)."""
    return await sqlite_store.list_pending_escalations()


@app.get("/api/escalations/{escalation_id}")
async def get_escalation(escalation_id: str) -> dict[str, Any]:
    """Return full context for a single escalation (includes conversation history)."""
    esc = await sqlite_store.get_escalation(escalation_id)
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    return esc


class EscalationResolutionPayload(BaseModel):
    """Human reviewer's decision for an agent escalation."""
    category: str
    notes: str = ""
    reviewer_id: str = "human-reviewer"


@app.post("/api/escalations/{escalation_id}/resolve")
async def resolve_escalation_endpoint(
    escalation_id: str,
    payload: EscalationResolutionPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    Human resolves an agent escalation.

    1. Load the escalation to find the associated run_id.
    2. Mark the escalation resolved in the DB.
    3. Resume the paused LangGraph run with the human's decision.
    """
    from ui.backend.engine import lg_engine as _lg
    from ui.backend.routes.workflows import _store as _wf_store

    esc = await sqlite_store.get_escalation(escalation_id)
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    if esc.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Escalation already {esc.get('status')}")

    run_id = esc["run_id"]
    run = _workflow_runs.get(run_id)

    # Mark the run as resuming (visible in the runs list immediately)
    if run:
        run.status = "running"

    decision = {
        "category":    payload.category,
        "notes":       payload.notes,
        "reviewer_id": payload.reviewer_id,
    }

    # Resolve in DB now (the lg_engine fire-and-forget will also call this — idempotent)
    await sqlite_store.resolve_escalation(escalation_id, decision, resolved_by=payload.reviewer_id)

    # Audit
    audit_logger.log(
        EventType.HITL_APPROVED,
        f"escalation:{escalation_id}",
        run_id,
        payload={
            "escalation_id": escalation_id,
            "category": payload.category,
            "reviewer": payload.reviewer_id,
            "notes": payload.notes,
            "agent": esc.get("agent_name", ""),
        },
    )

    # Resume LangGraph — runs in background so the HTTP response returns immediately
    if run and _lg.is_available():
        wf = _wf_store.get(run.workflow_id)
        if wf:
            background_tasks.add_task(_resume_lg_run, run, decision)

    await broadcast({
        "type":           "escalation_resolved",
        "escalation_id":  escalation_id,
        "run_id":         run_id,
        "category":       payload.category,
        "reviewer_id":    payload.reviewer_id,
    })

    return {"status": "resumed", "run_id": run_id, "category": payload.category}


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
