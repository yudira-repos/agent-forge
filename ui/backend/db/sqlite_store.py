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
                """)
        await asyncio.to_thread(_create)


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
