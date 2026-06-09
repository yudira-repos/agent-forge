"""
Workflow Designer API
====================
CRUD endpoints for workflow definitions.
Workflows are stored in-memory (replace _store with a DB backend in production).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

# ── Pydantic models ────────────────────────────────────────────────────────────

class WFNode(BaseModel):
    node_id: str
    node_type: str                      # trigger | agent | api | condition | hitl | transform | event | loop | subwf
    name: str
    x: float
    y: float
    detail: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class WFEdge(BaseModel):
    edge_id: str
    from_node: str
    to_node: str
    port: str | None = None             # "yes" | "no" | None (default out)
    label: str | None = None


class WorkflowDefinition(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    nodes: list[WFNode] = Field(default_factory=list)
    edges: list[WFEdge] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class CreateWorkflowPayload(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0.0"
    nodes: list[WFNode] = Field(default_factory=list)
    edges: list[WFEdge] = Field(default_factory=list)


class UpdateWorkflowPayload(BaseModel):
    name: str | None = None
    description: str | None = None
    version: str | None = None
    nodes: list[WFNode] | None = None
    edges: list[WFEdge] | None = None


# ── In-memory store ────────────────────────────────────────────────────────────
_store: dict[str, WorkflowDefinition] = {}


def _seed() -> None:
    """Pre-load the invoice processing example workflow."""
    wf = WorkflowDefinition(
        workflow_id="wf-invoice-001",
        name="Invoice processing",
        description="End-to-end invoice intake: extract → validate → approve → pay → confirm",
        version="1.2.0",
        nodes=[
            WFNode(node_id="n1", node_type="trigger",   name="Invoice received",  x=15,   y=165, detail="POST /webhook/invoice",
                   config={"Method": "POST", "Path": "/webhook/invoice", "Auth": "HMAC-SHA256", "Schema": "InvoiceEvent v1"}),
            WFNode(node_id="n2", node_type="agent",     name="Extract invoice",   x=185,  y=165, detail="invoice-extractor · v2.1",
                   config={"Agent ID": "invoice-extractor-001", "Model": "claude-opus-4-5", "Output": "vendor_id, amount_usd, items"}),
            WFNode(node_id="n3", node_type="api",       name="Validate vendor",   x=355,  y=165, detail="GET /vendors/{id} · SAP",
                   config={"URL": "sap.internal/vendors/{vendor_id}", "Method": "GET", "Auth": "Bearer", "Timeout": "5000ms"}),
            WFNode(node_id="n4", node_type="condition", name="Amount > $10k?",    x=525,  y=165, detail="amount_usd > 10000",
                   config={"Expression": "ctx.amount_usd > 10000", "Yes branch": "Finance Approval", "No branch": "Auto-Process"}),
            WFNode(node_id="n5", node_type="hitl",      name="Finance approval",  x=695,  y=70,  detail="finance-leads · 15 min",
                   config={"Reviewer group": "finance-leads", "Timeout": "900s", "Escalation": "standard", "Notify": "Slack #approvals"}),
            WFNode(node_id="n6", node_type="agent",     name="Process payment",   x=870,  y=70,  detail="payment-processor · prod",
                   config={"Agent ID": "payment-processor-001", "Scopes": "payments:write", "Requires HITL": "true"}),
            WFNode(node_id="n7", node_type="agent",     name="Auto-process",      x=695,  y=275, detail="payment-processor · auto",
                   config={"Agent ID": "payment-processor-001", "Scopes": "payments:write", "Mode": "auto"}),
            WFNode(node_id="n8", node_type="api",       name="Update ERP",        x=1055, y=165, detail="PATCH /records · SAP S/4HANA",
                   config={"URL": "sap.internal/records/{id}", "Method": "PATCH", "Auth": "OAuth2"}),
            WFNode(node_id="n9", node_type="event",     name="Payment confirmed", x=1250, y=165, detail="payments.confirmed · Kafka",
                   config={"Topic": "payments.confirmed", "Schema": "PaymentEvent v1", "Broker": "kafka.internal:9092"}),
        ],
        edges=[
            WFEdge(edge_id="e1", from_node="n1", to_node="n2"),
            WFEdge(edge_id="e2", from_node="n2", to_node="n3"),
            WFEdge(edge_id="e3", from_node="n3", to_node="n4"),
            WFEdge(edge_id="e4", from_node="n4", to_node="n5", port="yes", label="yes"),
            WFEdge(edge_id="e5", from_node="n4", to_node="n7", port="no",  label="no"),
            WFEdge(edge_id="e6", from_node="n5", to_node="n6"),
            WFEdge(edge_id="e7", from_node="n6", to_node="n8"),
            WFEdge(edge_id="e8", from_node="n7", to_node="n8"),
            WFEdge(edge_id="e9", from_node="n8", to_node="n9"),
        ],
    )
    _store[wf.workflow_id] = wf


def _seed_multiagent() -> None:
    """
    Multi-agent demo workflow: Claude + OpenAI agents working together.

    Flow
    ----
    Invoice received
      → claude-invoice-extractor   (Anthropic — extracts fields)
      → openai-risk-scorer         (OpenAI — scores payment risk)
      → Condition: high risk or large amount?
          yes → HITL Finance approval
               → claude-payment-processor (Anthropic — generates payment)
               → Payment confirmed
          no  → claude-payment-processor (auto)
               → Payment confirmed

    Both agent nodes carry an ``Agent ID`` that maps to a registered
    ``AgentManifest`` in the global registry.  The runner picks the
    correct LLM vendor (Anthropic vs OpenAI) from the manifest's
    ``runtime_adapter`` field — the workflow designer never hard-codes
    API keys or model strings.
    """
    wf = WorkflowDefinition(
        workflow_id="wf-multiagent-001",
        name="AI Dual-Vendor Invoice Approval",
        description=(
            "Demonstrates Claude (Anthropic) and GPT (OpenAI) agents collaborating in a "
            "single workflow with HITL, OTEL metrics, and live orchestration."
        ),
        version="1.0.0",
        nodes=[
            WFNode(
                node_id="ma1", node_type="trigger", name="Invoice received",
                x=15, y=165, detail="POST /webhook/invoice",
                config={"Method": "POST", "Path": "/webhook/invoice",
                        "Auth": "HMAC-SHA256"},
            ),
            WFNode(
                node_id="ma2", node_type="agent",
                name="Extract Invoice (Claude)",
                x=195, y=165, detail="claude-invoice-extractor · Anthropic",
                config={
                    "Agent ID": "claude-invoice-extractor",
                    # Model and system prompt come from the registered manifest —
                    # these are informational labels for the designer only.
                    "Model":    "claude-haiku-4-5-20251001 (from registry)",
                    "Output":   "vendor_id, amount_usd, currency, invoice_number, due_date, items",
                },
            ),
            WFNode(
                node_id="ma3", node_type="agent",
                name="Score Risk (OpenAI)",
                x=395, y=165, detail="openai-risk-scorer · GPT-4o-mini",
                config={
                    "Agent ID": "openai-risk-scorer",
                    "Model":    "gpt-4o-mini (from registry)",
                    "Output":   "risk_score, risk_level, rationale",
                },
            ),
            WFNode(
                node_id="ma4", node_type="condition",
                name="High risk or large amount?",
                x=595, y=165,
                detail="risk_score > 70 or amount_usd > 15000",
                config={
                    "Expression": "ctx.risk_score > 70 or ctx.amount_usd > 15000",
                    "Yes branch": "Finance Approval",
                    "No branch":  "Auto-Process",
                },
            ),
            WFNode(
                node_id="ma5", node_type="hitl",
                name="Finance approval",
                x=780, y=70, detail="finance-leads · 15 min",
                config={
                    "Reviewer group": "finance-leads",
                    "Timeout": "900s",
                    "Notify": "Slack #approvals",
                },
            ),
            WFNode(
                node_id="ma6", node_type="agent",
                name="Process Payment (Claude)",
                x=970, y=70, detail="claude-payment-processor · Anthropic",
                config={
                    "Agent ID": "claude-payment-processor",
                    "Model":    "claude-haiku-4-5-20251001 (from registry)",
                    "Output":   "payment_id, transaction_id, status, processed_at",
                    "Scopes":   "payments:write",
                },
            ),
            WFNode(
                node_id="ma7", node_type="agent",
                name="Auto-Process Payment (Claude)",
                x=780, y=280, detail="claude-payment-processor · auto",
                config={
                    "Agent ID": "claude-payment-processor",
                    "Model":    "claude-haiku-4-5-20251001 (from registry)",
                    "Output":   "payment_id, transaction_id, status, processed_at",
                    "Mode":     "auto",
                },
            ),
            WFNode(
                node_id="ma8", node_type="event",
                name="Payment confirmed",
                x=1160, y=165, detail="payments.confirmed · Kafka",
                config={
                    "Topic":  "payments.confirmed",
                    "Schema": "PaymentEvent v1",
                    "Broker": "kafka.internal:9092",
                },
            ),
        ],
        edges=[
            WFEdge(edge_id="me1", from_node="ma1", to_node="ma2"),
            WFEdge(edge_id="me2", from_node="ma2", to_node="ma3"),
            WFEdge(edge_id="me3", from_node="ma3", to_node="ma4"),
            WFEdge(edge_id="me4", from_node="ma4", to_node="ma5", port="yes", label="yes"),
            WFEdge(edge_id="me5", from_node="ma4", to_node="ma7", port="no",  label="no"),
            WFEdge(edge_id="me6", from_node="ma5", to_node="ma6"),
            WFEdge(edge_id="me7", from_node="ma6", to_node="ma8"),
            WFEdge(edge_id="me8", from_node="ma7", to_node="ma8"),
        ],
    )
    _store[wf.workflow_id] = wf


_seed()
_seed_multiagent()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[dict[str, Any]])
def list_workflows() -> list[dict[str, Any]]:
    """Return all workflows (summary — no node/edge lists)."""
    return [
        {
            "workflow_id": wf.workflow_id,
            "name": wf.name,
            "description": wf.description,
            "version": wf.version,
            "node_count": len(wf.nodes),
            "edge_count": len(wf.edges),
            "updated_at": wf.updated_at,
        }
        for wf in sorted(_store.values(), key=lambda w: w.updated_at, reverse=True)
    ]


@router.get("/{workflow_id}", response_model=WorkflowDefinition)
def get_workflow(workflow_id: str) -> WorkflowDefinition:
    wf = _store.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.post("", response_model=WorkflowDefinition, status_code=201)
def create_workflow(payload: CreateWorkflowPayload) -> WorkflowDefinition:
    wf = WorkflowDefinition(
        workflow_id=f"wf-{uuid.uuid4().hex[:8]}",
        name=payload.name,
        description=payload.description,
        version=payload.version,
        nodes=payload.nodes,
        edges=payload.edges,
    )
    _store[wf.workflow_id] = wf
    return wf


@router.put("/{workflow_id}", response_model=WorkflowDefinition)
def update_workflow(workflow_id: str, payload: UpdateWorkflowPayload) -> WorkflowDefinition:
    wf = _store.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if payload.name is not None:
        wf.name = payload.name
    if payload.description is not None:
        wf.description = payload.description
    if payload.version is not None:
        wf.version = payload.version
    if payload.nodes is not None:
        wf.nodes = payload.nodes
    if payload.edges is not None:
        wf.edges = payload.edges
    wf.updated_at = time.time()
    return wf


@router.delete("/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str) -> None:
    if workflow_id not in _store:
        raise HTTPException(status_code=404, detail="Workflow not found")
    del _store[workflow_id]
