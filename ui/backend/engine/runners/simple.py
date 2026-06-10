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

async def _publish_kafka(topic: str, payload: dict[str, Any]) -> str:
    """Publish to Kafka if aiokafka is installed and KAFKA_BOOTSTRAP_SERVERS is set."""
    import os
    servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    if not servers:
        raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS not set")
    from aiokafka import AIOKafkaProducer  # type: ignore
    producer = AIOKafkaProducer(bootstrap_servers=servers)
    await producer.start()
    try:
        await producer.send_and_wait(topic, json.dumps(payload).encode())
    finally:
        await producer.stop()
    return f"kafka://{servers}/{topic}"


async def _publish_webhook(url: str, payload: dict[str, Any]) -> str:
    """POST the event payload to an HTTP webhook (EventBridge, Pub/Sub push, custom)."""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    return url


async def _publish_eventbridge(bus: str, source: str, detail_type: str, payload: dict[str, Any]) -> str:
    """Put an event to AWS EventBridge if boto3 is available."""
    import boto3  # type: ignore
    client = boto3.client("events")
    client.put_events(Entries=[{
        "Source": source,
        "DetailType": detail_type,
        "Detail": json.dumps(payload),
        "EventBusName": bus,
    }])
    return f"eventbridge://{bus}/{detail_type}"


async def run_event(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Publishes a domain event to a real broker.

    Broker selection (first match wins):
      1. ``KAFKA_BOOTSTRAP_SERVERS`` env var → Kafka via aiokafka
      2. ``EVENT_WEBHOOK_URL`` env var → HTTP POST (works with EventBridge,
         Pub/Sub push subscriptions, Zapier, custom webhooks)
      3. ``EVENTBRIDGE_BUS_NAME`` env var → AWS EventBridge via boto3
      4. Fallback → structured JSON log (honest: no external side-effect)

    Node config keys:
      - ``Topic``  — Kafka topic name or EventBridge detail-type  (default: "agentforge.events")
      - ``Schema`` — event schema identifier for consumers
      - ``Broker`` — informational label (e.g. "kafka", "eventbridge")
    """
    import os

    cfg = node.config
    topic       = cfg.get("Topic", cfg.get("topic", "agentforge.events"))
    schema      = cfg.get("Schema", "")
    broker_hint = cfg.get("Broker", "")

    published_at = time.time()
    payload = {
        "topic":       topic,
        "schema":      schema,
        "run_id":      run_id,
        "workflow_id": ctx.workflow_id,
        "event_data":  {k: v for k, v in ctx.snapshot().items() if not k.startswith("_")},
        "published_at": published_at,
    }

    broker_used = "log"
    delivery_url = ""

    # ── 1. Kafka ──────────────────────────────────────────────────────────────
    if os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
        try:
            delivery_url = await _publish_kafka(topic, payload)
            broker_used = "kafka"
            logger.info("Event '%s': published to Kafka topic '%s'", node.name, topic)
        except Exception as exc:
            logger.warning("Event '%s': Kafka publish failed (%s) — falling through", node.name, exc)

    # ── 2. HTTP Webhook ───────────────────────────────────────────────────────
    if broker_used == "log" and os.environ.get("EVENT_WEBHOOK_URL"):
        try:
            delivery_url = await _publish_webhook(os.environ["EVENT_WEBHOOK_URL"], payload)
            broker_used = "webhook"
            logger.info("Event '%s': POSTed to webhook %s", node.name, delivery_url)
        except Exception as exc:
            logger.warning("Event '%s': webhook POST failed (%s) — falling through", node.name, exc)

    # ── 3. AWS EventBridge ────────────────────────────────────────────────────
    if broker_used == "log" and os.environ.get("EVENTBRIDGE_BUS_NAME"):
        try:
            delivery_url = await _publish_eventbridge(
                bus=os.environ["EVENTBRIDGE_BUS_NAME"],
                source=f"agentforge.{ctx.workflow_id}",
                detail_type=topic,
                payload=payload,
            )
            broker_used = "eventbridge"
            logger.info("Event '%s': sent to EventBridge bus '%s'", node.name, os.environ["EVENTBRIDGE_BUS_NAME"])
        except Exception as exc:
            logger.warning("Event '%s': EventBridge publish failed (%s) — falling through", node.name, exc)

    # ── 4. Structured log fallback ────────────────────────────────────────────
    if broker_used == "log":
        logger.info(
            "Event '%s': no broker configured — logging event. "
            "Set KAFKA_BOOTSTRAP_SERVERS, EVENT_WEBHOOK_URL, or EVENTBRIDGE_BUS_NAME "
            "to publish to a real broker. payload=%s",
            node.name, json.dumps(payload, default=str)[:500],
        )

    ctx.set("last_event", {"topic": topic, "broker": broker_used, "published_at": published_at})

    return NodeResult(output={
        "event_published": broker_used != "log",
        "broker":          broker_used,
        "topic":           topic,
        "schema":          schema,
        "delivery_url":    delivery_url,
        "published_at":    published_at,
        "note": (
            "No broker configured — set KAFKA_BOOTSTRAP_SERVERS, "
            "EVENT_WEBHOOK_URL, or EVENTBRIDGE_BUS_NAME to enable real publishing."
            if broker_used == "log" else ""
        ),
    })


# ── Loop ──────────────────────────────────────────────────────────────────────

async def run_loop(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Iterates over a collection and applies a per-item transform expression.

    Config keys::

        Items     — expression that resolves to a list, e.g. "ctx.line_items"
        Variable  — name exposed per-iteration, e.g. "item"  (default: "item")
        Transform — optional expression applied to each item, e.g.
                    "{'sku': item['sku'], 'total': item['qty'] * item['unit_price']}"
        Reduce    — "sum" | "list" | "first"  (default: "list")
                    How to combine per-item results into the output.

    Results are written to ctx as ``{loop_var}_results`` (list of per-item
    outputs) and ``{loop_var}_count``.  The first item is also available as
    ``{loop_var}`` for single-item access patterns.

    Example::

        {"Items": "ctx.items", "Variable": "item",
         "Transform": "{'total': item['qty'] * item['unit_price']}"}

        → ctx.item_results = [{"total": 500.0}, {"total": 200.0}]
        → ctx.item_count   = 2
    """
    cfg = node.config
    items_expr = cfg.get("Items", cfg.get("items", "[]")).strip()
    loop_var   = cfg.get("Variable", cfg.get("variable", "item"))
    xform_expr = cfg.get("Transform", cfg.get("transform", "")).strip()
    reduce_op  = cfg.get("Reduce", "list").strip().lower()

    # ── Resolve the collection ────────────────────────────────────────────────
    namespace: dict[str, Any] = {
        **_SAFE_BUILTINS,
        "ctx": ctx.as_namespace(),
        **ctx.data,
    }
    try:
        items = eval(  # noqa: S307
            compile(items_expr, "<loop-items>", "eval"),
            {"__builtins__": {}},
            namespace,
        )
    except Exception:
        items = ctx.get(items_expr.lstrip("ctx."), [])

    if not isinstance(items, (list, tuple)):
        items = [items] if items else []

    # ── Per-item processing ───────────────────────────────────────────────────
    results: list[Any] = []
    errors:  list[str] = []

    for i, item in enumerate(items):
        if xform_expr:
            item_ns: dict[str, Any] = {
                **_SAFE_BUILTINS,
                "ctx":     ctx.as_namespace(),
                loop_var:  item,
                "item":    item,   # always available as "item" regardless of loop_var
                "index":   i,
                **ctx.data,
            }
            try:
                result = eval(  # noqa: S307
                    compile(xform_expr, f"<loop-transform[{i}]>", "eval"),
                    {"__builtins__": {}},
                    item_ns,
                )
                results.append(result)
            except Exception as exc:
                errors.append(f"[{i}] {exc}")
                results.append(item)   # pass through unchanged on error
        else:
            results.append(item)

    # ── Reduce ────────────────────────────────────────────────────────────────
    if reduce_op == "sum":
        try:
            reduced: Any = sum(
                v for v in results
                if isinstance(v, (int, float))
            )
        except Exception:
            reduced = results
    elif reduce_op == "first":
        reduced = results[0] if results else None
    else:
        reduced = results   # "list" (default)

    # ── Write back to context ─────────────────────────────────────────────────
    ctx.set(f"{loop_var}_results",  results)
    ctx.set(f"{loop_var}_count",    len(items))
    ctx.set(f"{loop_var}_reduced",  reduced)
    if results:
        ctx.set(loop_var, results[0])   # first item for easy downstream access

    if errors:
        logger.warning("Loop '%s': %d transform errors: %s", node.name, len(errors), errors[:3])

    return NodeResult(output={
        "loop_variable":   loop_var,
        "iteration_count": len(items),
        "results":         results,
        "reduced":         reduced,
        "errors":          errors,
        "items_preview":   list(items)[:5],
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
