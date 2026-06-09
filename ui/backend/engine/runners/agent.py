"""
Agent node runner — executes an AI agent step.

Registry-aware execution
------------------------
When a node carries an ``Agent ID`` in its config, the runner looks up the
``AgentManifest`` from the global registry.  The manifest controls:

  • ``runtime_adapter``  — "anthropic" | "openai"  (which LLM vendor to use)
  • ``metadata.model``   — model string override
  • ``metadata.system_prompt`` — fully custom system prompt
  • ``metadata.output_fields`` — comma-separated fields to return
  • ``metadata.cost_per_1k_input/output`` — for cost tracking

If no manifest is found the runner falls back to the API-key heuristic:
  1. ANTHROPIC_API_KEY set → Claude
  2. OPENAI_API_KEY set    → GPT-4o-mini
  3. Neither               → deterministic mock

OTEL instrumentation
--------------------
Every call (real or mock) is recorded via ``record_span()`` with:
  agent_id, model, backend, run_id, latency_ms, tokens_in, tokens_out, success/error
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from ui.backend.engine.context import ExecutionContext
from ui.backend.engine.executor import NodeResult

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
_OPENAI_KEY:    str = os.environ.get("OPENAI_API_KEY", "")


# ── Registry lookup ────────────────────────────────────────────────────────────

def _get_manifest(agent_id: str) -> Any | None:
    """Return the AgentManifest for agent_id, or None if not registered."""
    if not agent_id:
        return None
    try:
        from ui.backend.agent_registry import registry
        return registry.get(agent_id)
    except Exception:
        return None


# ── Prompt construction ────────────────────────────────────────────────────────

def _system_prompt(node: Any, manifest: Any | None) -> str:
    cfg = node.config
    # Manifest system_prompt takes precedence over node config
    if manifest and manifest.metadata.get("system_prompt"):
        return manifest.metadata["system_prompt"]

    parts: list[str] = [
        f"You are an AI agent named '{node.name}' inside an automated enterprise workflow.",
        "Be precise, concise, and deterministic.",
        "Return ONLY a valid JSON object — no markdown, no explanation, no code fences.",
    ]
    output_fields = (
        manifest.metadata.get("output_fields") if manifest else None
    ) or cfg.get("Output", "")
    if output_fields:
        parts.append(f"Your response JSON must include these fields: {output_fields}.")
    if cfg.get("Scopes"):
        parts.append(f"You have been granted these permissions: {cfg['Scopes']}.")
    if cfg.get("System Prompt"):
        parts.append(cfg["System Prompt"])
    return " ".join(parts)


def _user_message(node: Any, ctx: ExecutionContext) -> str:
    cfg = node.config
    ctx_json = json.dumps(ctx.snapshot(), indent=2, default=str)
    header = f"## Current execution context\n```json\n{ctx_json}\n```"

    custom = cfg.get("Prompt", "").strip()
    if custom:
        rendered = ctx.render_template(custom)
        return f"{rendered}\n\n{header}"
    return f"Execute your task based on the context below.\n\n{header}"


# ── JSON extraction ────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a raw LLM response (strips code fences etc.)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {"result": result}
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {"result": text}


# ── Real LLM backends ──────────────────────────────────────────────────────────

async def _call_anthropic(
    node: Any,
    ctx: ExecutionContext,
    manifest: Any | None,
) -> tuple[dict[str, Any], int, int]:
    """
    Call the Anthropic API.  Returns (output_dict, tokens_in, tokens_out).
    """
    import anthropic

    # Model: manifest > node config > default
    model = (
        (manifest.metadata.get("model") if manifest else None)
        or node.config.get("Model", "claude-haiku-4-5-20251001")
    )
    if not model.startswith("claude"):
        model = "claude-haiku-4-5-20251001"

    max_tokens = int(
        (manifest.metadata.get("max_tokens") if manifest else None)
        or node.config.get("MaxTokens", 1024)
    )

    client = anthropic.AsyncAnthropic(api_key=_ANTHROPIC_KEY)
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_system_prompt(node, manifest),
        messages=[{"role": "user", "content": _user_message(node, ctx)}],
    )
    raw = response.content[0].text if response.content else "{}"
    tokens_in  = response.usage.input_tokens
    tokens_out = response.usage.output_tokens
    return _extract_json(raw), tokens_in, tokens_out


async def _call_openai(
    node: Any,
    ctx: ExecutionContext,
    manifest: Any | None,
) -> tuple[dict[str, Any], int, int]:
    """
    Call the OpenAI Chat Completions API.  Returns (output_dict, tokens_in, tokens_out).
    """
    import openai

    model = (
        (manifest.metadata.get("model") if manifest else None)
        or node.config.get("Model", "gpt-4o-mini")
    )
    if not (model.startswith("gpt") or model.startswith("o1") or model.startswith("o3")):
        model = "gpt-4o-mini"

    max_tokens = int(
        (manifest.metadata.get("max_tokens") if manifest else None)
        or node.config.get("MaxTokens", 1024)
    )

    client = openai.AsyncOpenAI(api_key=_OPENAI_KEY)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _system_prompt(node, manifest)},
            {"role": "user",   "content": _user_message(node, ctx)},
        ],
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    tokens_in  = response.usage.prompt_tokens
    tokens_out = response.usage.completion_tokens
    return _extract_json(raw), tokens_in, tokens_out


# ── Deterministic mock ─────────────────────────────────────────────────────────

def _mock_agent(node: Any, ctx: ExecutionContext, manifest: Any | None) -> dict[str, Any]:
    """
    Deterministic mock — no API key required.
    Seeded from node name for reproducible demo output.
    """
    import hashlib
    cfg = node.config
    seed = int(hashlib.md5(node.name.encode()).hexdigest()[:8], 16)

    vendor_id  = ctx.get("vendor_id",  f"V-{seed % 90000 + 10000}")
    amount_usd = ctx.get("amount_usd", round(seed % 50000 + 1000.0, 2))

    _values: dict[str, Any] = {
        "vendor_id":       vendor_id,
        "vendor_name":     f"Vendor Corp {seed % 100}",
        "amount":          amount_usd,
        "amount_usd":      amount_usd,
        "currency":        "USD",
        "invoice_number":  f"INV-{seed % 900000 + 100000}",
        "due_date":        "2026-07-31",
        "items": [
            {"sku": f"SKU-{(seed+1) % 9000 + 1000}", "qty": (seed % 10) + 1,
             "unit_price": round((seed % 500) + 50.0, 2)},
            {"sku": f"SKU-{(seed+2) % 9000 + 1000}", "qty": (seed % 5) + 1,
             "unit_price": round((seed % 200) + 20.0, 2)},
        ],
        # Risk scorer fields
        "risk_score":  round(30 + (seed % 70), 1),
        "risk_level":  ["low", "medium", "high"][(seed % 3)],
        "rationale":   f"Mock risk assessment for vendor {vendor_id}",
        # Payment processor fields
        "payment_id":      f"PAY-{seed % 900000 + 100000}",
        "transaction_id":  f"TXN-{seed % 9000000 + 1000000}",
        "status":          "success",
        "processed_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "approved":        True,
        "confidence":      round(0.85 + (seed % 15) / 100, 2),
        "message":         f"Processed by {node.name} (mock)",
    }

    # Determine output fields: manifest > node config > all values
    output_fields = (
        manifest.metadata.get("output_fields") if manifest else None
    ) or cfg.get("Output", "")
    fields = [f.strip() for f in output_fields.split(",") if f.strip()]

    if fields:
        return {f: _values.get(f, f"mock_{f}_{seed % 1000}") for f in fields}
    return {"status": "success", "node": node.name, "mock": True}


# ── Public runner ──────────────────────────────────────────────────────────────

async def run_agent(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    """
    Execute an agent node.

    1. Look up the AgentManifest by Agent ID (if configured).
    2. Route to the correct backend based on manifest.runtime_adapter, falling
       back to the API-key heuristic if no manifest exists.
    3. Record an OTEL span for every call.
    """
    cfg = node.config
    agent_id   = cfg.get("Agent ID", node.node_id)
    manifest   = _get_manifest(agent_id)
    workflow_id = getattr(ctx, "workflow_id", "unknown")

    # Determine which backend to use
    adapter = (manifest.runtime_adapter if manifest else None)

    # Override logic: manifest adapter → env-key heuristic → mock
    if adapter == "anthropic" and _ANTHROPIC_KEY:
        use_backend = "anthropic"
    elif adapter == "openai" and _OPENAI_KEY:
        use_backend = "openai"
    elif adapter is None and _ANTHROPIC_KEY:
        use_backend = "anthropic"
    elif adapter is None and _OPENAI_KEY:
        use_backend = "openai"
    else:
        use_backend = "mock"

    model = (
        (manifest.metadata.get("model") if manifest else None)
        or cfg.get("Model", "claude-haiku-4-5-20251001")
    )
    agent_name = manifest.name if manifest else node.name
    cost_in  = manifest.metadata.get("cost_per_1k_input",  0.0) if manifest else 0.0
    cost_out = manifest.metadata.get("cost_per_1k_output", 0.0) if manifest else 0.0

    logger.info(
        "Agent '%s' (id=%s adapter=%s model=%s) → using backend=%s",
        node.name, agent_id, adapter or "auto", model, use_backend,
    )

    t0 = time.perf_counter()
    error_msg: str | None = None
    tokens_in  = 0
    tokens_out = 0

    try:
        if use_backend == "anthropic":
            output, tokens_in, tokens_out = await _call_anthropic(node, ctx, manifest)
        elif use_backend == "openai":
            output, tokens_in, tokens_out = await _call_openai(node, ctx, manifest)
        else:
            output = _mock_agent(node, ctx, manifest)
            # Simulate realistic token counts for the mock so the metrics
            # dashboard has something interesting to display
            tokens_in  = len(json.dumps(ctx.snapshot())) // 4
            tokens_out = len(json.dumps(output)) // 4

    except Exception as exc:
        error_msg = str(exc)
        logger.error("Agent '%s' failed: %s", node.name, exc, exc_info=True)
        # Return a minimal output so the workflow can continue (no hard crash)
        output = {"_error": error_msg, "_agent": node.name}

    latency_ms = (time.perf_counter() - t0) * 1000.0

    # ── OTEL / in-memory telemetry ─────────────────────────────────────────────
    try:
        from ui.backend.observability.otel import record_span
        record_span(
            agent_id=            agent_id,
            agent_name=          agent_name,
            run_id=              run_id,
            workflow_id=         workflow_id,
            backend=             use_backend,
            model=               model,
            latency_ms=          latency_ms,
            tokens_in=           tokens_in,
            tokens_out=          tokens_out,
            success=             error_msg is None,
            error=               error_msg,
            cost_per_1k_input=   cost_in,
            cost_per_1k_output=  cost_out,
        )
    except Exception as otel_exc:
        logger.debug("OTEL record_span failed (non-fatal): %s", otel_exc)

    # Merge agent output into shared context
    ctx.update(output)

    if error_msg:
        return NodeResult(
            output=output,
            error=error_msg,
        )

    return NodeResult(output={
        "_agent":   agent_name,
        "_backend": use_backend,
        "_model":   model,
        **output,
    })
