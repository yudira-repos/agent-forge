"""
API node runner — executes an outbound HTTP call.

For URLs that are publicly reachable (https:// or http://) the runner makes
a real async HTTP request via httpx.

For internal/mock URLs (e.g. ``sap.internal/vendors/{vendor_id}``, common in
demo workflows) it returns realistic deterministic mock data so the workflow
can run end-to-end without real infrastructure.

Context template substitution is applied to the URL and body before the
request is sent, so a URL like::

    sap.internal/vendors/{vendor_id}

becomes::

    sap.internal/vendors/V-12345

if ``ctx.vendor_id == "V-12345"``.
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


# ── Timeout helper ────────────────────────────────────────────────────────────

def _timeout_s(cfg: dict[str, Any]) -> float:
    raw = str(cfg.get("Timeout", "10s"))
    m = re.match(r"(\d+(?:\.\d+)?)\s*(ms|s|m)?", raw)
    if not m:
        return 10.0
    val, unit = float(m.group(1)), (m.group(2) or "s")
    if unit == "ms":
        return val / 1000
    if unit == "m":
        return val * 60
    return val


# ── Real HTTP call ────────────────────────────────────────────────────────────

async def _real_http(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any,
    timeout: float,
) -> dict[str, Any]:
    import httpx  # lazy — not available in all environments
    async with httpx.AsyncClient(timeout=timeout) as client:
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            data = {"text": response.text}
        return {"status": response.status_code, "response": data}


# ── Deterministic mock for internal/demo URLs ─────────────────────────────────

def _mock_response(node: Any, url: str, method: str, ctx: ExecutionContext) -> dict[str, Any]:
    import hashlib
    seed = int(hashlib.md5(url.encode()).hexdigest()[:8], 16)

    url_lower = url.lower()
    vendor_id = ctx.get("vendor_id", f"V-{seed % 90000 + 10000}")

    if "vendor" in url_lower:
        return {
            "vendor": {
                "id": vendor_id,
                "name": "Acme Corporation",
                "status": "approved",
                "credit_limit": round(seed % 500000 + 50000.0, 2),
                "payment_terms": "NET-30",
                "country": "US",
            },
            "approved": True,
        }
    if "record" in url_lower or "erp" in url_lower:
        return {
            "record_id": f"REC-{seed % 900000 + 100000}",
            "status": "updated",
            "updated_at": time.time(),
        }
    if "payment" in url_lower:
        return {
            "payment_id": f"PAY-{seed % 900000 + 100000}",
            "status": "processed",
            "gateway": "mock-gateway",
            "processed_at": time.time(),
        }
    if "user" in url_lower or "auth" in url_lower:
        return {
            "user_id": f"USR-{seed % 9000 + 1000}",
            "authenticated": True,
            "roles": ["viewer", "approver"],
        }
    # Generic success
    return {
        "status": "ok",
        "node": node.name,
        "url": url,
        "method": method,
        "timestamp": time.time(),
    }


# ── Public runner ─────────────────────────────────────────────────────────────

async def run_api(node: Any, ctx: ExecutionContext, run_id: str) -> NodeResult:
    cfg = node.config
    url_template = cfg.get("URL", cfg.get("url", ""))
    method = cfg.get("Method", cfg.get("method", "GET")).upper()
    timeout = _timeout_s(cfg)

    # Template substitution: {vendor_id} → ctx.vendor_id
    url = ctx.render_template(url_template)

    # Build headers
    headers: dict[str, str] = {}
    auth_type = cfg.get("Auth", "")
    if auth_type.startswith("Bearer"):
        token = ctx.get("api_token") or os.environ.get("API_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif auth_type.startswith("Basic"):
        import base64
        creds = os.environ.get("API_CREDENTIALS", "user:pass")
        encoded = base64.b64encode(creds.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"

    # Build body if configured
    body: Any = None
    raw_body = cfg.get("Body")
    if raw_body:
        if isinstance(raw_body, dict):
            # Template-substitute string values in the body dict
            body = {
                k: ctx.render_template(v) if isinstance(v, str) else v
                for k, v in raw_body.items()
            }
        else:
            rendered = ctx.render_template(str(raw_body))
            try:
                body = json.loads(rendered)
            except json.JSONDecodeError:
                body = {"data": rendered}

    is_real_url = url.startswith("http://") or url.startswith("https://")

    if is_real_url:
        logger.info("API '%s': %s %s", node.name, method, url)
        result = await _real_http(method, url, headers, body, timeout)
        if isinstance(result.get("response"), dict):
            ctx.update(result["response"])
        return NodeResult(output={"_api": node.name, "url": url, "method": method, **result})
    else:
        logger.info("API '%s': mock response for internal URL '%s'", node.name, url)
        mock = _mock_response(node, url, method, ctx)
        ctx.update(mock)
        return NodeResult(output={"_api": node.name, "url": url, "method": method, "status": 200, **mock})
