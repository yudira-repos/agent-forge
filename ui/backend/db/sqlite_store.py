"""
Persistence store for AgentForge UI — workflow runs and HITL decisions.

Auto-selects backend based on DATABASE_URL:
  DATABASE_URL set  →  PostgreSQL via asyncpg  (Railway production)
  No DATABASE_URL   →  SQLite via sqlite3      (local dev, no extra deps)

Public API is identical for both backends — all functions are async.

Railway setup:
  The DATABASE_URL env var is injected automatically when you add the
  Postgres plugin to your Railway project.  Internal hostname:
    postgres.railway.internal
  No extra config needed — just add the plugin and redeploy.

Local dev:
  SQLite file lands at agentforge_data.db in the current working directory
  (wherever you run `python ui/backend/main.py`).
  Override with AGENTFORGE_DB_PATH env var if needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

# ── Backend selection ─────────────────────────────────────────────────────────
_DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
_PG: bool = bool(_DATABASE_URL)

if _PG:
    # ── PostgreSQL (Railway) ───────────────────────────────────────────────────
    # asyncpg is already installed in the Railway Dockerfile.
    import asyncpg  # type: ignore

    _pg_pool: asyncpg.Pool | None = None  # type: ignore

    async def _pool() -> asyncpg.Pool:  # type: ignore
        global _pg_pool
        if _pg_pool is None:
            # Strip SQLAlchemy driver suffix and normalise scheme.
            # Railway injects postgresql+asyncpg:// (SQLAlchemy-style);
            # asyncpg only accepts postgresql:// or postgres://
            import re as _re
            url = _re.sub(r'\+\w+', '', _DATABASE_URL)   # postgresql+asyncpg → postgresql
            url = url.replace("postgres://", "postgresql://", 1)
            _pg_pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
        return _pg_pool

else:
    # ── SQLite (local dev) ────────────────────────────────────────────────────
    _DB_PATH = Path(os.environ.get("AGENTFORGE_DB_PATH", "agentforge_data.db")).resolve()

    def _sqlite_conn() -> sqlite3.Connection:
        c = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c


# ── Schema init ───────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create tables if they don't exist.  Call once at startup (async)."""
    if _PG:
        p = await _pool()
        async with p.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id      TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    data        TEXT NOT NULL,
                    updated_at  DOUBLE PRECISION NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS hitl_decisions (
                    request_id  TEXT PRIMARY KEY,
                    run_id      TEXT NOT NULL,
                    node_id     TEXT NOT NULL,
                    approved    BOOLEAN,
                    reviewer_id TEXT,
                    reason      TEXT DEFAULT '',
                    created_at  DOUBLE PRECISION NOT NULL,
                    decided_at  DOUBLE PRECISION
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_spans (
                    span_id      TEXT PRIMARY KEY,
                    agent_id     TEXT NOT NULL,
                    agent_name   TEXT NOT NULL,
                    run_id       TEXT NOT NULL,
                    workflow_id  TEXT NOT NULL,
                    backend      TEXT NOT NULL,
                    model        TEXT NOT NULL,
                    started_at   DOUBLE PRECISION NOT NULL,
                    latency_ms   DOUBLE PRECISION NOT NULL,
                    tokens_in    INTEGER NOT NULL DEFAULT 0,
                    tokens_out   INTEGER NOT NULL DEFAULT 0,
                    success      BOOLEAN NOT NULL DEFAULT TRUE,
                    error        TEXT,
                    cost_usd     DOUBLE PRECISION NOT NULL DEFAULT 0
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_spans_run_id ON agent_spans (run_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_spans_agent_id ON agent_spans (agent_id)"
            )
    else:
        def _create() -> None:
            with _sqlite_conn() as c:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS workflow_runs (
                        run_id      TEXT PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        data        TEXT NOT NULL,
                        updated_at  REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS hitl_decisions (
                        request_id  TEXT PRIMARY KEY,
                        run_id      TEXT NOT NULL,
                        node_id     TEXT NOT NULL,
                        approved    INTEGER,
                        reviewer_id TEXT,
                        reason      TEXT DEFAULT '',
                        created_at  REAL NOT NULL,
                        decided_at  REAL
                    );
                    CREATE TABLE IF NOT EXISTS agent_spans (
                        span_id      TEXT PRIMARY KEY,
                        agent_id     TEXT NOT NULL,
                        agent_name   TEXT NOT NULL,
                        run_id       TEXT NOT NULL,
                        workflow_id  TEXT NOT NULL,
                        backend      TEXT NOT NULL,
                        model        TEXT NOT NULL,
                        started_at   REAL NOT NULL,
                        latency_ms   REAL NOT NULL,
                        tokens_in    INTEGER NOT NULL DEFAULT 0,
                        tokens_out   INTEGER NOT NULL DEFAULT 0,
                        success      INTEGER NOT NULL DEFAULT 1,
                        error        TEXT,
                        cost_usd     REAL NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_spans_run_id   ON agent_spans (run_id);
                    CREATE INDEX IF NOT EXISTS idx_spans_agent_id ON agent_spans (agent_id);
                """)
        await asyncio.to_thread(_create)

    # Agent escalations table (agent-level HITL)
    await init_escalations_table()


# ── Workflow runs ─────────────────────────────────────────────────────────────

async def upsert_run(run_id: str, workflow_id: str, data: dict[str, Any]) -> None:
    """Insert or update a run (full JSON blob)."""
    if _PG:
        p = await _pool()
        await p.execute(
            """INSERT INTO workflow_runs (run_id, workflow_id, data, updated_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (run_id) DO UPDATE
                 SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at""",
            run_id, workflow_id, json.dumps(data), time.time(),
        )
    else:
        def _fn() -> None:
            with _sqlite_conn() as c:
                c.execute(
                    """INSERT INTO workflow_runs (run_id, workflow_id, data, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE
                         SET data=excluded.data, updated_at=excluded.updated_at""",
                    (run_id, workflow_id, json.dumps(data), time.time()),
                )
        await asyncio.to_thread(_fn)


async def list_runs(workflow_id: str | None = None) -> list[dict[str, Any]]:
    if _PG:
        p = await _pool()
        if workflow_id:
            rows = await p.fetch(
                "SELECT data FROM workflow_runs WHERE workflow_id=$1 ORDER BY updated_at DESC",
                workflow_id,
            )
        else:
            rows = await p.fetch("SELECT data FROM workflow_runs ORDER BY updated_at DESC")
        return [json.loads(r["data"]) for r in rows]
    else:
        def _fn() -> list[dict[str, Any]]:
            with _sqlite_conn() as c:
                if workflow_id:
                    rows = c.execute(
                        "SELECT data FROM workflow_runs WHERE workflow_id=? ORDER BY updated_at DESC",
                        (workflow_id,),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT data FROM workflow_runs ORDER BY updated_at DESC"
                    ).fetchall()
            return [json.loads(r["data"]) for r in rows]
        return await asyncio.to_thread(_fn)


async def get_run(run_id: str) -> dict[str, Any] | None:
    if _PG:
        p = await _pool()
        row = await p.fetchrow("SELECT data FROM workflow_runs WHERE run_id=$1", run_id)
        return json.loads(row["data"]) if row else None
    else:
        def _fn() -> dict[str, Any] | None:
            with _sqlite_conn() as c:
                row = c.execute(
                    "SELECT data FROM workflow_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            return json.loads(row["data"]) if row else None
        return await asyncio.to_thread(_fn)


# ── HITL decisions ────────────────────────────────────────────────────────────

async def create_hitl_pending(request_id: str, run_id: str, node_id: str) -> None:
    """Record a new pending HITL request (no decision yet)."""
    if _PG:
        p = await _pool()
        await p.execute(
            """INSERT INTO hitl_decisions (request_id, run_id, node_id, created_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (request_id) DO NOTHING""",
            request_id, run_id, node_id, time.time(),
        )
    else:
        def _fn() -> None:
            with _sqlite_conn() as c:
                c.execute(
                    """INSERT OR IGNORE INTO hitl_decisions (request_id, run_id, node_id, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (request_id, run_id, node_id, time.time()),
                )
        await asyncio.to_thread(_fn)


async def resolve_hitl(
    request_id: str, approved: bool, reviewer_id: str, reason: str
) -> None:
    """Record a human (or system) decision."""
    if _PG:
        p = await _pool()
        await p.execute(
            """UPDATE hitl_decisions
               SET approved=$1, reviewer_id=$2, reason=$3, decided_at=$4
               WHERE request_id=$5""",
            approved, reviewer_id, reason, time.time(), request_id,
        )
    else:
        def _fn() -> None:
            with _sqlite_conn() as c:
                c.execute(
                    """UPDATE hitl_decisions
                       SET approved=?, reviewer_id=?, reason=?, decided_at=?
                       WHERE request_id=?""",
                    (1 if approved else 0, reviewer_id, reason, time.time(), request_id),
                )
        await asyncio.to_thread(_fn)


async def get_hitl_decision(request_id: str) -> dict[str, Any] | None:
    """Return the decision if made; None if still pending."""
    if _PG:
        p = await _pool()
        row = await p.fetchrow(
            """SELECT approved, reviewer_id, reason
               FROM hitl_decisions
               WHERE request_id=$1 AND decided_at IS NOT NULL""",
            request_id,
        )
        if not row:
            return None
        return {
            "approved": bool(row["approved"]),
            "reviewer_id": row["reviewer_id"] or "unknown",
            "reason": row["reason"] or "",
        }
    else:
        def _fn() -> dict[str, Any] | None:
            with _sqlite_conn() as c:
                row = c.execute(
                    """SELECT approved, reviewer_id, reason
                       FROM hitl_decisions
                       WHERE request_id=? AND decided_at IS NOT NULL""",
                    (request_id,),
                ).fetchone()
            if not row:
                return None
            return {
                "approved": bool(row["approved"]),
                "reviewer_id": row["reviewer_id"] or "unknown",
                "reason": row["reason"] or "",
            }
        return await asyncio.to_thread(_fn)


async def wait_for_decision(
    request_id: str,
    timeout_seconds: float = 3600.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """
    Poll the DB every second until a HITL decision is recorded.

    Unlike asyncio.Future this survives server restarts — on restart the
    background task is gone, but any new approve/reject call still writes to
    the DB and will be visible to a resumed polling loop.

    Returns {approved, reviewer_id, reason}.
    On timeout returns an auto-reject from 'system'.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        decision = await get_hitl_decision(request_id)
        if decision:
            return decision
        await asyncio.sleep(poll_interval)
    return {
        "approved": False,
        "reviewer_id": "system",
        "reason": f"Auto-rejected: no decision within {int(timeout_seconds)}s",
    }


# ── Agent spans ───────────────────────────────────────────────────────────────

async def write_span(span: dict[str, Any]) -> None:
    """
    Persist one OTEL span record to the database.

    ``span`` must contain the fields emitted by SpanRecord.to_dict().
    Fire-and-forget: errors are logged but never re-raised so the runner
    is never blocked by a DB write failure.
    """
    try:
        if _PG:
            p = await _pool()
            await p.execute(
                """INSERT INTO agent_spans
                   (span_id, agent_id, agent_name, run_id, workflow_id,
                    backend, model, started_at, latency_ms,
                    tokens_in, tokens_out, success, error, cost_usd)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                   ON CONFLICT (span_id) DO NOTHING""",
                span["span_id"], span["agent_id"], span["agent_name"],
                span["run_id"], span["workflow_id"],
                span["backend"], span["model"], span["started_at"],
                span["latency_ms"], span["tokens_in"], span["tokens_out"],
                span["success"], span.get("error"), span["cost_usd"],
            )
        else:
            def _fn() -> None:
                with _sqlite_conn() as c:
                    c.execute(
                        """INSERT OR IGNORE INTO agent_spans
                           (span_id, agent_id, agent_name, run_id, workflow_id,
                            backend, model, started_at, latency_ms,
                            tokens_in, tokens_out, success, error, cost_usd)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            span["span_id"], span["agent_id"], span["agent_name"],
                            span["run_id"], span["workflow_id"],
                            span["backend"], span["model"], span["started_at"],
                            span["latency_ms"], span["tokens_in"], span["tokens_out"],
                            1 if span["success"] else 0,
                            span.get("error"), span["cost_usd"],
                        ),
                    )
            await asyncio.to_thread(_fn)
    except Exception as exc:
        # Never let a DB write block the agent runner
        import logging
        logging.getLogger(__name__).warning("write_span failed (non-fatal): %s", exc)


async def get_spans_for_run(run_id: str) -> list[dict[str, Any]]:
    """Return all persisted spans for a run, oldest first."""
    if _PG:
        p = await _pool()
        rows = await p.fetch(
            "SELECT * FROM agent_spans WHERE run_id=$1 ORDER BY started_at ASC",
            run_id,
        )
        return [dict(r) for r in rows]
    else:
        def _fn() -> list[dict[str, Any]]:
            with _sqlite_conn() as c:
                rows = c.execute(
                    "SELECT * FROM agent_spans WHERE run_id=? ORDER BY started_at ASC",
                    (run_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_fn)


async def get_agent_metrics_from_db() -> list[dict[str, Any]]:
    """
    Aggregate per-agent metrics from the persisted span table.

    Returns one row per agent_id with: invocations, successes, failures,
    avg_latency_ms, total_tokens_in, total_tokens_out, total_cost_usd,
    error_rate.
    """
    if _PG:
        p = await _pool()
        rows = await p.fetch("""
            SELECT
                agent_id, agent_name, backend, model,
                COUNT(*)                          AS invocations,
                SUM(CASE WHEN success THEN 1 ELSE 0 END) AS successes,
                SUM(CASE WHEN success THEN 0 ELSE 1 END) AS failures,
                ROUND(AVG(latency_ms)::numeric, 1)        AS avg_latency_ms,
                SUM(tokens_in)                    AS total_tokens_in,
                SUM(tokens_out)                   AS total_tokens_out,
                SUM(cost_usd)                     AS total_cost_usd
            FROM agent_spans
            GROUP BY agent_id, agent_name, backend, model
            ORDER BY invocations DESC
        """)
        result = []
        for r in rows:
            d = dict(r)
            inv = d["invocations"] or 1
            d["error_rate"] = round((d["failures"] or 0) / inv, 4)
            d["total_cost_usd"] = round(float(d["total_cost_usd"] or 0), 6)
            result.append(d)
        return result
    else:
        def _fn() -> list[dict[str, Any]]:
            with _sqlite_conn() as c:
                rows = c.execute("""
                    SELECT
                        agent_id, agent_name, backend, model,
                        COUNT(*)                                  AS invocations,
                        SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS successes,
                        SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failures,
                        ROUND(AVG(latency_ms), 1)                  AS avg_latency_ms,
                        SUM(tokens_in)                             AS total_tokens_in,
                        SUM(tokens_out)                            AS total_tokens_out,
                        SUM(cost_usd)                              AS total_cost_usd
                    FROM agent_spans
                    GROUP BY agent_id, agent_name, backend, model
                    ORDER BY invocations DESC
                """).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                inv = d["invocations"] or 1
                d["error_rate"] = round((d["failures"] or 0) / inv, 4)
                d["total_cost_usd"] = round(float(d["total_cost_usd"] or 0), 6)
                result.append(d)
            return result
        return await asyncio.to_thread(_fn)


# ── Agent escalations ─────────────────────────────────────────────────────────

async def init_escalations_table() -> None:
    """
    Create the agent_escalations table.  Called from init_db().

    Stores everything needed to reconstruct the agent's session when the
    human resolves the escalation:
      - conversation_history  — messages sent to the LLM (JSON)
      - context_snapshot      — full workflow context at escalation time (JSON)
      - partial_output        — what the LLM returned before deciding to escalate (JSON)
      - candidates            — categories/options the agent was unsure between (JSON array)
    """
    if _PG:
        p = await _pool()
        async with p.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_escalations (
                    escalation_id        TEXT PRIMARY KEY,
                    agent_id             TEXT NOT NULL,
                    agent_name           TEXT NOT NULL,
                    run_id               TEXT NOT NULL,
                    workflow_id          TEXT NOT NULL,
                    node_id              TEXT NOT NULL,
                    reason               TEXT NOT NULL DEFAULT '',
                    confidence           DOUBLE PRECISION DEFAULT 0.0,
                    candidates           TEXT DEFAULT '[]',
                    context_snapshot     TEXT DEFAULT '{}',
                    conversation_history TEXT DEFAULT '[]',
                    partial_output       TEXT DEFAULT '{}',
                    status               TEXT NOT NULL DEFAULT 'pending',
                    created_at           DOUBLE PRECISION NOT NULL,
                    resolved_at          DOUBLE PRECISION,
                    resolved_by          TEXT,
                    resolution           TEXT
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_esc_run_id ON agent_escalations (run_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_esc_status ON agent_escalations (status)"
            )
    else:
        def _create() -> None:
            with _sqlite_conn() as c:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS agent_escalations (
                        escalation_id        TEXT PRIMARY KEY,
                        agent_id             TEXT NOT NULL,
                        agent_name           TEXT NOT NULL,
                        run_id               TEXT NOT NULL,
                        workflow_id          TEXT NOT NULL,
                        node_id              TEXT NOT NULL,
                        reason               TEXT NOT NULL DEFAULT '',
                        confidence           REAL DEFAULT 0.0,
                        candidates           TEXT DEFAULT '[]',
                        context_snapshot     TEXT DEFAULT '{}',
                        conversation_history TEXT DEFAULT '[]',
                        partial_output       TEXT DEFAULT '{}',
                        status               TEXT NOT NULL DEFAULT 'pending',
                        created_at           REAL NOT NULL,
                        resolved_at          REAL,
                        resolved_by          TEXT,
                        resolution           TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_esc_run_id ON agent_escalations (run_id);
                    CREATE INDEX IF NOT EXISTS idx_esc_status  ON agent_escalations (status);
                """)
        await asyncio.to_thread(_create)


async def write_escalation(esc: dict[str, Any]) -> None:
    """Persist a new agent escalation record."""
    if _PG:
        p = await _pool()
        await p.execute("""
            INSERT INTO agent_escalations
              (escalation_id, agent_id, agent_name, run_id, workflow_id, node_id,
               reason, confidence, candidates, context_snapshot,
               conversation_history, partial_output, status, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'pending',$13)
            ON CONFLICT (escalation_id) DO NOTHING
        """,
            esc["escalation_id"], esc["agent_id"], esc["agent_name"],
            esc["run_id"], esc["workflow_id"], esc["node_id"],
            esc.get("reason", ""), float(esc.get("confidence", 0)),
            esc.get("candidates", "[]"), esc.get("context_snapshot", "{}"),
            esc.get("conversation_history", "[]"), esc.get("partial_output", "{}"),
            esc["created_at"],
        )
    else:
        def _fn() -> None:
            with _sqlite_conn() as c:
                c.execute("""
                    INSERT OR IGNORE INTO agent_escalations
                      (escalation_id, agent_id, agent_name, run_id, workflow_id, node_id,
                       reason, confidence, candidates, context_snapshot,
                       conversation_history, partial_output, status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)
                """, (
                    esc["escalation_id"], esc["agent_id"], esc["agent_name"],
                    esc["run_id"], esc["workflow_id"], esc["node_id"],
                    esc.get("reason", ""), float(esc.get("confidence", 0)),
                    esc.get("candidates", "[]"), esc.get("context_snapshot", "{}"),
                    esc.get("conversation_history", "[]"), esc.get("partial_output", "{}"),
                    esc["created_at"],
                ))
        await asyncio.to_thread(_fn)


async def resolve_escalation(
    escalation_id: str, resolution: dict[str, Any], resolved_by: str = "human"
) -> None:
    """Mark an escalation as resolved with the human's decision."""
    now = time.time()
    resolution_json = json.dumps(resolution)
    if _PG:
        p = await _pool()
        await p.execute("""
            UPDATE agent_escalations
            SET status='resolved', resolved_at=$1, resolved_by=$2, resolution=$3
            WHERE escalation_id=$4
        """, now, resolved_by, resolution_json, escalation_id)
    else:
        def _fn() -> None:
            with _sqlite_conn() as c:
                c.execute("""
                    UPDATE agent_escalations
                    SET status='resolved', resolved_at=?, resolved_by=?, resolution=?
                    WHERE escalation_id=?
                """, (now, resolved_by, resolution_json, escalation_id))
        await asyncio.to_thread(_fn)


async def get_escalation(escalation_id: str) -> dict[str, Any] | None:
    """Load a single escalation by ID."""
    if _PG:
        p = await _pool()
        row = await p.fetchrow(
            "SELECT * FROM agent_escalations WHERE escalation_id=$1", escalation_id
        )
        if not row:
            return None
        d = dict(row)
        for field in ("candidates", "context_snapshot", "conversation_history", "partial_output", "resolution"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        return d
    else:
        def _fn() -> dict[str, Any] | None:
            with _sqlite_conn() as c:
                row = c.execute(
                    "SELECT * FROM agent_escalations WHERE escalation_id=?", (escalation_id,)
                ).fetchone()
            if not row:
                return None
            d = dict(row)
            for field in ("candidates", "context_snapshot", "conversation_history", "partial_output", "resolution"):
                if isinstance(d.get(field), str):
                    try:
                        d[field] = json.loads(d[field])
                    except Exception:
                        pass
            return d
        return await asyncio.to_thread(_fn)


async def list_pending_escalations() -> list[dict[str, Any]]:
    """Return all unresolved agent escalations, newest first."""
    if _PG:
        p = await _pool()
        rows = await p.fetch(
            "SELECT * FROM agent_escalations WHERE status='pending' ORDER BY created_at DESC"
        )
        result = []
        for row in rows:
            d = dict(row)
            for f in ("candidates", "context_snapshot", "partial_output"):
                if isinstance(d.get(f), str):
                    try: d[f] = json.loads(d[f])
                    except Exception: pass
            result.append(d)
        return result
    else:
        def _fn() -> list[dict[str, Any]]:
            with _sqlite_conn() as c:
                rows = c.execute(
                    "SELECT * FROM agent_escalations WHERE status='pending' ORDER BY created_at DESC"
                ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                for f in ("candidates", "context_snapshot", "partial_output"):
                    if isinstance(d.get(f), str):
                        try: d[f] = json.loads(d[f])
                        except Exception: pass
                result.append(d)
            return result
        return await asyncio.to_thread(_fn)


async def get_recent_spans_from_db(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent N spans across all runs."""
    if _PG:
        p = await _pool()
        rows = await p.fetch(
            "SELECT * FROM agent_spans ORDER BY started_at DESC LIMIT $1", limit
        )
        return [dict(r) for r in rows]
    else:
        def _fn() -> list[dict[str, Any]]:
            with _sqlite_conn() as c:
                rows = c.execute(
                    "SELECT * FROM agent_spans ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_fn)
