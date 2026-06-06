"""
SQLAlchemy ORM models — the persistent schema for AgentForge.

Tables:
  orgs             — multi-tenant organisations
  users            — human users with roles
  api_keys         — hashed API keys for programmatic access
  agent_manifests  — registry of all agents
  audit_events     — append-only audit log
  hitl_requests    — HITL approval queue
  hitl_decisions   — resolved HITL decisions
  policies         — governance policies
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


# Use JSONB on Postgres, JSON on SQLite
JsonType = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    pass


# ── Orgs ──────────────────────────────────────────────────────────────────────
class Org(Base):
    """A tenant organisation (company, team, or individual account)."""
    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="free")  # free | pro | enterprise
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)

    users: Mapped[list["User"]] = relationship(back_populates="org")
    agents: Mapped[list["AgentManifestRow"]] = relationship(back_populates="org")
    api_keys: Mapped[list["APIKey"]] = relationship(back_populates="org")


# ── Users ─────────────────────────────────────────────────────────────────────
class User(Base):
    """A human user who can log in to the console."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(50), default="viewer")  # viewer|operator|supervisor|admin
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    oauth_provider: Mapped[str | None] = mapped_column(String(50))  # google|okta|azure
    oauth_subject: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org: Mapped["Org"] = relationship(back_populates="users")

    __table_args__ = (UniqueConstraint("org_id", "email"),)


# ── API Keys ──────────────────────────────────────────────────────────────────
class APIKey(Base):
    """Hashed API key for programmatic / service-to-service access."""
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)  # e.g. "af_live_AbCd"
    role: Mapped[str] = mapped_column(String(50), default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org: Mapped["Org"] = relationship(back_populates="api_keys")


# ── Agent Registry ────────────────────────────────────────────────────────────
class AgentManifestRow(Base):
    """Persistent storage for AgentManifest objects."""
    __tablename__ = "agent_manifests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    org_id: Mapped[str] = mapped_column(ForeignKey("orgs.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    runtime_adapter: Mapped[str] = mapped_column(String(50), default="anthropic")
    status: Mapped[str] = mapped_column(String(50), default="active")
    tags: Mapped[list] = mapped_column(JsonType, default=list)
    capabilities: Mapped[list] = mapped_column(JsonType, default=list)
    required_roles: Mapped[list] = mapped_column(JsonType, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    org: Mapped["Org"] = relationship(back_populates="agents")

    __table_args__ = (
        UniqueConstraint("org_id", "agent_id"),
        Index("ix_agent_manifests_org_status", "org_id", "status"),
        Index("ix_agent_manifests_org_owner", "org_id", "owner"),
    )


# ── Audit Events ──────────────────────────────────────────────────────────────
class AuditEventRow(Base):
    """
    Append-only audit log.

    Never UPDATE or DELETE rows — this table is the tamper-evident record.
    Previous_event_id chains events within the same correlation_id.
    """
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    previous_event_id: Mapped[str | None] = mapped_column(String(36))
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_audit_org_ts", "org_id", "timestamp"),
        Index("ix_audit_org_agent", "org_id", "agent_id"),
        Index("ix_audit_correlation", "correlation_id"),
        Index("ix_audit_severity", "org_id", "severity"),
    )


# ── HITL Requests ─────────────────────────────────────────────────────────────
class HITLRequestRow(Base):
    """A pending or resolved human-in-the-loop approval request."""
    __tablename__ = "hitl_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str] = mapped_column(String(255), nullable=False)
    context: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    reviewer_id: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    resolved_at: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("ix_hitl_org_status", "org_id", "status"),
        Index("ix_hitl_org_agent", "org_id", "agent_id"),
    )


# ── Governance Policies ────────────────────────────────────────────────────────
class PolicyRow(Base):
    """Stored governance policy (serialized as JSON rules)."""
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(50), default="1.0.0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    profile: Mapped[str | None] = mapped_column(String(50))  # soc2|hipaa|gdpr|custom
    rules_json: Mapped[list] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("org_id", "name"),)
