"""
Simple node runners — trigger, transform, event, loop, subwf.

trigger   — entry point; initialises the context from any supplied input_data.
transform — evaluates a Python expression to reshape ctx data.
event     — publishes a domain event (mock Kafka/EventBridge; stubbed for prod).
loop      — records iteration plan; full re-entrant loop is a TODO.
subwf     — invokes a child workflow; full recursive execution is a TODO.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ui.backend.engine.context import ExecutionContext
from ui.backend.engine.executor import NodeResult

logger = logging.getLogger(__name__)

_SAFE_BUILTINS: dict[str, Any] = {
    "True": True, "False": False, "None": None,
    "abs": abs, "bool": bool, "dict": dict, "float": float,
    "int": int, "len": len, "list": list, "max": max, "min": min,
    "round": round, "str": str, "sum": sum,
    "json": json, "time": time,
}


# ── Trigger ───────────────────────────────────────────────────────────────────

async def run_trigger(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Entry point of the workflow.

    In production this is invoked by a webhook receiver or event bridge consumer.
    The trigger node's config describes the expected schema; any ``input_data``
    already seeded into the context (from the /execute payload) is left intact.

    If the node config contains a ``Sample Data`` key (a JSON string) AND the
    context doesn't already have those fields, the sample data is injected.
    This lets demo workflows run with one click and no extra input.
    """
    cfg = node.config
    meta = {
        "triggered_at": time.time(),
        "trigger_type": cfg.get("Method", "manual"),
        "path": cfg.get("Path", ""),
        "schema": cfg.get("Schema", ""),
        "run_id": run_id,
    }
    ctx.update(meta)

    # Inject Sample Data when context has no real payload yet
    sample_raw = cfg.get("Sample Data", "")
    if sample_raw:
        try:
            sample: dict = json.loads(sample_raw)
            # Only inject fields not already set by the caller's input_data
            missing = {k: v for k, v in sample.items() if not ctx.get(k)}
            if missing:
                ctx.update(missing)
                meta["_sample_data_injected"] = list(missing.keys())
                logger.info(
                    "Trigger '%s': injected sample data keys %s",
                    node.name, list(missing.keys()),
                )
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Trigger '%s': invalid Sample Data JSON — %s", node.name, exc)

    return NodeResult(output=meta)


# ── Transform ─────────────────────────────────────────────────────────────────

async def run_transform(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Reshapes or derives data from the current context.

    Config examples::

        {"Expression": "{'total_usd': ctx.amount_usd * 1.1, 'tax': ctx.amount_usd * 0.1}"}
        {"Mapping": "{'display_name': ctx.vendor['name'].upper()}"}

    The expression is evaluated in a namespace that exposes:
      - ``ctx``          — SimpleNamespace of all ctx fields
      - top-level keys   — so ``amount_usd`` works alongside ``ctx.amount_usd``
      - safe builtins    — json, time, str, int, round, etc.
    """
    expr = (
        node.config.get("Expression")
        or node.config.get("Mapping")
        or node.config.get("expression")
        or ""
    ).strip()

    if not expr:
        return NodeResult(output={"transformed": False, "reason": "No expression configured"})

    namespace: dict[str, Any] = {
        **_SAFE_BUILTINS,
        "ctx": ctx.as_namespace(),
        **ctx.data,
    }

    try:
        result = eval(  # noqa: S307
            compile(expr, "<transform>", "eval"),
            {"__builtins__": {}},
            namespace,
        )
    except Exception as exc:
        return NodeResult(error=f"Transform expression failed: {exc}")

    if isinstance(result, dict):
        ctx.update(result)
        return NodeResult(output={"transformed": True, **result})

    ctx.set("transform_result", result)
    return NodeResult(output={"transformed": True, "result": result})


# ── Event ─────────────────────────────────────────────────────────────────────

async def run_event(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Publishes a domain event.

    Production integration points (pick one per deployment):
      - Kafka:        ``await producer.send(topic, json.dumps(payload).encode())``
      - AWS EventBridge: ``await eb.put_events(Entries=[{...}])``
      - Google Pub/Sub:  ``await topic.publish(json.dumps(payload).encode())``
      - Azure Service Bus: ``await sender.send_messages(ServiceBusMessage(...))``

    For now, the event is logged at INFO level and the context is annotated.
    """
    cfg = node.config
    topic = cfg.get("Topic", cfg.get("topic", "agentforge.events"))
    schema = cfg.get("Schema", "")
    broker = cfg.get("Broker", "internal")

    payload = {
        "topic": topic,
        "schema": schema,
        "run_id": run_id,
        "workflow_id": ctx.workflow_id,
        "event_data": ctx.snapshot(),
        "published_at": time.time(),
    }

    logger.info(
        "Event '%s': publishing to topic '%s' on broker '%s'",
        node.name, topic, broker,
    )

    # TODO: replace with real broker client
    # await kafka_producer.send(topic, value=json.dumps(payload).encode())

    ctx.set("last_event", {"topic": topic, "published_at": payload["published_at"]})

    return NodeResult(output={
        "event_published": True,
        "topic": topic,
        "broker": broker,
        "schema": schema,
        "published_at": payload["published_at"],
    })


# ── Loop ──────────────────────────────────────────────────────────────────────

async def run_loop(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Iterates over a collection in the context.

    Config::

        {"Items": "ctx.items", "Variable": "item"}

    The DAG walker calls ``execute_node`` for each child node once; to get true
    per-item execution the orchestrator needs to clone the context per iteration
    and fan-out.  That is planned but not yet implemented.

    For now the loop records the iteration plan in ctx so downstream nodes have
    access to the collection.
    """
    cfg = node.config
    items_expr = cfg.get("Items", cfg.get("items", "[]")).strip()
    loop_var = cfg.get("Variable", cfg.get("variable", "item"))

    namespace: dict[str, Any] = {
        **_SAFE_BUILTINS,
        "ctx": ctx.as_namespace(),
        **ctx.data,
    }

    try:
        items = eval(  # noqa: S307
            compile(items_expr, "<loop>", "eval"),
            {"__builtins__": {}},
            namespace,
        )
    except Exception:
        # Fallback: treat expr as a bare ctx key name
        items = ctx.get(items_expr.lstrip("ctx."), [])

    if not isinstance(items, (list, tuple)):
        items = [items] if items else []

    ctx.set(f"{loop_var}_count", len(items))
    ctx.set(f"{loop_var}_items", list(items))

    return NodeResult(output={
        "loop_variable": loop_var,
        "iteration_count": len(items),
        "items_preview": list(items)[:5],
    })


# ── Sub-workflow ──────────────────────────────────────────────────────────────

async def run_subwf(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Invokes a child workflow.

    Config::

        {"Workflow ID": "wf-xyz", "Input": {"key": "ctx.value"}}

    Production: recursively call ``_run_workflow`` with a child run_id and a
    clone of the current context.  The child run appears in the Executions tab
    with ``parent_run_id`` set.

    TODO: implement recursive execution and child run linking.
    """
    cfg = node.config
    sub_wf_id = cfg.get("Workflow ID", cfg.get("workflow_id", ""))

    logger.info(
        "SubWF '%s': would invoke workflow '%s' (parent run_id=%s)",
        node.name, sub_wf_id, run_id,
    )

    return NodeResult(output={
        "sub_workflow_id": sub_wf_id,
        "parent_run_id": run_id,
        "invoked": True,
        "invoked_at": time.time(),
        "note": "Recursive execution not yet wired — child run not created",
    })
