"""
ExecutionContext — the mutable data envelope that flows through the workflow DAG.

Each node reads from and writes to this shared context.  Downstream nodes can
reference any field set by an upstream node, enabling true data pipelines:

    trigger → ctx.invoice_raw = {...}
    agent   → ctx.vendor_id = "V-12345", ctx.amount_usd = 15000
    api     → ctx.vendor = {"name": "Acme", "approved": True}
    cond    → eval("ctx.amount_usd > 10000") → True  →  "yes" branch
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class ExecutionContext:
    run_id: str
    workflow_id: str
    workflow_name: str
    # All data flows through here — nodes read from and write to this dict
    data: dict[str, Any] = field(default_factory=dict)

    # ── Read / write helpers ──────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def update(self, d: dict[str, Any]) -> None:
        self.data.update(d)

    def snapshot(self) -> dict[str, Any]:
        """Return a shallow copy of the current context data."""
        return dict(self.data)

    def as_namespace(self) -> SimpleNamespace:
        """
        Return data as a SimpleNamespace so expressions like
        ``ctx.amount_usd > 10000`` work in condition/transform eval.
        """
        return SimpleNamespace(**self.data)

    def render_template(self, template: str) -> str:
        """
        Substitute ``{key}`` placeholders from ctx.data.

        Example::
            url = "sap.internal/vendors/{vendor_id}"
            ctx.render_template(url)  →  "sap.internal/vendors/V-12345"
        """
        try:
            return template.format_map(self.data)
        except (KeyError, ValueError):
            return template
