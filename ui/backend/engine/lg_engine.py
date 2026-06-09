"""
LangGraph execution engine for AgentForge workflows.

Converts a WorkflowDefinition (WFNode + WFEdge) into a compiled LangGraph
StateGraph at execution time, runs it, and handles HITL via LangGraph's
built-in ``interrupt()`` / ``Command(resume=...)`` mechanism.

Why LangGraph over the custom DAG walker:
  - Built-in interrupt/resume for HITL (no DB polling loop needed)
  - State persistence via checkpointer (survives Railway restarts natively)
  - Streaming node outputs
  - Time-travel / audit replay via get_state_history()

Checkpointer selection:
  DATABASE_URL set  →  try AsyncPostgresSaver (Railway)
  No DATABASE_URL   →  MemorySaver (local dev)

Usage from main.py::

    from ui.backend.engine import lg_engine

    # Startup
    await lg_engine.setup_checkpointer()

    # Execute
    result = await lg_engine.run_workflow(run_id, workflow_def, input_data)

    # Resume after HITL
    result = await lg_engine.resume_workflow(run_id, workflow_def, decision)

    # Check if paused at HITL
    info = lg_engine.get_interrupt_info(run_id, workflow_def)
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

# ── LangGraph imports ─────────────────────────────────────────────────────────
try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt, Command
    from langgraph.checkpoint.memory import MemorySaver
    _LG_AVAILABLE = True
except ImportError:
    _LG_AVAILABLE = False
    logger.warning(
        "langgraph not installed — LangGraph engine disabled. "
        "Install with: pip install langgraph langchain-anthropic"
    )


# ── State schema ──────────────────────────────────────────────────────────────

class WorkflowState(TypedDict):
    """
    Shared state that flows through every node in the LangGraph.

    ``data`` carries all extracted fields (vendor_id, amount_usd, …) so
    downstream nodes can read values produced by upstream ones — exactly
    like ExecutionContext in the custom engine but serialisable by LangGraph.
    """
    run_id: str
    workflow_id: str
    workflow_name: str
    # The live data envelope — grows as each node extracts / transforms fields
    data: dict[str, Any]
    # Ordered list of completed node IDs (for the UI steps panel)
    completed_nodes: list[str]
    # Set by condition nodes; consumed by conditional edge routers
    next_port: str | None
    # Error message from any failing node
    error: str | None
    # HITL fields — populated after a HITL node runs
    hitl_request_id: str | None
    hitl_approved: bool | None
    hitl_reviewer: str | None
    hitl_reason: str | None


# ── Node factory ──────────────────────────────────────────────────────────────

def _make_node_fn(wf_node: Any):
    """
    Wrap a WFNode into an async LangGraph node function.

    Each function receives the full WorkflowState, runs the appropriate
    runner (reusing our existing engine runners), and returns a partial
    update dict that LangGraph merges into the state.
    """
    from ui.backend.engine.context import ExecutionContext
    from ui.backend.engine.runners.agent import run_agent
    from ui.backend.engine.runners.api import run_api
    from ui.backend.engine.runners.condition import run_condition
    from ui.backend.engine.runners.simple import (
        run_event, run_loop, run_subwf, run_transform, run_trigger,
    )

    node_type = wf_node.node_type

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        run_id = state["run_id"]

        # ── HITL node — pause here until a human decides ──────────────────────
        if node_type == "hitl":
            request_id = f"req-{uuid.uuid4().hex[:8]}"
            # interrupt() suspends execution; LangGraph serialises the state to
            # the checkpointer and returns from ainvoke().  The caller resumes
            # by calling ainvoke(Command(resume=decision), config=...).
            decision: dict[str, Any] = interrupt({
                "request_id": request_id,
                "action": wf_node.name,
                "workflow": state["workflow_name"],
                "run_id": run_id,
                "context": state["data"],
            })
            approved = bool(decision.get("approved", False))
            reviewer = str(decision.get("reviewer_id", "unknown"))
            reason   = str(decision.get("reason", ""))
            return {
                "data": {
                    **state["data"],
                    "hitl_approved": approved,
                    "hitl_reviewer": reviewer,
                },
                "completed_nodes": state.get("completed_nodes", []) + [wf_node.node_id],
                "hitl_request_id": request_id,
                "hitl_approved":   approved,
                "hitl_reviewer":   reviewer,
                "hitl_reason":     reason,
                # Condition router will read this to pick approved vs rejected edge
                "next_port": "approved" if approved else "rejected",
            }

        # ── All other node types — delegate to existing runners ───────────────
        ctx = ExecutionContext(
            run_id=run_id,
            workflow_id=state["workflow_id"],
            workflow_name=state["workflow_name"],
            data=dict(state["data"]),
        )

        _runner_map = {
            "trigger":   run_trigger,
            "agent":     run_agent,
            "api":       run_api,
            "condition": run_condition,
            "transform": run_transform,
            "event":     run_event,
            "loop":      run_loop,
            "subwf":     run_subwf,
        }
        runner = _runner_map.get(node_type)
        if runner is None:
            logger.warning("LangGraph: unknown node_type '%s' for node '%s' — skipping",
                           node_type, wf_node.name)
            return {
                "completed_nodes": state.get("completed_nodes", []) + [wf_node.node_id],
            }

        result = await runner(wf_node, ctx, run_id)

        if result.error:
            return {
                "error": result.error,
                "completed_nodes": state.get("completed_nodes", []) + [wf_node.node_id],
            }

        return {
            "data": ctx.snapshot(),
            "completed_nodes": state.get("completed_nodes", []) + [wf_node.node_id],
            "next_port": result.next_port,
            "error": None,
        }

    node_fn.__name__ = f"node_{wf_node.node_id}_{wf_node.node_type}"
    return node_fn


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph(workflow: Any, checkpointer: Any) -> Any:
    """
    Compile a LangGraph StateGraph from a WorkflowDefinition.

    Handles three edge patterns:
    1. Plain edges (A → B)
    2. Ported condition edges (A → B port='yes', A → C port='no')
    3. HITL edges: approved → next_node, rejected → END
    """
    graph: StateGraph = StateGraph(WorkflowState)

    # Index outgoing edges by source node
    edges_from: dict[str, list[Any]] = {}
    for e in workflow.edges:
        edges_from.setdefault(e.from_node, []).append(e)

    # Find entry node (no incoming edges)
    all_targets = {e.to_node for e in workflow.edges}
    entry_id = next(
        (n.node_id for n in workflow.nodes if n.node_id not in all_targets),
        workflow.nodes[0].node_id,
    )

    node_map = {n.node_id: n for n in workflow.nodes}

    # Register nodes
    for wf_node in workflow.nodes:
        graph.add_node(wf_node.node_id, _make_node_fn(wf_node))

    # Entry edge
    graph.add_edge(START, entry_id)

    # Wire outgoing edges for each node
    for wf_node in workflow.nodes:
        outgoing = edges_from.get(wf_node.node_id, [])
        node_type = wf_node.node_type

        if not outgoing:
            # Terminal node
            graph.add_edge(wf_node.node_id, END)

        elif node_type == "hitl":
            # HITL: route on approval decision
            # The single (or first) outgoing edge is the "approved" path.
            approved_target = outgoing[0].to_node if outgoing else END
            routing_map = {"approved": approved_target, "rejected": END}
            graph.add_conditional_edges(
                wf_node.node_id,
                lambda s: "approved" if s.get("hitl_approved") else "rejected",
                routing_map,
            )

        elif any(e.port for e in outgoing):
            # Condition node: port="yes"/"no" edges
            port_map: dict[str, str] = {
                e.port: e.to_node for e in outgoing if e.port
            }
            unported = [e.to_node for e in outgoing if not e.port]
            default_target = unported[0] if unported else END

            def _make_router(pm: dict[str, str], dt: Any):
                # Returns the destination node ID directly.
                # Do NOT pass a path_map to add_conditional_edges — that would
                # treat the return value as a key to look up in the map, causing
                # KeyError when the value is already a node ID (e.g. 'ma7').
                def _router(state: WorkflowState) -> Any:
                    return pm.get((state.get("next_port") or "").strip(), dt)
                return _router

            graph.add_conditional_edges(
                wf_node.node_id,
                _make_router(port_map, default_target),
            )

        else:
            # Single default edge
            graph.add_edge(wf_node.node_id, outgoing[0].to_node)

    return graph.compile(checkpointer=checkpointer)


# ── Checkpointer + graph cache ────────────────────────────────────────────────

_checkpointer: Any = None
_graphs: dict[str, Any] = {}   # workflow_id → compiled graph


async def setup_checkpointer() -> None:
    """
    Initialise the checkpointer at server startup.

    Railway: DATABASE_URL is set → try PostgreSQL checkpointer.
    Local dev: no DATABASE_URL → MemorySaver.
    """
    global _checkpointer
    if not _LG_AVAILABLE:
        return

    database_url = os.environ.get("DATABASE_URL", "")
    if database_url:
        try:
            import re
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # type: ignore
            url = re.sub(r"\+\w+", "", database_url)          # strip +asyncpg suffix
            url = url.replace("postgres://", "postgresql://", 1)
            _checkpointer = AsyncPostgresSaver.from_conn_string(url)
            await _checkpointer.setup()   # creates langgraph_checkpoints tables
            logger.info("LangGraph: PostgreSQL checkpointer ready")
        except Exception as exc:
            logger.warning(
                "LangGraph: PostgreSQL checkpointer failed (%s) — falling back to MemorySaver",
                exc,
            )
            _checkpointer = MemorySaver()
    else:
        _checkpointer = MemorySaver()
        logger.info("LangGraph: MemorySaver (local dev)")


def _get_graph(workflow: Any) -> Any:
    """Return a cached compiled graph, rebuilding if the workflow was updated."""
    key = f"{workflow.workflow_id}:{workflow.updated_at}"
    if key not in _graphs:
        # Clear stale entries for the same workflow_id
        for k in list(_graphs):
            if k.startswith(f"{workflow.workflow_id}:"):
                del _graphs[k]
        _graphs[key] = build_graph(workflow, _checkpointer or MemorySaver())
    return _graphs[key]


# ── Public API ────────────────────────────────────────────────────────────────

def is_available() -> bool:
    return _LG_AVAILABLE


async def run_workflow(
    run_id: str,
    workflow: Any,
    input_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Start a workflow run.

    Returns the LangGraph state dict.  If the graph paused at a HITL node,
    the returned state contains ``hitl_request_id`` and the run should be
    marked ``waiting_hitl``; otherwise the run is complete.
    """
    if not _LG_AVAILABLE:
        raise RuntimeError("langgraph is not installed")

    graph = _get_graph(workflow)
    initial: WorkflowState = {
        "run_id":         run_id,
        "workflow_id":    workflow.workflow_id,
        "workflow_name":  workflow.name,
        "data":           dict(input_data),
        "completed_nodes": [],
        "next_port":      None,
        "error":          None,
        "hitl_request_id": None,
        "hitl_approved":   None,
        "hitl_reviewer":   None,
        "hitl_reason":     None,
    }
    config = {"configurable": {"thread_id": run_id}}
    return await graph.ainvoke(initial, config=config)


async def resume_workflow(
    run_id: str,
    workflow: Any,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """
    Resume a workflow paused at a HITL interrupt.

    ``decision`` must contain at least ``approved`` (bool) and optionally
    ``reviewer_id`` (str) and ``reason`` (str).  These values are returned
    by the ``interrupt()`` call inside the HITL node function.
    """
    if not _LG_AVAILABLE:
        raise RuntimeError("langgraph is not installed")

    graph = _get_graph(workflow)
    config = {"configurable": {"thread_id": run_id}}
    return await graph.ainvoke(Command(resume=decision), config=config)


def get_interrupt_info(run_id: str, workflow: Any) -> dict[str, Any] | None:
    """
    Return the interrupt payload if the run is paused at a HITL node,
    otherwise None.

    The payload is the dict passed to ``interrupt()`` inside the node —
    it includes ``request_id``, ``action``, ``context``, etc.
    """
    if not _LG_AVAILABLE:
        return None
    try:
        graph = _get_graph(workflow)
        config = {"configurable": {"thread_id": run_id}}
        snapshot = graph.get_state(config)
        if snapshot and snapshot.tasks:
            for task in snapshot.tasks:
                if task.interrupts:
                    return dict(task.interrupts[0].value)
    except Exception:
        pass
    return None
