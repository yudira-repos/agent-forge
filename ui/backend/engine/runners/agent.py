"""
Agent node runner — executes an AI agent step.

Priority order:
  1. Anthropic (ANTHROPIC_API_KEY set)       → real Claude call
  2. OpenAI   (OPENAI_API_KEY set)           → real GPT call
  3. Neither                                  → deterministic mock (no keys needed)

The runner merges the agent's JSON output into the shared ExecutionContext so
downstream nodes can reference any extracted field (e.g. ctx.vendor_id).
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
_OPENAI_KEY: str = os.environ.get("OPENAI_API_KEY", "")


# ── Prompt construction ───────────────────────────────────────────────────────

def _system_prompt(node: Any) -> str:
    cfg = node.config
    parts: list[str] = [
        f"You are an AI agent named '{node.name}' inside an automated enterprise workflow.",
        "Be precise, concise, and deterministic.",
        "Return ONLY a valid JSON object — no markdown, no explanation, no code fences.",
    ]
    if cfg.get("Output"):
        parts.append(
            f"Your response JSON must include these fields: {cfg['Output']}."
        )
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


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a raw LLM response (strips code fences etc.)."""
    text = text.strip()

    # Strip markdown fences  ```json ... ```
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()

    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {"result": result}
    except json.JSONDecodeError:
        # Last resort: find the first {...} block
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {"result": text}


# ── Real LLM backends ─────────────────────────────────────────────────────────

async def _call_anthropic(node: Any, ctx: ExecutionContext) -> dict[str, Any]:
    import anthropic  # already in Dockerfile

    model = node.config.get("Model", "claude-haiku-4-5-20251001")
    # Guard against unsupported model strings that might come from the designer
    if not model.startswith("claude"):
        model = "claude-haiku-4-5-20251001"

    client = anthropic.AsyncAnthropic(api_key=_ANTHROPIC_KEY)
    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=_system_prompt(node),
        messages=[{"role": "user", "content": _user_message(node, ctx)}],
    )
    raw = response.content[0].text if response.content else "{}"
    return _extract_json(raw)


async def _call_openai(node: Any, ctx: ExecutionContext) -> dict[str, Any]:
    import openai  # already in Dockerfile

    model = node.config.get("Model", "gpt-4o-mini")
    if not model.startswith("gpt"):
        model = "gpt-4o-mini"

    client = openai.AsyncOpenAI(api_key=_OPENAI_KEY)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _system_prompt(node)},
            {"role": "user", "content": _user_message(node, ctx)},
        ],
        max_tokens=1024,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    return _extract_json(raw)


# ── Deterministic mock (no API key required) ──────────────────────────────────

def _mock_agent(node: Any, ctx: ExecutionContext) -> dict[str, Any]:
    """
    Returns realistic-looking output deterministically seeded from the node name.
    Designed so the demo workflow produces coherent data end-to-end:
      - Extract invoice  → vendor_id, amount_usd, items
      - Process payment  → payment_id, transaction_id, status
    """
    import hashlib
    cfg = node.config
    seed = int(hashlib.md5(node.name.encode()).hexdigest()[:8], 16)

    # Carry forward useful values already in ctx
    vendor_id = ctx.get("vendor_id", f"V-{seed % 90000 + 10000}")
    amount_usd = ctx.get("amount_usd", round(seed % 50000 + 1000.0, 2))

    # Well-known mock values per field name
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
        "payment_id":      f"PAY-{seed % 900000 + 100000}",
        "transaction_id":  f"TXN-{seed % 9000000 + 1000000}",
        "status":          "success",
        "approved":        True,
        "confidence":      round(0.85 + (seed % 15) / 100, 2),
        "processed_at":    time.time(),
        "message":         f"Processed by {node.name} (mock)",
    }

    # Use "Output" config to decide which fields to return
    output_spec = cfg.get("Output", "")
    fields = [f.strip() for f in output_spec.split(",") if f.strip()]

    if fields:
        return {f: _values.get(f, f"mock_{f}_{seed % 1000}") for f in fields}

    # Fallback — return all well-known values for this node
    return {
        "status": "success",
        "node": node.name,
        "agent_id": cfg.get("Agent ID", "unknown"),
        "mock": True,
    }


# ── Public runner ─────────────────────────────────────────────────────────────

async def run_agent(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    model = node.config.get("Model", "claude-haiku-4-5-20251001")

    if _ANTHROPIC_KEY:
        logger.info("Agent '%s': calling Anthropic (%s)", node.name, model)
        output = await _call_anthropic(node, ctx)
        backend = "anthropic"
    elif _OPENAI_KEY:
        logger.info("Agent '%s': calling OpenAI (%s)", node.name, model)
        output = await _call_openai(node, ctx)
        backend = "openai"
    else:
        logger.info("Agent '%s': using deterministic mock (no API key)", node.name)
        output = _mock_agent(node, ctx)
        backend = "mock"

    # Merge into shared context so downstream nodes can access extracted fields
    ctx.update(output)

    return NodeResult(output={"_agent": node.name, "_backend": backend, **output})
