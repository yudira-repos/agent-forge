"""
Governance engine singleton — shared across the whole process.

Instantiated once at import time so both main.py and the agent runner
share the same policy engine instance.  main.py previously created
``gov_engine`` locally but never called ``evaluate()``; this module
closes that gap by making the instance importable from anywhere.

Usage::

    from ui.backend.governance_singleton import gov_engine
    decision = gov_engine.evaluate(PolicyContext(...))
    if decision.is_denied:
        raise PermissionError(decision.reason)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from agentforge.governance import SOC2Profile
    gov_engine = SOC2Profile.engine()
    logger.info("Governance: SOC2Profile engine initialised (%d policies)", gov_engine.policy_count())
except Exception as exc:
    # Graceful degradation — if agentforge isn't importable the rest of the
    # server still starts, but governance checks will be no-ops.
    logger.warning("Governance engine unavailable (%s) — all requests will be ALLOWED", exc)

    class _NoOpEngine:
        def evaluate(self, ctx: object) -> "_NoOpDecision":
            return _NoOpDecision()
        def policy_count(self) -> int:
            return 0

    class _NoOpDecision:
        is_allowed = True
        is_denied = False
        requires_hitl = False
        reason = "governance unavailable"

    gov_engine = _NoOpEngine()  # type: ignore[assignment]
