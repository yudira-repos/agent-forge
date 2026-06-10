"""
AgentForge OTEL observability module.

Two-tier design
---------------
1. **In-memory span store** (always available, zero dependencies)
   Every agent call is captured as a SpanRecord.  The store keeps the last
   2 000 spans and supports O(1) aggregation queries.  This is what the
   live orchestration dashboard reads.

2. **OpenTelemetry SDK** (optional, enabled when ``opentelemetry-sdk`` is
   installed).  If the SDK is present, proper Tracer/Meter providers are
   wired up and, if ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, spans and
   metrics are exported to the configured OTLP endpoint (e.g. Grafana
   Cloud, Honeycomb, Datadog, Railway's built-in OTEL receiver).

Usage from the agent runner
---------------------------
    from ui.backend.observability.otel import record_span

    record_span(
        agent_id="claude-invoice-extractor",
        agent_name="Invoice Extractor (Claude)",
        run_id="run-abc123",
        workflow_id="wf-multiagent-001",
        backend="anthropic",
        model="claude-haiku-4-5-20251001",
        latency_ms=342.7,
        tokens_in=512,
        tokens_out=128,
        success=True,
        error=None,
    )

Querying metrics
----------------
    from ui.backend.observability.otel import get_agent_metrics, get_run_spans

    metrics = get_agent_metrics()          # list[AgentMetrics]
    spans   = get_run_spans("run-abc123")  # list[SpanRecord]
"""

from __future__ import annotations

import collections
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Optional OTEL SDK wiring ───────────────────────────────────────────────────

_OTEL_AVAILABLE = False
_tracer: Any = None

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME

    _resource = Resource(attributes={SERVICE_NAME: "agentforge"})
    _provider = TracerProvider(resource=_resource)

    # Always add a console exporter so spans appear in Railway logs
    _provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    # OTLP export when endpoint is configured
    _otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if _otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            # Explicitly parse OTEL_EXPORTER_OTLP_HEADERS — some SDK versions
            # don't auto-read it when the exporter is given an explicit endpoint,
            # causing silent 401s to Honeycomb that BatchSpanProcessor swallows.
            # Format expected: "key1=value1,key2=value2"
            _raw_headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            _headers: dict[str, str] = {}
            for _pair in _raw_headers.split(","):
                _pair = _pair.strip()
                if "=" in _pair:
                    _k, _v = _pair.split("=", 1)
                    _headers[_k.strip()] = _v.strip()

            # Honeycomb requires the full /v1/traces path on the endpoint
            _traces_endpoint = _otlp_endpoint.rstrip("/")
            if not _traces_endpoint.endswith("/v1/traces"):
                _traces_endpoint += "/v1/traces"

            _otlp_exporter = OTLPSpanExporter(
                endpoint=_traces_endpoint,
                headers=_headers if _headers else None,
            )
            _provider.add_span_processor(BatchSpanProcessor(_otlp_exporter))
            logger.info(
                "OTEL: OTLP exporter active → %s  auth_headers=%s",
                _traces_endpoint, list(_headers.keys()),
            )
        except Exception as exc:
            logger.warning("OTEL: OTLP exporter failed to init (%s)", exc)

    trace.set_tracer_provider(_provider)
    _tracer = trace.get_tracer("agentforge.runner", "0.1.0")
    _OTEL_AVAILABLE = True
    logger.info("OTEL: TracerProvider ready (SDK available)")

except ImportError:
    logger.info("OTEL: opentelemetry-sdk not installed — using in-memory store only")


# ── In-memory span store ───────────────────────────────────────────────────────

_MAX_SPANS = 2_000   # rolling window


@dataclass
class SpanRecord:
    """One recorded agent invocation."""
    span_id:     str
    agent_id:    str
    agent_name:  str
    run_id:      str
    workflow_id: str
    backend:     str        # anthropic | openai | mock
    model:       str
    started_at:  float      # epoch seconds
    latency_ms:  float
    tokens_in:   int
    tokens_out:  int
    success:     bool
    error:       str | None = None
    # estimated USD cost (computed from registry metadata at record time)
    cost_usd:    float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id":    self.span_id,
            "agent_id":   self.agent_id,
            "agent_name": self.agent_name,
            "run_id":     self.run_id,
            "workflow_id": self.workflow_id,
            "backend":    self.backend,
            "model":      self.model,
            "started_at": self.started_at,
            "latency_ms": round(self.latency_ms, 1),
            "tokens_in":  self.tokens_in,
            "tokens_out": self.tokens_out,
            "success":    self.success,
            "error":      self.error,
            "cost_usd":   round(self.cost_usd, 6),
        }


@dataclass
class AgentMetrics:
    """Aggregated metrics for a single agent_id."""
    agent_id:    str
    agent_name:  str
    backend:     str
    model:       str
    invocations: int = 0
    successes:   int = 0
    failures:    int = 0
    total_latency_ms: float = 0.0
    total_tokens_in:  int = 0
    total_tokens_out: int = 0
    total_cost_usd:   float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return round(self.total_latency_ms / self.invocations, 1) if self.invocations else 0.0

    @property
    def error_rate(self) -> float:
        return round(self.failures / self.invocations, 4) if self.invocations else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id":       self.agent_id,
            "agent_name":     self.agent_name,
            "backend":        self.backend,
            "model":          self.model,
            "invocations":    self.invocations,
            "successes":      self.successes,
            "failures":       self.failures,
            "avg_latency_ms": self.avg_latency_ms,
            "total_tokens_in":  self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "error_rate":     self.error_rate,
        }


# The rolling span store — a deque keeps insertion O(1) and auto-evicts old spans
_spans: collections.deque[SpanRecord] = collections.deque(maxlen=_MAX_SPANS)

# Per-agent running aggregates — updated in O(1) on every record_span call
_agent_agg: dict[str, AgentMetrics] = {}


# ── Public API ─────────────────────────────────────────────────────────────────

import uuid as _uuid


def record_span(
    *,
    agent_id:    str,
    agent_name:  str,
    run_id:      str,
    workflow_id: str,
    backend:     str,
    model:       str,
    latency_ms:  float,
    tokens_in:   int,
    tokens_out:  int,
    success:     bool,
    error:       str | None = None,
    cost_per_1k_input:  float = 0.0,
    cost_per_1k_output: float = 0.0,
) -> SpanRecord:
    """
    Record one agent invocation.

    Called by the agent runner at the end of every LLM call, regardless of
    success or failure.  Thread-safe for asyncio (single-threaded event loop).
    """
    cost = (tokens_in  * cost_per_1k_input  / 1000.0
          + tokens_out * cost_per_1k_output / 1000.0)

    record = SpanRecord(
        span_id=    _uuid.uuid4().hex[:16],
        agent_id=   agent_id,
        agent_name= agent_name,
        run_id=     run_id,
        workflow_id=workflow_id,
        backend=    backend,
        model=      model,
        started_at= time.time() - latency_ms / 1000.0,
        latency_ms= latency_ms,
        tokens_in=  tokens_in,
        tokens_out= tokens_out,
        success=    success,
        error=      error,
        cost_usd=   cost,
    )
    _spans.append(record)

    # ── Async DB write (fire-and-forget, survives restarts) ───────────────────
    # We try to schedule a DB write only if we're already inside an asyncio
    # event loop (i.e. called from an async context like the LangGraph runner).
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        if loop is not None:
            async def _persist(r: SpanRecord) -> None:
                try:
                    from ui.backend.db.sqlite_store import write_span
                    await write_span(r.to_dict())
                except Exception:
                    pass
            loop.create_task(_persist(record))
    except RuntimeError:
        pass  # no event loop — local unit test, skip DB write

    # Update rolling aggregates
    agg = _agent_agg.setdefault(agent_id, AgentMetrics(
        agent_id=agent_id, agent_name=agent_name, backend=backend, model=model,
    ))
    agg.invocations      += 1
    agg.successes        += 1 if success else 0
    agg.failures         += 0 if success else 1
    agg.total_latency_ms += latency_ms
    agg.total_tokens_in  += tokens_in
    agg.total_tokens_out += tokens_out
    agg.total_cost_usd   += cost

    # ── Optional OTEL SDK span ────────────────────────────────────────────────
    if _OTEL_AVAILABLE and _tracer:
        try:
            from opentelemetry import trace as _trace
            from opentelemetry.trace import SpanKind, StatusCode
            with _tracer.start_as_current_span(
                "agentforge.agent.invoke",
                kind=SpanKind.CLIENT,
            ) as span:
                span.set_attribute("agent.id",          agent_id)
                span.set_attribute("agent.name",        agent_name)
                span.set_attribute("agent.backend",     backend)
                span.set_attribute("llm.model",         model)
                span.set_attribute("workflow.run_id",   run_id)
                span.set_attribute("workflow.id",       workflow_id)
                span.set_attribute("llm.tokens.input",  tokens_in)
                span.set_attribute("llm.tokens.output", tokens_out)
                span.set_attribute("llm.latency_ms",    latency_ms)
                span.set_attribute("llm.cost_usd",      cost)
                if not success and error:
                    span.set_status(StatusCode.ERROR, error)
                    span.set_attribute("error.message", error)
        except Exception:
            pass   # never let OTEL crash the runner

    return record


def get_agent_metrics() -> list[dict[str, Any]]:
    """Return aggregated metrics for every agent seen so far."""
    return [m.to_dict() for m in sorted(
        _agent_agg.values(), key=lambda m: m.invocations, reverse=True
    )]


def get_run_spans(run_id: str) -> list[dict[str, Any]]:
    """Return all spans recorded for a specific workflow run, oldest first."""
    return [s.to_dict() for s in _spans if s.run_id == run_id]


def get_recent_spans(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent N spans across all runs."""
    recent = list(_spans)[-limit:]
    return [s.to_dict() for s in reversed(recent)]


def get_live_runs() -> list[dict[str, Any]]:
    """
    Return one entry per distinct run_id seen in the last 60 seconds.
    Used by the SSE endpoint to show active executions.
    """
    cutoff = time.time() - 60
    seen: dict[str, dict[str, Any]] = {}
    for s in _spans:
        if s.started_at >= cutoff:
            entry = seen.setdefault(s.run_id, {
                "run_id":      s.run_id,
                "workflow_id": s.workflow_id,
                "agents":      [],
                "total_latency_ms": 0.0,
                "total_tokens":     0,
                "total_cost_usd":   0.0,
            })
            entry["agents"].append({
                "agent_id":   s.agent_id,
                "agent_name": s.agent_name,
                "backend":    s.backend,
                "latency_ms": s.latency_ms,
                "tokens":     s.tokens_in + s.tokens_out,
                "success":    s.success,
                "started_at": s.started_at,
            })
            entry["total_latency_ms"] += s.latency_ms
            entry["total_tokens"]     += s.tokens_in + s.tokens_out
            entry["total_cost_usd"]   += s.cost_usd
    return list(seen.values())


def otel_status() -> dict[str, Any]:
    """Return observability health info for the /health endpoint."""
    return {
        "otel_sdk":       _OTEL_AVAILABLE,
        "otlp_endpoint":  os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        "spans_in_store": len(_spans),
        "agents_tracked": len(_agent_agg),
    }
