"""
Central configuration — reads from environment variables / .env file.
All secrets live here and nowhere else in the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────────────────────
    app_name: str = "AgentForge"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    # ── Database ───────────────────────────────────────────────────────────
    # Postgres in production; sqlite:///./agentforge.db for local dev
    database_url: str = "sqlite+aiosqlite:///./agentforge.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # ── Auth ───────────────────────────────────────────────────────────────
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    jwt_secret_key: str = "CHANGE_ME_IN_PRODUCTION_use_secrets_token_hex_32"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30

    # API key hashing pepper
    api_key_pepper: str = "CHANGE_ME_IN_PRODUCTION"

    # ── OAuth2 / SSO ───────────────────────────────────────────────────────
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    oauth_okta_domain: str = ""          # e.g. "acme.okta.com"
    oauth_okta_client_id: str = ""
    oauth_okta_client_secret: str = ""
    oauth_azure_tenant_id: str = ""
    oauth_azure_client_id: str = ""
    oauth_azure_client_secret: str = ""

    # ── Signing (AgentForge AIAM) ──────────────────────────────────────────
    # Used to sign agent credentials and delegation tokens
    aiam_signing_secret: str = "CHANGE_ME_IN_PRODUCTION_aiam_signing_key"

    # ── HITL Notifications ─────────────────────────────────────────────────
    slack_webhook_url: str = ""          # Incoming webhook URL
    slack_bot_token: str = ""            # For rich Slack API calls
    slack_approval_channel: str = "#agent-approvals"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "agentforge@yourdomain.com"
    smtp_tls: bool = True

    pagerduty_routing_key: str = ""      # Events API v2 routing key

    # ── Observability ──────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = True                # False = pretty console logs
    otel_endpoint: str = ""              # e.g. "http://otel-collector:4317"
    otel_service_name: str = "agentforge"
    prometheus_enabled: bool = True

    # ── Rate limiting ──────────────────────────────────────────────────────
    rate_limit_per_minute: int = 120     # per IP / API key
    rate_limit_burst: int = 20

    # ── Multi-tenancy ──────────────────────────────────────────────────────
    default_org_id: str = "default"
    max_orgs: int = 1000

    # ── Marketplace / SaaS ─────────────────────────────────────────────────
    saas_mode: bool = False              # True = hosted SaaS, False = self-hosted
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    aws_marketplace_product_code: str = ""

    # ── CORS ───────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]

    @field_validator("jwt_secret_key", "aiam_signing_secret")
    @classmethod
    def warn_default_secrets(cls, v: str) -> str:
        if v.startswith("CHANGE_ME"):
            import warnings
            warnings.warn(
                "Using default secret key — set a strong random value in .env before production!",
                UserWarning,
                stacklevel=2,
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def aiam_signing_secret_bytes(self) -> bytes:
        return self.aiam_signing_secret.encode()


@lru_cache
def get_settings() -> Settings:
    return Settings()
