"""
Authentication — API keys, JWT session tokens, OAuth2/SSO.

Supports three auth methods:
  1. API key (Bearer af_live_xxx) — for programmatic / CI access
  2. JWT (Bearer eyJ...) — for UI sessions after login
  3. OAuth2 (Google, Okta, Azure AD) — for SSO login flows

All methods resolve to a CurrentUser with org_id + role.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    from jose import JWTError, jwt
    HAS_JOSE = True
except ImportError:
    HAS_JOSE = False

from ui.backend.config import get_settings

bearer = HTTPBearer(auto_error=False)
settings = get_settings()

API_KEY_PREFIX = "af_"


# ── Current user context ──────────────────────────────────────────────────────
@dataclass
class CurrentUser:
    """Resolved identity attached to every authenticated request."""
    user_id: str
    org_id: str
    email: str
    role: str          # viewer | operator | supervisor | admin
    auth_method: str   # "api_key" | "jwt" | "oauth2"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_supervisor_or_above(self) -> bool:
        return self.role in ("supervisor", "admin")

    @property
    def can_approve_hitl(self) -> bool:
        return self.role in ("supervisor", "admin")


# ── API Key helpers ────────────────────────────────────────────────────────────
def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.
    Returns (raw_key, key_hash, key_prefix).
    The raw_key is shown once to the user. Store only key_hash.
    """
    raw = API_KEY_PREFIX + "live_" + secrets.token_urlsafe(32)
    prefix = raw[:12]
    key_hash = _hash_api_key(raw)
    return raw, key_hash, prefix


def _hash_api_key(raw_key: str) -> str:
    """HMAC-SHA256 hash of the raw key using the pepper."""
    return hmac.new(
        settings.api_key_pepper.encode(),
        raw_key.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_api_key(raw_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_hash_api_key(raw_key), stored_hash)


# ── JWT helpers ────────────────────────────────────────────────────────────────
def create_access_token(data: dict, expires_minutes: int | None = None) -> str:
    if not HAS_JOSE:
        raise RuntimeError("pip install python-jose[cryptography]")
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.jwt_access_token_expire_minutes
    )
    return jwt.encode(
        {**data, "exp": expire, "iat": datetime.now(timezone.utc)},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def create_refresh_token(data: dict) -> str:
    return create_access_token(data, expires_minutes=settings.jwt_refresh_token_expire_days * 24 * 60)


def decode_token(token: str) -> dict:
    if not HAS_JOSE:
        raise RuntimeError("pip install python-jose[cryptography]")
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ── FastAPI dependencies ───────────────────────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer),
) -> CurrentUser:
    """
    Resolve the current user from a Bearer token.
    Accepts both JWT and API key tokens.

    In development (no credentials), returns a default admin user
    so the UI works without configuring auth.
    """
    if credentials is None:
        if settings.environment == "development":
            return _dev_user()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # API key: starts with known prefix
    if token.startswith(API_KEY_PREFIX):
        return await _resolve_api_key(token)

    # JWT
    return _resolve_jwt(token)


def _dev_user() -> CurrentUser:
    """Default user for local development — admin on the default org."""
    return CurrentUser(
        user_id="dev-user",
        org_id=settings.default_org_id,
        email="dev@localhost",
        role="admin",
        auth_method="dev",
    )


def _resolve_jwt(token: str) -> CurrentUser:
    payload = decode_token(token)
    return CurrentUser(
        user_id=payload.get("sub", ""),
        org_id=payload.get("org_id", settings.default_org_id),
        email=payload.get("email", ""),
        role=payload.get("role", "viewer"),
        auth_method="jwt",
    )


async def _resolve_api_key(raw_key: str) -> CurrentUser:
    """
    Validate an API key against the database.
    In-memory fallback for development when no DB is configured.
    """
    # Full implementation queries DB; this is the interface contract.
    # Replace _lookup_api_key_from_db with a real DB lookup in production.
    key_hash = _hash_api_key(raw_key)

    # Fallback: check in-memory dev keys
    dev_key = _DEV_API_KEYS.get(key_hash)
    if dev_key:
        return dev_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


# Dev API key store (populated on startup for local testing)
_DEV_API_KEYS: dict[str, CurrentUser] = {}


def register_dev_api_key(raw_key: str, user: CurrentUser) -> None:
    """Register an in-memory API key for development/testing."""
    _DEV_API_KEYS[_hash_api_key(raw_key)] = user


# ── Role guards ────────────────────────────────────────────────────────────────
def require_role(*roles: str):
    """Dependency factory: require one of the given roles."""
    async def _check(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not permitted. Required: {list(roles)}",
            )
        return user
    return _check


require_admin = require_role("admin")
require_supervisor = require_role("supervisor", "admin")
require_operator = require_role("operator", "supervisor", "admin")
require_viewer = require_role("viewer", "operator", "supervisor", "admin")


# ── OAuth2 redirect helpers ────────────────────────────────────────────────────
def google_auth_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": settings.oauth_google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
    }
    from urllib.parse import urlencode
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def okta_auth_url(redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": settings.oauth_okta_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    }
    domain = settings.oauth_okta_domain
    return f"https://{domain}/oauth2/v1/authorize?" + urlencode(params)
