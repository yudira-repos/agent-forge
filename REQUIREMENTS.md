# AgentForge — Product Requirements: Enterprise Scale

**Version:** 1.0 · **Date:** 2026-06-10 · **Owner:** Ravi
**Scope:** Take AgentForge from demo-grade to production-grade in two phases — Phase 1 for small apps (LifeOS-class B2C), Phase 2 for large enterprises (BlackLine, TriNet, OpenAI-class B2B).

---

## 0. Non-Negotiable Product Principles

These apply to every requirement below and override any conflicting implementation shortcut.

| # | Principle | What it means concretely |
|---|---|---|
| P1 | **No seed data, ever** | The system boots empty. Every agent, workflow, run, audit event, HITL request, and user exists because a real interaction created it. Workflow *templates* may ship as definitions, but they produce zero runs/events until executed. |
| P2 | **Real execution only** | Every workflow node executes on the orchestration engine against real adapters (Claude, OpenAI, Vertex, HTTP APIs). No mocked outputs, no pre-canned run histories, no fake metrics. |
| P3 | **Nothing lives only in memory** | All state of record (agents, workflows, runs, HITL queue, audit, users, sessions, escalations, metrics) is in a database. Hot/shared state (live runs, WS fan-out, rate limits) lives in a distributed cache. In-memory is permitted only as a read-through cache with a persistent source of truth. |
| P4 | **Survive restart and scale horizontally** | Kill any single process at any moment: no data loss, in-flight runs resume, pending approvals remain. Run N replicas behind a load balancer with no sticky-session requirement. |
| P5 | **Stability is a feature** | UI and backend have defined error states, timeouts, retries, and graceful degradation. No unhandled-exception 500s, no blank screens, no stale-then-blank dashboards. |

---

## 1. Current-State Assessment (gap analysis)

Grounded in the code as of today (`ui/backend/main.py`, `routes/workflows.py`, `agentforge/*`):

| Area | Today | Gap vs. principles |
|---|---|---|
| Agent registry | In-memory default; SQLite backend exists but the console instantiates `AgentRegistry()` (in-memory) | Violates P3/P4 |
| Audit | Console uses `InMemoryAuditSink`; chain is tamper-evident in design but lost on restart | Violates P3; kills the core product promise |
| HITL queue | `dict[str, ApprovalRequest]` inside `HITLOrchestrator` | Pending approvals vanish on restart — violates P3/P4 |
| Workflows | In-memory `_store` dict; 5 demo workflows seeded at import time | Violates P1/P3 |
| Workflow runs | In-memory `_workflow_runs` dict (sqlite_store exists but is partial) | Violates P3/P4 |
| Users/auth | Seeded demo users, hardcoded `agentforge-demo-secret`, 16-hex-char truncated HMAC token | Not shippable to any paying customer |
| Seed endpoints | `/api/dev/seed-events`, `/api/dev/seed-hitl`, `seed_demo_data()` at startup | Violates P1 |
| Persistence | SQLite default; Postgres only via Railway `DATABASE_URL` sniffing; no migrations framework | No schema versioning; two divergent code paths |
| Realtime | WS clients and SSE generators in process memory | Breaks at >1 replica — violates P4 |
| CORS/security | `allow_origins=["*"]`, no rate limiting, no tenant isolation | Violates baseline security |
| Frontend | 4 static HTML files, inline JS | No build pipeline, no error boundaries, no component reuse |
| Tests | 5 unit-test files on the core library; none on the API/engine/UI | No regression safety for the surface customers touch |

---

## 2. Phase 1 — Small Apps (LifeOS-class: B2C and small B2B)

**Target customer:** a 1–10 person team embedding AgentForge under a personal/SMB agent product. Single tenant per deployment. One Postgres, one Redis, 1–3 app replicas.
**Exit criteria:** a LifeOS developer can deploy AgentForge with Docker Compose, connect real agents, run real workflows from templates, approve real HITL requests, and lose zero data across deploys/restarts — with an empty system at first boot.

### 2.1 Eliminate seed data; real onboarding instead (P1)

- **FR-1.1** Remove `seed_demo_data()`, `_seed_users()`, `_seed*()` workflow seeders, and `/api/dev/seed-*` endpoints from all production code paths. *Acceptance:* fresh boot → `/api/agents`, `/api/workflows/runs`, `/api/audit`, `/api/hitl/pending` all return empty; zero rows in DB.*
- **FR-1.2** First-run onboarding wizard in the console: create admin account → connect at least one LLM provider key (validated by a live test call) → register first agent → optionally instantiate a workflow template. Empty states on every screen guide the next real action ("No agents yet — register one").
- **FR-1.3** Workflow **templates as definitions only**: a curated template gallery (support-ticket triage, invoice approval with HITL gate, multi-agent research, escalation flow) stored as versioned JSON definitions. Instantiating a template clones it into the tenant's workflow store; it has no run history until the orchestration engine actually executes it with real adapter calls (P2).
- **FR-1.4** A separately-packaged `examples/` simulator (CLI) may drive *real* traffic through the public API for demos — it is a client, never a server-side seeder.

### 2.2 Persistence everywhere (P3)

- **FR-2.1** **PostgreSQL is the system of record** for: agents/manifests/capabilities, workflow definitions (versioned), workflow runs + per-node step results, HITL requests/decisions, escalations + paused-agent session context, audit events, users/roles/sessions, provider credentials (encrypted), OTEL span summaries/metrics rollups. SQLite remains only as a dev-mode embedded option behind the same repository interface.
- **FR-2.2** Single data-access layer (SQLAlchemy 2.x async + asyncpg) replacing the dual `sqlite_store.py` code path. **Alembic migrations** required for every schema change; CI fails on un-migrated model drift.
- **FR-2.3** **Redis (distributed cache + coordination)** for: live run state and node progress, WS/SSE pub-sub fan-out, HITL wakeup signaling (replace in-process `asyncio` waits with Redis pub-sub + DB polling fallback), rate limiting, session cache. Every Redis entry must be reconstructable from Postgres (cache, not truth).
- **FR-2.4** Audit chain persisted append-only in Postgres with per-event hash chaining (`prev_event_hash`), verified by a `GET /api/audit/verify` endpoint that detects gaps/tampering. *Acceptance: kill -9 the server mid-workflow; on restart the chain verifies and the run's events are intact.*

### 2.3 Orchestration engine: durable, real execution (P2, P4)

- **FR-3.1** Run lifecycle persisted as a state machine: `queued → running → waiting_hitl → waiting_event → completed | failed | cancelled`, with every node transition written to Postgres before being acted on (write-ahead semantics).
- **FR-3.2** **Crash recovery:** on startup, a recovery sweep finds runs in `running` with expired worker heartbeats and resumes them from the last completed node (node executions must be idempotent or guarded by idempotency keys).
- **FR-3.3** HITL gates and agent escalations are **durable suspensions**: full node/session context serialized to Postgres; approval via API/UI resumes the run on any replica. No `asyncio.Event`-in-RAM dependency.
- **FR-3.4** Per-node execution contract: timeout, max retries with exponential backoff + jitter, on-failure policy (fail-run | continue | fallback-edge), and dead-letter record for permanently failed nodes.
- **FR-3.5** All agent nodes execute through the real runtime adapters with per-call OTEL spans persisted (tokens, latency, cost). Provider keys are tenant-configured, encrypted at rest (Fernet/AES-GCM via app-level KMS key), never logged.
- **FR-3.6** Triggers beyond manual: cron schedules and inbound webhook triggers (HMAC-signed), both persisted and survivable across restarts.

### 2.4 Real authentication and authorization

- **FR-4.1** Replace the demo token scheme: proper JWT (RS256 or HS256 with ≥256-bit secret from env/secret store), short-lived access + refresh tokens, revocation list in Redis. Passwords via argon2/bcrypt. Remove all hardcoded secrets.
- **FR-4.2** Users, roles (admin / supervisor / operator / viewer), and API keys in Postgres; API keys scoped (e.g., `workflows:execute`, `audit:read`) for SDK/machine callers, hashed at rest, last-used tracking, rotation.
- **FR-4.3** CORS locked to configured origins; security headers (HSTS, CSP, X-Frame-Options); per-key and per-IP rate limiting (Redis token bucket); request body size limits.

### 2.5 Backend stability (P5)

- **FR-5.1** Global exception handling → structured RFC-7807 error responses; no stack traces to clients; correlation ID on every request, propagated into audit events and logs (structured JSON logging).
- **FR-5.2** Health endpoints with real dependency checks: `/health/live`, `/health/ready` (DB ping, Redis ping, migration version match).
- **FR-5.3** Graceful shutdown: drain HTTP, checkpoint in-flight node executions, close WS with reconnect hint.
- **FR-5.4** Backpressure: bounded execution concurrency per replica (worker pool), queue depth metric, 429 + Retry-After when saturated.
- **FR-5.5** API test suite: integration tests for every route (happy path + auth failure + validation failure), engine tests for crash/resume and HITL suspension, target ≥80% coverage on `ui/backend` and the engine. CI gates merge.

### 2.6 Frontend stability (P5)

- **FR-6.1** Migrate static HTMLs to a built SPA (React + TypeScript + Vite), shared API client with auth/refresh handling, typed against the OpenAPI schema.
- **FR-6.2** Every view has loading / empty / error states; WS and SSE clients auto-reconnect with exponential backoff and resync state from REST on reconnect (no permanently stale dashboards).
- **FR-6.3** Optimistic UI only where safe (HITL approve/reject confirms server-side before removing from queue); destructive actions require confirmation; all times shown in user TZ with UTC stored.
- **FR-6.4** E2E smoke suite (Playwright): login → register agent → instantiate template → execute → approve HITL → see audit trail. Runs in CI against a real Postgres+Redis stack.

### 2.7 Deployment (Phase 1)

- **FR-7.1** `docker-compose.yml` for app + Postgres + Redis with one-command bootstrap (runs migrations, creates first admin via env or wizard). Helm chart updated to match.
- **FR-7.2** 12-factor config: all settings via env vars with validated startup config (`pydantic-settings`); fail fast on missing/invalid config.

### Phase 1 NFRs

| Metric | Target |
|---|---|
| Concurrent workflow runs | 100 per deployment |
| Registered agents | 1,000 |
| Audit write throughput | 500 events/sec sustained |
| API p95 latency (non-LLM) | < 300 ms |
| Recovery time after process kill | < 30 s, zero data loss |
| Availability | 99.5% (single-region) |

---

## 3. Phase 2 — Enterprise (BlackLine / TriNet / OpenAI-class B2B)

**Target customer:** regulated enterprises and platform companies embedding governance for thousands of agents across many teams. Multi-tenant SaaS **and** self-hosted (VPC / on-prem) from the same codebase.
**Exit criteria:** an enterprise can onboard via SSO, isolate business units as tenants, run thousands of concurrent governed workflows across regions, pass a SOC 2 Type II audit using AgentForge's own evidence exports, and get contractual SLAs.

### 3.1 Multi-tenancy and identity

- **FR-8.1** First-class `tenant_id` on every table and API path; Postgres row-level security as defense-in-depth; per-tenant encryption keys (envelope encryption, BYO-KMS: AWS KMS / GCP KMS / Azure Key Vault). Option of dedicated DB/schema per tenant for premium isolation tiers.
- **FR-8.2** **SSO:** OIDC + SAML 2.0 (Okta, Entra ID, Google Workspace); **SCIM 2.0** user/group provisioning and deprovisioning; enforced MFA passthrough.
- **FR-8.3** Hierarchical orgs: organization → business units/projects → environments (dev/staging/prod) with policy and RBAC inheritance; custom roles with fine-grained permissions (beyond the 4 fixed roles); approval-group routing for HITL (route `finance.wire_transfer` to "Treasury Approvers" group, quorum N-of-M).
- **FR-8.4** Agent identity upgraded from HMAC to asymmetric: per-tenant CA issuing short-lived agent certificates (SPIFFE-compatible IDs), agent-to-agent trust chain verification at runtime (roadmap v0.8 item).

### 3.2 Orchestration at scale

- **FR-9.1** Split the console from execution: stateless **API tier** + horizontally-scaled **worker tier** consuming from a durable queue. Adopt **Temporal** (preferred) or Postgres-backed durable queue + own scheduler as the workflow execution substrate — gaining durable timers, retries, saga compensation, and exactly-once-effect semantics instead of reimplementing them.
- **FR-9.2** Scale targets: 10,000+ concurrent runs per tenant cluster; 1M+ workflow executions/day; long-running workflows (30+ days suspended on HITL) with zero resource leakage.
- **FR-9.3** Priority classes and per-tenant quotas/fair-share scheduling so one tenant's burst can't starve others; per-tenant and per-workflow concurrency caps; cost budgets with hard/soft limits on LLM spend (deny or HITL-gate when budget exceeded — governance rule type).
- **FR-9.4** Event-driven integration: emit run/HITL/audit lifecycle events to Kafka/PubSub/EventBridge (outbox pattern from Postgres); inbound event triggers from customer buses. Webhooks with signed payloads, retries, and dead-lettering.
- **FR-9.5** Sub-workflows, fan-out/fan-in (map over collection with bounded parallelism), and compensation (rollback) edges as first-class node types — "reversible" per the product vision.

### 3.3 Audit and compliance at enterprise scale

- **FR-10.1** Audit store partitioned by tenant + month; tiered storage (hot Postgres → warm object storage in Parquet) with seamless query across tiers; per-tenant retention policies (1–7 years) and legal hold.
- **FR-10.2** Cryptographic anchoring: periodic Merkle-root checkpoints of the audit chain signed and (optionally) externally anchored, so even a DB admin cannot silently rewrite history.
- **FR-10.3** Streaming export to SIEM (Splunk HEC, Datadog, Sentinel) and warehouse (Snowflake/BigQuery); auditor workspace: read-only role, evidence packs (SOC 2 control mappings auto-generated from policy decisions + HITL records).
- **FR-10.4** Compliance program for AgentForge itself: SOC 2 Type II, ISO 27001; HIPAA BAA support; GDPR DPA with data residency (EU/US region pinning per tenant) and right-to-erasure workflows that preserve chain integrity via crypto-shredding of payload keys.
- **FR-10.5** Governance engine: OPA/Rego backend option (roadmap v0.7), policy versioning with effective-dating, dry-run/shadow mode for new policies, and policy-change approvals (governance of the governors).

### 3.4 Reliability and operations

- **FR-11.1** 99.9% SLA (99.95% premium); multi-AZ by default; documented RPO ≤ 5 min / RTO ≤ 1 h with tested cross-region DR runbooks.
- **FR-11.2** Zero-downtime deploys (rolling + expand/contract migrations); API versioning (`/api/v1`) with 12-month deprecation policy; published SDK compatibility matrix.
- **FR-11.3** Full observability of AgentForge itself: OTEL traces/metrics/logs exported to customer's APM; per-tenant usage metering (runs, LLM tokens, audit events, seats) feeding billing.
- **FR-11.4** Chaos and load testing in CI/CD pipeline: kill-worker, kill-DB-failover, Redis-loss (system degrades to DB polling, never loses truth), 10k-concurrent-run soak test before each release.
- **FR-11.5** Status page, incident process, and customer-visible audit of platform access (who at the vendor touched tenant data).

### 3.5 Enterprise security

- **FR-12.1** Pen test per release cycle + public bug bounty; SSDLC with dependency scanning and SBOM per release.
- **FR-12.2** Secrets: integration with customer vaults (HashiCorp Vault, AWS Secrets Manager) for provider keys; no plaintext secrets in DB, logs, or traces (automatic redaction middleware).
- **FR-12.3** PII handling: field-level encryption for HITL context payloads and audit payload bodies; configurable payload redaction policies per tenant.
- **FR-12.4** IP allowlisting, private connectivity (PrivateLink/PSC) for SaaS; air-gapped install option for on-prem.

### 3.6 Platform and ecosystem (what makes OpenAI-class buyers care)

- **FR-13.1** Stable public SDKs (Python first, then TypeScript/Go) hitting `/api/v1`; the embedded-SDK mode and remote mode behave identically (same governance results for same inputs).
- **FR-13.2** Hosted multi-tenant **agent registry** with cross-org manifest publishing/discovery (the "App Store" moat) — signed manifests, version pinning, vulnerability/deprecation notices.
- **FR-13.3** Terraform provider + admin API for everything in the UI (no console-only operations).
- **FR-13.4** Marketplace for compliance profiles and workflow templates with publisher signing and review.

### Phase 2 NFRs

| Metric | Target |
|---|---|
| Tenants per cluster | 1,000+ |
| Concurrent runs (cluster) | 50,000 |
| Audit ingest | 50,000 events/sec (cluster) |
| API p99 (non-LLM) | < 500 ms |
| Availability | 99.9% (99.95% premium) |
| RPO / RTO | ≤ 5 min / ≤ 1 h |
| HITL approval propagation (decision → run resumed) | < 2 s p95 |

---

## 4. Target Architecture (end-state)

```
                    ┌────────────────────────────┐
   SSO/OIDC ───────▶│  API Tier (stateless, N×)  │◀─── SDKs / Terraform / UI (React SPA)
                    └─────┬──────────────┬───────┘
                          │              │ pub-sub (Redis)
              Postgres ◀──┤              ├──▶ WS/SSE fan-out (any replica)
        (system of record,│              │
         RLS multi-tenant,│       ┌──────▼────────────┐
         Alembic-managed) │       │ Durable Queue /   │
                          │       │ Temporal Server   │
                          │       └──────┬────────────┘
                          │              │
                    ┌─────▼──────────────▼───────┐      ┌──────────────────┐
                    │  Worker Tier (N×)          │─────▶│ LLM Adapters     │
                    │  node exec · retries ·     │      │ Claude/GPT/Vertex│
                    │  HITL suspend/resume       │      └──────────────────┘
                    └─────┬──────────────────────┘
                          │ outbox
                    ┌─────▼───────────────────────────────────────┐
                    │ Kafka / EventBridge → SIEM · Warehouse ·    │
                    │ Webhooks · Customer event buses             │
                    └─────────────────────────────────────────────┘
   Audit: append-only, hash-chained, Merkle-anchored → hot (PG) + warm (object storage)
```

---

## 5. Sequenced Roadmap

| Release | Theme | Headline requirements |
|---|---|---|
| **v0.6** | Truth in the database | FR-1.1, FR-2.1–2.4, FR-4.1–4.3 (de-seed, Postgres+Redis+Alembic, real auth) |
| **v0.7** | Durable orchestration | FR-3.1–3.6, FR-5.1–5.5 (crash recovery, durable HITL, triggers, backend hardening) |
| **v0.8** | Product surface | FR-1.2–1.3, FR-6.1–6.4, FR-7.1–7.2 (onboarding, template gallery, SPA, deploy) → **Phase 1 GA** |
| **v0.9** | Multi-tenant + SSO | FR-8.1–8.3, FR-9.1 (tenancy, OIDC/SAML/SCIM, Temporal adoption) |
| **v1.0** | Enterprise GA | FR-9.2–9.5, FR-10.x, FR-11.1–11.3, FR-12.x (scale, compliance, SLAs) |
| **v1.x** | Ecosystem | FR-13.x, FR-8.4 (hosted registry, marketplace, agent trust chains) |

**Definition of Done (every release):** boots empty (P1) · all state recoverable from Postgres (P3) · kill-any-process test passes (P4) · CI green incl. integration + E2E (P5) · no mocked execution paths in production code (P2).
