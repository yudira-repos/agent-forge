"""Agent Registry — in-memory and SQLite-backed implementations."""

from __future__ import annotations

import json
import sqlite3
import time
from abc import ABC, abstractmethod
from typing import Any

from .models import AgentManifest, AgentStatus


class RegistryBackend(ABC):
    """Abstract persistence backend for the registry."""

    @abstractmethod
    def save(self, manifest: AgentManifest) -> None: ...

    @abstractmethod
    def load(self, agent_id: str) -> AgentManifest | None: ...

    @abstractmethod
    def delete(self, agent_id: str) -> bool: ...

    @abstractmethod
    def list_all(self) -> list[AgentManifest]: ...


class InMemoryBackend(RegistryBackend):
    """Default in-process backend — suitable for testing and single-process deployments."""

    def __init__(self) -> None:
        self._store: dict[str, AgentManifest] = {}

    def save(self, manifest: AgentManifest) -> None:
        self._store[manifest.agent_id] = manifest

    def load(self, agent_id: str) -> AgentManifest | None:
        return self._store.get(agent_id)

    def delete(self, agent_id: str) -> bool:
        return self._store.pop(agent_id, None) is not None

    def list_all(self) -> list[AgentManifest]:
        return list(self._store.values())


class SQLiteBackend(RegistryBackend):
    """
    Lightweight persistent backend using SQLite.

    Suitable for single-node deployments where durability matters but
    a full database server is overkill.

    Example::

        backend = SQLiteBackend("./registry.db")
        registry = AgentRegistry(backend=backend)
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                manifest_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def save(self, manifest: AgentManifest) -> None:
        manifest.updated_at = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO agents VALUES (?, ?, ?)",
            (manifest.agent_id, json.dumps(manifest.to_dict()), manifest.updated_at),
        )
        self._conn.commit()

    def load(self, agent_id: str) -> AgentManifest | None:
        row = self._conn.execute(
            "SELECT manifest_json FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            return None
        return self._from_json(json.loads(row[0]))

    def delete(self, agent_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_all(self) -> list[AgentManifest]:
        rows = self._conn.execute("SELECT manifest_json FROM agents").fetchall()
        return [self._from_json(json.loads(r[0])) for r in rows]

    @staticmethod
    def _from_json(data: dict[str, Any]) -> AgentManifest:
        from .models import AgentCapability

        caps = [
            AgentCapability(
                name=c["name"],
                description=c["description"],
                tags=c.get("tags", []),
                requires_hitl=c.get("requires_hitl", False),
                idempotent=c.get("idempotent", True),
            )
            for c in data.get("capabilities", [])
        ]
        return AgentManifest(
            agent_id=data["agent_id"],
            name=data["name"],
            version=data["version"],
            description=data["description"],
            owner=data["owner"],
            capabilities=caps,
            runtime_adapter=data.get("runtime_adapter", "anthropic"),
            tags=data.get("tags", []),
            status=AgentStatus(data.get("status", "active")),
            registered_at=data.get("registered_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            metadata=data.get("metadata", {}),
            required_roles=data.get("required_roles", []),
        )


class AgentRegistry:
    """
    Central registry for discovering and managing agents.

    The registry is the single source of truth for what agents exist,
    what they can do, and whether they are healthy.

    Example::

        registry = AgentRegistry()
        registry.register(manifest)
        found = registry.get("invoice-processor-001")
        active_agents = registry.find(tags=["finance"], status=AgentStatus.ACTIVE)
    """

    def __init__(self, backend: RegistryBackend | None = None) -> None:
        self._backend = backend or InMemoryBackend()
        self._version_history: dict[str, list[AgentManifest]] = {}

    def register(self, manifest: AgentManifest) -> None:
        """Register a new agent or update an existing one."""
        existing = self._backend.load(manifest.agent_id)
        if existing:
            # Push old version to history
            history = self._version_history.setdefault(manifest.agent_id, [])
            history.append(existing)
        self._backend.save(manifest)

    def deregister(self, agent_id: str) -> bool:
        """Remove an agent from the registry. Returns True if it existed."""
        return self._backend.delete(agent_id)

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._backend.load(agent_id)

    def find(
        self,
        tags: list[str] | None = None,
        status: AgentStatus | None = None,
        owner: str | None = None,
        runtime_adapter: str | None = None,
        capability: str | None = None,
    ) -> list[AgentManifest]:
        """
        Query the registry with optional filters.

        Example::

            agents = registry.find(tags=["nlp"], status=AgentStatus.ACTIVE)
        """
        results = self._backend.list_all()

        if tags:
            results = [a for a in results if all(t in a.tags for t in tags)]
        if status is not None:
            results = [a for a in results if a.status == status]
        if owner:
            results = [a for a in results if a.owner == owner]
        if runtime_adapter:
            results = [a for a in results if a.runtime_adapter == runtime_adapter]
        if capability:
            results = [a for a in results if capability in a.capability_names()]

        return results

    def list_all(self) -> list[AgentManifest]:
        return self._backend.list_all()

    def version_history(self, agent_id: str) -> list[AgentManifest]:
        """Return previous versions of an agent manifest."""
        return list(self._version_history.get(agent_id, []))

    def deprecate(self, agent_id: str) -> bool:
        """Mark an agent as deprecated."""
        manifest = self._backend.load(agent_id)
        if manifest is None:
            return False
        manifest.status = AgentStatus.DEPRECATED
        self._backend.save(manifest)
        return True

    def stats(self) -> dict[str, Any]:
        """Return summary statistics about the registry."""
        all_agents = self._backend.list_all()
        by_status: dict[str, int] = {}
        by_adapter: dict[str, int] = {}
        for a in all_agents:
            by_status[a.status.value] = by_status.get(a.status.value, 0) + 1
            by_adapter[a.runtime_adapter] = by_adapter.get(a.runtime_adapter, 0) + 1
        return {
            "total": len(all_agents),
            "by_status": by_status,
            "by_adapter": by_adapter,
        }
