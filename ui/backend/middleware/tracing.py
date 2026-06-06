"""
OpenTelemetry distributed tracing.

Traces every agent invocation, governance decision, and HITL event
end-to-end — so you can correlate a HITL approval back to the exact
tool call that triggered it across service boundaries.
"""

from __future__ import annotations

from ui.backend.config import get_settings


def configure_tracing() -> None:
    """
    Initialize OpenTelemetry SDK and instrument FastAPI.
    No-ops if otel_endpoint is not configured.
    """
    settings = get_settings()
    if not settings.otel_endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({
            "service.name": settings.otel_service_name,
            "service.version": settings.app_version,
            "deployment.environment": settings.environment,
        })

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor().instrument()
        SQLAlchemyInstrumentor().instrument()

    except ImportError:
        pass  # OTel packages not installed — tracing disabled


def get_tracer(name: str = "agentforge"):
    """Get a tracer instance for manual span creation."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoOpTracer()


class _NoOpTracer:
    """Fallback when OTel is not installed."""
    def start_as_current_span(self, name: str, **_):
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            yield None
        return _noop()
