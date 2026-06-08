"""
Condition node runner — evaluates a boolean expression and routes to "yes" or
"no" outgoing edges.

Expression syntax
-----------------
Expressions may reference ctx fields via the ``ctx.`` prefix or directly:

    ctx.amount_usd > 10000
    ctx.vendor["approved"] == True
    status == "success"          # top-level ctx keys are also in scope
    len(ctx.items) > 0

The evaluator uses a restricted namespace — only ``ctx``, the top-level ctx
keys, and a small set of safe builtins are available.  ``__import__``, ``exec``,
``eval``, and similar are excluded.

Behaviour on error
------------------
If the expression raises *any* exception (NameError, TypeError, etc.) the
runner logs a warning and returns the "no" branch rather than failing the run.
This makes condition nodes safe to use with partially-populated contexts.
"""
from __future__ import annotations

import logging
from typing import Any

from ui.backend.engine.context import ExecutionContext
from ui.backend.engine.executor import NodeResult

logger = logging.getLogger(__name__)

# Builtins safe to expose inside an expression
_SAFE_BUILTINS: dict[str, Any] = {
    "True": True, "False": False, "None": None,
    "abs": abs, "all": all, "any": any,
    "bool": bool, "float": float, "int": int, "len": len,
    "list": list, "max": max, "min": min,
    "round": round, "str": str, "sum": sum,
}


def _eval_expr(expr: str, ctx: ExecutionContext) -> bool:
    namespace: dict[str, Any] = {
        **_SAFE_BUILTINS,
        "ctx": ctx.as_namespace(),
        # Also expose top-level keys directly so expressions like
        # ``amount_usd > 10000`` work alongside ``ctx.amount_usd > 10000``
        **ctx.data,
    }
    result = eval(  # noqa: S307 — restricted namespace, no __builtins__
        compile(expr, "<condition>", "eval"),
        {"__builtins__": {}},
        namespace,
    )
    return bool(result)


async def run_condition(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    expr = (
        node.config.get("Expression")
        or node.config.get("expression")
        or ""
    ).strip()

    if not expr:
        # No expression configured → always take "yes" branch
        return NodeResult(
            output={"expression": "", "result": True, "branch": "yes"},
            next_port="yes",
        )

    try:
        result = _eval_expr(expr, ctx)
    except Exception as exc:
        logger.warning(
            "Condition '%s': expression %r raised %s — defaulting to 'no'",
            node.name, expr, exc,
        )
        result = False

    port = "yes" if result else "no"
    logger.info("Condition '%s': %r → %s", node.name, expr, port)

    return NodeResult(
        output={"expression": expr, "result": result, "branch": port},
        next_port=port,
    )
