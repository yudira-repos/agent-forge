"""
Health and readiness endpoints.

/health  — liveness: is the process alive?
/ready   — readiness: are all dependencies up?
/metrics — Prometheus metrics (scrape endpoint)

Kubernetes liveness probe → /health
Kubernetes readiness probe → /ready
Prometheus scrape          → /metrics
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response, status

from ui.backend.config import get_settings

router = APIRouter(tags=["ops"])
_start_time = time.time()


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe — returns 200 if process is alive."""
    return {
        "status": "ok",
        "version": get_settings().app_version,
        "uptime_seconds": round(time.time() - _start_time),
    }


@router.get("/ready")
async def ready(response: Response) -> dict[str, Any]:
    """
    Readiness probe — checks all critical dependencies.
    Returns 200 if ready to serve traffic, 503 if not.
    """
    checks: dict[str, str] = {}
    healthy = True

    # Database check
    try:
        from ui.backend.db.session import get_engine
        from sqlalchemy import text
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if healthy else "degraded",
        "checks": checks,
        "version": get_settings().app_version,
    }


@router.get("/version")
async def version() -> dict[str, str]:
    s = get_settings()
    return {
        "version": s.app_version,
        "environment": s.environment,
        "app": s.app_name,
    }
