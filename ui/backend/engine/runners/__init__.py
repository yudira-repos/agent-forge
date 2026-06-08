"""
Runner registry — maps node_type strings to async runner callables.

Import order matters: runners are imported here so that executor.RUNNERS is
fully populated before any workflow execution starts.
"""
from ui.backend.engine.executor import RUNNERS
from ui.backend.engine.runners.agent import run_agent
from ui.backend.engine.runners.api import run_api
from ui.backend.engine.runners.condition import run_condition
from ui.backend.engine.runners.simple import (
    run_event,
    run_loop,
    run_subwf,
    run_transform,
    run_trigger,
)

RUNNERS.update({
    "trigger":   run_trigger,
    "agent":     run_agent,
    "api":       run_api,
    "condition": run_condition,
    "transform": run_transform,
    "event":     run_event,
    "loop":      run_loop,
    "subwf":     run_subwf,
})

__all__ = ["RUNNERS"]
