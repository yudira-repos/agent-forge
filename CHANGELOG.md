# Changelog

All notable changes to AgentForge are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Tier 1: PostgreSQL backend (SQLAlchemy + Alembic migrations)
- Tier 1: Authentication (API keys, JWT, OAuth2/SSO)
- Tier 1: Docker + docker-compose deployment
- Tier 1: Slack, email, PagerDuty HITL notification webhooks
- Tier 2: Structured logging (structlog) + Prometheus metrics
- Tier 2: Audit log export (CSV, JSON) with date range filters
- Tier 2: Role-based UI access wired to AgentForge RBAC
- Tier 2: Rate limiting (slowapi) per org/IP
- Tier 2: /health and /ready endpoints
- Tier 3: Kubernetes manifests + Helm chart
- Tier 3: Multi-tenancy (org_id scoping across all resources)
- Tier 3: OpenTelemetry distributed tracing
- Tier 3: Compliance report generator (SOC 2, HIPAA, GDPR)
- Enterprise UI console (FastAPI + React dashboard)

## [0.1.0] - 2024-06-06

### Added
- **AIAM** — Agent Identity & Authority Management
  - `AgentIdentity` with HMAC-SHA256 signed credentials
  - `AgentAuthority` with explicit-deny-wins scope evaluation
  - `RBACPolicy` with role inheritance (viewer → operator → supervisor → admin)
  - `TrustChain` with scope-narrowing delegation tokens
- **Agent Registry** — in-memory and SQLite backends, versioning, capability declarations
- **Governance Framework** — composable `PolicyEngine`, SOC 2 / HIPAA / GDPR compliance profiles
- **Auditability SDK** — tamper-evident chained events, pluggable sinks, `AuditTrail.replay()`
- **HITL Orchestration** — async approval workflows, L1→L2→L3 escalation, timeout handling
- **Enterprise Agent Runtime** — adapters for Anthropic Claude, OpenAI, LangChain, GCP Vertex
- 63 passing tests, zero required dependencies

[Unreleased]: https://github.com/agentforge-oss/agentforge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/agentforge-oss/agentforge/releases/tag/v0.1.0
