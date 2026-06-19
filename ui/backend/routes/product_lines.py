"""
Product Lines Router — /api/pl/*
=================================
Exposes the three product-line workflows (Health, Investment, Kids) as HTTP
endpoints so they can be triggered from the browser and their HITL queues can
be approved/rejected interactively.

Endpoints
---------
POST /api/pl/health/trigger      — start a health workflow (sample or custom)
POST /api/pl/investment/trigger  — start an investment workflow
POST /api/pl/kids/trigger        — start a kids-activities workflow
GET  /api/pl/hitl/pending        — all pending HITL requests across all 3 lines
POST /api/pl/hitl/{id}/approve   — approve a pending request
POST /api/pl/hitl/{id}/reject    — reject a pending request
GET  /api/pl/runs                — recent run summaries (all product lines)
GET  /api/pl/status              — registry health + queue depths

HITL bridge
-----------
When a product-line agent raises a HITL request, the notifier in the
HITLRegistry also injects it into the shared ``hitl_orchestrator`` that the
main server exposes at /api/hitl/pending — so it surfaces in BOTH the
product-line panel AND the existing HITL dashboard.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ── Make sure the examples/ directory is importable ──────────────────────────
_repo_root = Path(__file__).parents[3]
_examples_dir = _repo_root / "examples"
if str(_examples_dir) not in sys.path:
    sys.path.insert(0, str(_examples_dir))

from product_lines.health.workflow import PatientCase, run_health_workflow
from product_lines.investment.workflow import Portfolio, TradeOrder, TradeType, run_investment_workflow
from product_lines.kids.workflow import Child, run_kids_workflow
from product_lines.shared.hitl_registry import HITLRegistry, ProductLine
from agentforge.hitl import ApprovalDecision, ApprovalRequest, ApprovalStatus

router = APIRouter(prefix="/api/pl", tags=["product-lines"])

# ── Shared HITLRegistry (one instance for the life of the server) ─────────────
_pl_registry: HITLRegistry | None = None
# Live broadcasts: list of asyncio.Queue per connected WebSocket client
_pl_ws_clients: list[asyncio.Queue] = []
# In-memory run log
_runs: list[dict[str, Any]] = []


def _registry() -> HITLRegistry:
    """Lazy-init the HITLRegistry (called after main app is ready)."""
    global _pl_registry
    if _pl_registry is None:
        _pl_registry = _build_registry()
    return _pl_registry


async def _broadcast(msg: dict[str, Any]) -> None:
    for q in list(_pl_ws_clients):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _build_registry() -> HITLRegistry:
    """
    Build the HITLRegistry and patch each product line's notifier so that
    every HITL request is ALSO broadcast over the WebSocket.
    """
    from product_lines.shared import hitl_registry as _mod
    from product_lines.shared.hitl_registry import (
        HITLConfig, ProductLine,
        _health_escalation, _investment_escalation, _kids_escalation,
        _health_renderer, _investment_renderer, _kids_renderer,
    )

    reg = HITLRegistry()

    for pl, policy, renderer, channel, timeout in [
        (ProductLine.HEALTH,     _health_escalation(),     _health_renderer,     "slack:#clinical-approvals", 2400),
        (ProductLine.INVESTMENT, _investment_escalation(), _investment_renderer, "slack:#trade-approvals",    5400),
        (ProductLine.KIDS,       _kids_escalation(),       _kids_renderer,       "sms+push",                 5400),
    ]:
        config = HITLConfig(
            product_line=pl,
            escalation_policy=policy,
            context_renderer=renderer,
            notification_channel=channel,
            default_timeout_seconds=timeout,
        )
        reg.register(config)

    # Monkey-patch notifiers to add WebSocket broadcast
    for pl, orch in reg._orchestrators.items():
        _original_notify = orch._notify
        cfg = reg._configs[pl]

        async def _ws_notify(req: ApprovalRequest, group: str,
                             _pl=pl, _cfg=cfg, _orig=_original_notify) -> None:
            if _orig:
                await _orig(req, group)
            await _broadcast({
                "type": "hitl_pending",
                "product_line": _pl.value,
                "request_id": req.request_id,
                "action": req.action,
                "agent_id": req.agent_id,
                "context": req.context,
                "channel": _cfg.notification_channel,
                "reviewer_group": group,
                "created_at": req.created_at,
                "escalation_level": req.escalation_level,
                "summary": _cfg.render_context(req.context),
            })

        orch._notify = _ws_notify

    return reg


# ── Pydantic payloads ─────────────────────────────────────────────────────────

class HealthTriggerPayload(BaseModel):
    patient_id: str = "P-DEMO-001"
    age: int = 45
    symptoms: list[str] = ["chest pain", "difficulty breathing"]
    medical_history: list[str] = ["hypertension"]
    allergies: list[str] = []
    clinician_approves: bool | None = None   # None = real HITL (don't simulate)


class InvestmentTriggerPayload(BaseModel):
    account_id: str = "ACC-DEMO-001"
    total_value_usd: float = 250_000.0
    holdings: dict[str, float] = {
        "AAPL": 80_000.0, "MSFT": 60_000.0,
        "BND": 50_000.0,  "VTI": 60_000.0,
    }
    trade_type: str = "buy"
    ticker: str = "BND"
    amount_usd: float = 25_000.0
    rationale: str = "Rebalance equity-heavy portfolio toward fixed income"
    advisor_approves: bool | None = None


class KidsTriggerPayload(BaseModel):
    child_name: str = "Alex"
    child_age: int = 9
    allergies: list[str] = []
    existing_schedule: list[str] = []
    preferences: list[str] = ["sports"]
    activity_index: int = 0
    parent_approves: bool | None = None


class ApprovePayload(BaseModel):
    reviewer_id: str = "demo-reviewer@agentforge.dev"
    reason: str = "Approved via demo UI"


class RejectPayload(BaseModel):
    reviewer_id: str = "demo-reviewer@agentforge.dev"
    reason: str = "Rejected via demo UI"


# ── Trigger endpoints ─────────────────────────────────────────────────────────

@router.post("/health/trigger")
async def trigger_health(
    payload: HealthTriggerPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Trigger a health workflow. If clinician_approves is None, HITL stays open until you approve/reject via UI."""
    run_id = f"pl-health-{uuid.uuid4().hex[:8]}"
    reg = _registry()

    case = PatientCase(
        patient_id=payload.patient_id,
        age=payload.age,
        symptoms=payload.symptoms,
        medical_history=payload.medical_history,
        allergies=payload.allergies,
    )
    run_record: dict[str, Any] = {
        "run_id": run_id, "product_line": "health",
        "status": "running", "started_at": time.time(),
        "input": payload.model_dump(), "result": None,
    }
    _runs.append(run_record)

    await _broadcast({"type": "run_started", "run_id": run_id, "product_line": "health"})

    async def _run() -> None:
        try:
            if payload.clinician_approves is not None:
                # Simulation mode: auto-resolve after short delay
                from product_lines.health.workflow import _simulate_clinical_approval
                sim = asyncio.create_task(
                    _simulate_clinical_approval(reg, approve=payload.clinician_approves)
                )
                result = await run_health_workflow(case, reg, clinician_approves=payload.clinician_approves)
                await sim
            else:
                # Real HITL — workflow pauses until approve/reject hits /api/pl/hitl/{id}/approve
                result = await run_health_workflow(case, reg)
            run_record["status"] = "completed"
            run_record["result"] = result
        except Exception as e:
            run_record["status"] = "failed"
            run_record["error"] = str(e)
        finally:
            run_record["completed_at"] = time.time()
            await _broadcast({"type": "run_completed", "run_id": run_id,
                              "status": run_record["status"], "result": run_record.get("result")})

    background_tasks.add_task(_run)
    return {"run_id": run_id, "status": "running", "message": "Health workflow started"}


@router.post("/investment/trigger")
async def trigger_investment(
    payload: InvestmentTriggerPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Trigger an investment workflow. HITL pauses until advisor approves/rejects."""
    run_id = f"pl-inv-{uuid.uuid4().hex[:8]}"
    reg = _registry()

    portfolio = Portfolio(
        account_id=payload.account_id,
        total_value_usd=payload.total_value_usd,
        holdings=payload.holdings,
    )
    trade = TradeOrder(
        trade_type=TradeType(payload.trade_type.lower()),
        ticker=payload.ticker,
        amount_usd=payload.amount_usd,
        rationale=payload.rationale,
    )
    run_record: dict[str, Any] = {
        "run_id": run_id, "product_line": "investment",
        "status": "running", "started_at": time.time(),
        "input": payload.model_dump(), "result": None,
    }
    _runs.append(run_record)

    await _broadcast({"type": "run_started", "run_id": run_id, "product_line": "investment"})

    async def _run() -> None:
        try:
            if payload.advisor_approves is not None:
                from product_lines.investment.workflow import _simulate_advisor_approval
                sim = asyncio.create_task(
                    _simulate_advisor_approval(reg, approve=payload.advisor_approves)
                )
                result = await run_investment_workflow(portfolio, trade, reg,
                                                       advisor_approves=payload.advisor_approves)
                await sim
            else:
                result = await run_investment_workflow(portfolio, trade, reg)
            run_record["status"] = "completed"
            run_record["result"] = result
        except Exception as e:
            run_record["status"] = "failed"
            run_record["error"] = str(e)
        finally:
            run_record["completed_at"] = time.time()
            await _broadcast({"type": "run_completed", "run_id": run_id,
                              "status": run_record["status"], "result": run_record.get("result")})

    background_tasks.add_task(_run)
    return {"run_id": run_id, "status": "running", "message": "Investment workflow started"}


@router.post("/kids/trigger")
async def trigger_kids(
    payload: KidsTriggerPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Trigger a kids-activities workflow. HITL waits for parental approval."""
    run_id = f"pl-kids-{uuid.uuid4().hex[:8]}"
    reg = _registry()

    child = Child(
        child_id=f"KID-{uuid.uuid4().hex[:4].upper()}",
        name=payload.child_name,
        age=payload.child_age,
        allergies=payload.allergies,
        existing_schedule=payload.existing_schedule,
    )
    run_record: dict[str, Any] = {
        "run_id": run_id, "product_line": "kids",
        "status": "running", "started_at": time.time(),
        "input": payload.model_dump(), "result": None,
    }
    _runs.append(run_record)

    await _broadcast({"type": "run_started", "run_id": run_id, "product_line": "kids"})

    async def _run() -> None:
        try:
            if payload.parent_approves is not None:
                from product_lines.kids.workflow import _simulate_parent_approval
                sim = asyncio.create_task(
                    _simulate_parent_approval(reg, approve=payload.parent_approves,
                                              parent_name=f"{payload.child_name}'s parent")
                )
                result = await run_kids_workflow(
                    child, payload.preferences, payload.activity_index, reg,
                    parent_approves=payload.parent_approves,
                )
                await sim
            else:
                result = await run_kids_workflow(
                    child, payload.preferences, payload.activity_index, reg,
                )
            run_record["status"] = "completed"
            run_record["result"] = result
        except Exception as e:
            run_record["status"] = "failed"
            run_record["error"] = str(e)
        finally:
            run_record["completed_at"] = time.time()
            await _broadcast({"type": "run_completed", "run_id": run_id,
                              "status": run_record["status"], "result": run_record.get("result")})

    background_tasks.add_task(_run)
    return {"run_id": run_id, "status": "running", "message": "Kids workflow started"}


# ── HITL endpoints ────────────────────────────────────────────────────────────

@router.get("/hitl/pending")
def get_pl_pending() -> list[dict[str, Any]]:
    """Return all pending HITL requests across all product lines."""
    reg = _registry()
    result = []
    for pl, orch in reg._orchestrators.items():
        cfg = reg._configs[pl]
        for req in list(orch._pending.values()):
            if not req.is_resolved:
                age = time.time() - req.created_at
                tier = orch._policy.current_tier(age)
                result.append({
                    **req.to_dict(),
                    "product_line": pl.value,
                    "channel": cfg.notification_channel,
                    "age_seconds": round(age),
                    "escalation_tier": tier.tier.value if tier else "exhausted",
                    "reviewer_group": tier.reviewer_group if tier else "—",
                    "summary": cfg.render_context(req.context),
                })
    return sorted(result, key=lambda r: r["created_at"])


@router.post("/hitl/{request_id}/approve")
async def pl_approve(request_id: str, payload: ApprovePayload) -> dict[str, Any]:
    """Approve a pending product-line HITL request."""
    reg = _registry()
    resolved = False
    for pl, orch in reg._orchestrators.items():
        req = orch._pending.get(request_id)
        if req and not req.is_resolved:
            await orch.decide(ApprovalDecision(
                request_id=request_id,
                reviewer_id=payload.reviewer_id,
                approved=True,
                reason=payload.reason,
            ))
            resolved = True
            await _broadcast({
                "type": "hitl_resolved",
                "request_id": request_id,
                "product_line": pl.value,
                "approved": True,
                "reviewer_id": payload.reviewer_id,
            })
            break
    if not resolved:
        raise HTTPException(404, f"No pending request with id={request_id}")
    return {"status": "approved", "request_id": request_id}


@router.post("/hitl/{request_id}/reject")
async def pl_reject(request_id: str, payload: RejectPayload) -> dict[str, Any]:
    """Reject a pending product-line HITL request."""
    reg = _registry()
    resolved = False
    for pl, orch in reg._orchestrators.items():
        req = orch._pending.get(request_id)
        if req and not req.is_resolved:
            await orch.decide(ApprovalDecision(
                request_id=request_id,
                reviewer_id=payload.reviewer_id,
                approved=False,
                reason=payload.reason,
            ))
            resolved = True
            await _broadcast({
                "type": "hitl_resolved",
                "request_id": request_id,
                "product_line": pl.value,
                "approved": False,
                "reviewer_id": payload.reviewer_id,
            })
            break
    if not resolved:
        raise HTTPException(404, f"No pending request with id={request_id}")
    return {"status": "rejected", "request_id": request_id}


# ── Runs & status ─────────────────────────────────────────────────────────────

@router.get("/runs")
def get_runs() -> list[dict[str, Any]]:
    """Return recent run summaries (last 50), newest first."""
    return list(reversed(_runs[-50:]))


@router.get("/status")
def pl_status() -> dict[str, Any]:
    reg = _registry()
    queues = {pl.value: reg.queue_depth(pl) for pl in ProductLine}
    total_pending = sum(queues.values())
    return {
        "product_lines": list(queues.keys()),
        "queue_depths": queues,
        "total_pending_hitl": total_pending,
        "total_runs": len(_runs),
        "notifications_sent": len(reg.notifications),
    }


# ── WebSocket: live updates ───────────────────────────────────────────────────

from fastapi import WebSocket, WebSocketDisconnect
import json as _json

@router.websocket("/ws")
async def pl_websocket(ws: WebSocket) -> None:
    """Push HITL events and run updates to connected browsers in real time."""
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _pl_ws_clients.append(q)
    try:
        while True:
            msg = await q.get()
            await ws.send_text(_json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        _pl_ws_clients.remove(q)
