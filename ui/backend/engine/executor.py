"""
Execution dispatcher — applies timeout, structured error handling, and routes
each WFNode to its registered runner function.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from ui.backend.engine.context import ExecutionContext

logger = logging.getLogger(__name__)

# Default per-node wall-clock timeout (seconds).  Node config can override via
# a "Timeout" key (e.g. "Timeout": "60s").
DEFAULT_TIMEOUT = 60.0


@dataclass
class NodeResult:
    """Everything a runner returns to the DAG walker."""
    output: dict[str, Any] = field(default_factory=dict)
    # "yes" / "no" for condition nodes; None means follow all outgoing edges.
    next_port: str | None = None
    error: str | None = None


# Type alias for runner callables
RunnerFn = Callable[..., Awaitable[NodeResult]]

# Populated by runners/__init__.py after import
RUNNERS: dict[str, RunnerFn] = {}


def _parse_timeout(cfg: dict[str, Any]) -> float:
    """Parse node-level timeout config: '30s', '2m', '500ms' → seconds."""
    import re
    raw = str(cfg.get("Timeout", cfg.get("timeout", "")))
    m = re.match(r"(\d+(?:\.\d+)?)\s*(ms|s|m)?", raw)
    if not m:
        return DEFAULT_TIMEOUT
    val, unit = float(m.group(1)), (m.group(2) or "s")
    if unit == "ms":
        return val / 1000
    elif unit == "m":
        return val * 60
    return val


async def run_node(
    node: Any,
    ctx: ExecutionContext,
    run_id: str,
) -> NodeResult:
    """
    Dispatch ``node`` to the appropriate runner.

    * Applies a per-node wall-clock timeout (from node.config or DEFAULT_TIMEOUT).
    * Wraps all exceptions into NodeResult.error so the caller always gets a
      structured result rather than an uncaught exception.
    """
    runner = RUNNERS.get(node.node_type)
    if runner is None:
        return NodeResult(
            output={"skipped": True, "reason": f"No runner for node type '{node.node_type}'"},
        )

    timeout = _parse_timeout(node.config)

    try:
        result = await asyncio.wait_for(runner(node, ctx, run_id), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        msg = f"Node '{node.name}' timed out after {timeout:.0f}s"
        logger.warning(msg)
        return NodeResult(error=msg)
    except Exception as exc:
        logger.exception("Unhandled error in node '%s'", node.name)
        return NodeResult(error=str(exc))
