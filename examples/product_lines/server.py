"""
Product Lines HITL Demo Server
================================
Standalone FastAPI server — no database, no Postgres, no external services.

    cd <repo-root>
    pip install fastapi uvicorn websockets
    python examples/product_lines/server.py

Then open:  http://localhost:8000/

Endpoints
---------
GET  /                           — interactive HITL demo UI
POST /api/pl/health/trigger      — start a health workflow
POST /api/pl/investment/trigger  — start an investment workflow
POST /api/pl/kids/trigger        — start a kids-activities workflow
GET  /api/pl/hitl/pending        — all pending HITL requests (all product lines)
POST /api/pl/hitl/{id}/approve   — approve a pending request
POST /api/pl/hitl/{id}/reject    — reject a pending request
GET  /api/pl/runs                — recent run history
GET  /api/pl/status              — queue depths + stats
WS   /api/pl/ws                  — live push updates (JSON frames)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────────────
_here = Path(__file__).parent
_repo = _here.parent.parent
_examples = _here.parent
for p in [str(_repo), str(_examples)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from product_lines.health.workflow import PatientCase, run_health_workflow
from product_lines.investment.workflow import Portfolio, TradeOrder, TradeType, run_investment_workflow
from product_lines.kids.workflow import Child, run_kids_workflow
from product_lines.shared.hitl_registry import (
    HITLConfig, HITLRegistry, ProductLine,
    _health_escalation, _investment_escalation, _kids_escalation,
    _health_renderer, _investment_renderer, _kids_renderer,
)
from agentforge.hitl import ApprovalDecision, ApprovalRequest

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="AgentForge Product Lines Demo", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── State ─────────────────────────────────────────────────────────────────────
_ws_clients: list[asyncio.Queue] = []
_runs: list[dict[str, Any]] = []
_registry: HITLRegistry | None = None


async def _broadcast(msg: dict[str, Any]) -> None:
    for q in list(_ws_clients):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def get_registry() -> HITLRegistry:
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def _build_registry() -> HITLRegistry:
    reg = HITLRegistry()
    for pl, policy, renderer, channel, timeout in [
        (ProductLine.HEALTH,     _health_escalation(),     _health_renderer,     "slack:#clinical-approvals", 2400),
        (ProductLine.INVESTMENT, _investment_escalation(), _investment_renderer, "slack:#trade-approvals",    5400),
        (ProductLine.KIDS,       _kids_escalation(),       _kids_renderer,       "sms+push",                 5400),
    ]:
        reg.register(HITLConfig(
            product_line=pl,
            escalation_policy=policy,
            context_renderer=renderer,
            notification_channel=channel,
            default_timeout_seconds=timeout,
        ))

    # Patch each orchestrator's notifier to also push over WebSocket
    for pl, orch in reg._orchestrators.items():
        orig = orch._notify
        cfg = reg._configs[pl]

        async def _ws_notify(req: ApprovalRequest, group: str,
                             _pl=pl, _cfg=cfg, _orig=orig) -> None:
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


# ── Pydantic models ───────────────────────────────────────────────────────────

class HealthPayload(BaseModel):
    patient_id: str = "P-DEMO-001"
    age: int = 67
    symptoms: list[str] = ["chest pain", "difficulty breathing"]
    medical_history: list[str] = ["diabetes"]
    allergies: list[str] = []
    clinician_approves: bool | None = None   # None = real HITL


class InvestmentPayload(BaseModel):
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


class KidsPayload(BaseModel):
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


# ── Helper: run a workflow in the background ──────────────────────────────────

def _make_run(product_line: str, input_data: dict) -> dict[str, Any]:
    run = {
        "run_id": f"pl-{product_line[:3]}-{uuid.uuid4().hex[:8]}",
        "product_line": product_line,
        "status": "running",
        "started_at": time.time(),
        "input": input_data,
        "result": None,
        "error": None,
        "completed_at": None,
    }
    _runs.append(run)
    return run


async def _finish(run: dict, coro) -> None:
    await _broadcast({"type": "run_started", "run_id": run["run_id"], "product_line": run["product_line"]})
    try:
        run["result"] = await coro
        run["status"] = "completed"
    except Exception as e:
        run["status"] = "failed"
        run["error"] = str(e)
    finally:
        run["completed_at"] = time.time()
        await _broadcast({
            "type": "run_completed",
            "run_id": run["run_id"],
            "status": run["status"],
            "result": run["result"],
        })


# ── Trigger endpoints ─────────────────────────────────────────────────────────

@app.post("/api/pl/health/trigger")
async def trigger_health(p: HealthPayload, bg: BackgroundTasks) -> dict:
    reg = get_registry()
    case = PatientCase(
        patient_id=p.patient_id, age=p.age,
        symptoms=p.symptoms, medical_history=p.medical_history, allergies=p.allergies,
    )
    run = _make_run("health", p.model_dump())

    if p.clinician_approves is not None:
        # Simulation: auto-resolve after a short delay so caller can watch HITL queue briefly
        from product_lines.health.workflow import _simulate_clinical_approval

        async def _sim_run():
            sim = asyncio.create_task(_simulate_clinical_approval(reg, approve=p.clinician_approves, delay=1.5))
            result = await run_health_workflow(case, reg)
            await sim
            return result

        bg.add_task(_finish, run, _sim_run())
    else:
        # Real HITL — stays open until UI approve/reject
        bg.add_task(_finish, run, run_health_workflow(case, reg))

    return {"run_id": run["run_id"], "status": "running"}


@app.post("/api/pl/investment/trigger")
async def trigger_investment(p: InvestmentPayload, bg: BackgroundTasks) -> dict:
    reg = get_registry()
    portfolio = Portfolio(
        account_id=p.account_id, total_value_usd=p.total_value_usd, holdings=p.holdings,
    )
    trade = TradeOrder(
        trade_type=TradeType(p.trade_type.lower()),
        ticker=p.ticker, amount_usd=p.amount_usd, rationale=p.rationale,
    )
    run = _make_run("investment", p.model_dump())

    if p.advisor_approves is not None:
        from product_lines.investment.workflow import _simulate_advisor_approval

        async def _sim_run():
            sim = asyncio.create_task(_simulate_advisor_approval(reg, approve=p.advisor_approves, delay=1.5))
            result = await run_investment_workflow(portfolio, trade, reg)
            await sim
            return result

        bg.add_task(_finish, run, _sim_run())
    else:
        bg.add_task(_finish, run, run_investment_workflow(portfolio, trade, reg))

    return {"run_id": run["run_id"], "status": "running"}


@app.post("/api/pl/kids/trigger")
async def trigger_kids(p: KidsPayload, bg: BackgroundTasks) -> dict:
    reg = get_registry()
    child = Child(
        child_id=f"KID-{uuid.uuid4().hex[:4].upper()}",
        name=p.child_name, age=p.child_age,
        allergies=p.allergies, existing_schedule=p.existing_schedule,
    )
    run = _make_run("kids", p.model_dump())

    if p.parent_approves is not None:
        from product_lines.kids.workflow import _simulate_parent_approval

        async def _sim_run():
            sim = asyncio.create_task(
                _simulate_parent_approval(reg, approve=p.parent_approves,
                                          parent_name=f"{p.child_name}'s parent", delay=1.5)
            )
            result = await run_kids_workflow(child, p.preferences, p.activity_index, reg)
            await sim
            return result

        bg.add_task(_finish, run, _sim_run())
    else:
        bg.add_task(_finish, run, run_kids_workflow(child, p.preferences, p.activity_index, reg))

    return {"run_id": run["run_id"], "status": "running"}


# ── HITL endpoints ────────────────────────────────────────────────────────────

@app.get("/api/pl/hitl/pending")
def get_pending() -> list[dict]:
    reg = get_registry()
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


@app.post("/api/pl/hitl/{request_id}/approve")
async def approve(request_id: str, p: ApprovePayload) -> dict:
    reg = get_registry()
    for pl, orch in reg._orchestrators.items():
        req = orch._pending.get(request_id)
        if req and not req.is_resolved:
            await orch.decide(ApprovalDecision(
                request_id=request_id,
                reviewer_id=p.reviewer_id,
                approved=True,
                reason=p.reason,
            ))
            await _broadcast({"type": "hitl_resolved", "request_id": request_id,
                              "product_line": pl.value, "approved": True,
                              "reviewer_id": p.reviewer_id})
            return {"status": "approved", "request_id": request_id}
    raise HTTPException(404, f"No pending request: {request_id}")


@app.post("/api/pl/hitl/{request_id}/reject")
async def reject(request_id: str, p: RejectPayload) -> dict:
    reg = get_registry()
    for pl, orch in reg._orchestrators.items():
        req = orch._pending.get(request_id)
        if req and not req.is_resolved:
            await orch.decide(ApprovalDecision(
                request_id=request_id,
                reviewer_id=p.reviewer_id,
                approved=False,
                reason=p.reason,
            ))
            await _broadcast({"type": "hitl_resolved", "request_id": request_id,
                              "product_line": pl.value, "approved": False,
                              "reviewer_id": p.reviewer_id})
            return {"status": "rejected", "request_id": request_id}
    raise HTTPException(404, f"No pending request: {request_id}")


# ── Status + runs ─────────────────────────────────────────────────────────────

@app.get("/api/pl/status")
def status() -> dict:
    reg = get_registry()
    queues = {pl.value: reg.queue_depth(pl) for pl in ProductLine}
    return {
        "product_lines": list(queues.keys()),
        "queue_depths": queues,
        "total_pending_hitl": sum(queues.values()),
        "total_runs": len(_runs),
        "notifications_sent": len(reg.notifications),
    }


@app.get("/api/pl/runs")
def runs() -> list[dict]:
    return list(reversed(_runs[-50:]))


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/api/pl/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _ws_clients.append(q)
    # Send current queue immediately on connect
    pending = get_pending()
    if pending:
        await ws.send_text(json.dumps({"type": "init_pending", "items": pending}))
    try:
        while True:
            msg = await q.get()
            await ws.send_text(json.dumps(msg))
    except WebSocketDisconnect:
        pass
    finally:
        if q in _ws_clients:
            _ws_clients.remove(q)


# ── Serve UI ──────────────────────────────────────────────────────────────────

_static = _repo / "ui" / "static"

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    page = _static / "product_lines.html"
    if page.exists():
        return FileResponse(str(page))
    return HTMLResponse("<h1>product_lines.html not found</h1>"
                        "<p>Expected at ui/static/product_lines.html</p>")


@app.get("/api/docs-redirect")
async def docs():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/docs")


# ── Dev entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"""
╔══════════════════════════════════════════════════════╗
║   AgentForge — Product Lines HITL Demo Server        ║
╚══════════════════════════════════════════════════════╝

  Demo UI   →  http://localhost:{port}/
  API docs  →  http://localhost:{port}/docs
  WS feed   →  ws://localhost:{port}/api/pl/ws

  Product lines: health | investment | kids
  All HITL stays open until you click Approve/Reject in the UI.
""")
    uvicorn.run("server:app", host="0.0.0.0", port=port,
                reload=False, log_level="warning",
                app_dir=str(_here))
