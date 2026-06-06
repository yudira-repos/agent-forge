"""Tests for the Agent Registry."""

import pytest

from agentforge.registry import (
    AgentCapability,
    AgentManifest,
    AgentRegistry,
    AgentStatus,
    DiscoveryQuery,
)
from agentforge.registry.discovery import AgentDiscovery
from agentforge.registry.registry import SQLiteBackend


def make_manifest(agent_id: str, tags: list[str] | None = None, owner: str = "team") -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        name=f"Agent {agent_id}",
        version="1.0.0",
        description="Test agent",
        owner=owner,
        tags=tags or [],
        capabilities=[
            AgentCapability("summarize", "Summarize text", tags=["nlp"]),
            AgentCapability("extract", "Extract data", tags=["extraction"], requires_hitl=True),
        ],
    )


class TestAgentRegistry:
    def test_register_and_get(self):
        registry = AgentRegistry()
        manifest = make_manifest("agent-001")
        registry.register(manifest)
        found = registry.get("agent-001")
        assert found is not None
        assert found.agent_id == "agent-001"

    def test_get_missing_returns_none(self):
        registry = AgentRegistry()
        assert registry.get("nonexistent") is None

    def test_deregister(self):
        registry = AgentRegistry()
        registry.register(make_manifest("agent-001"))
        assert registry.deregister("agent-001")
        assert registry.get("agent-001") is None
        assert not registry.deregister("agent-001")  # already gone

    def test_find_by_tag(self):
        registry = AgentRegistry()
        registry.register(make_manifest("a1", tags=["finance"]))
        registry.register(make_manifest("a2", tags=["nlp"]))
        registry.register(make_manifest("a3", tags=["finance", "nlp"]))

        results = registry.find(tags=["finance"])
        ids = {r.agent_id for r in results}
        assert ids == {"a1", "a3"}

    def test_find_by_owner(self):
        registry = AgentRegistry()
        registry.register(make_manifest("a1", owner="team-a"))
        registry.register(make_manifest("a2", owner="team-b"))
        results = registry.find(owner="team-a")
        assert len(results) == 1
        assert results[0].owner == "team-a"

    def test_version_history(self):
        registry = AgentRegistry()
        v1 = make_manifest("agent-001")
        v1.version = "1.0.0"
        registry.register(v1)

        v2 = make_manifest("agent-001")
        v2.version = "2.0.0"
        registry.register(v2)

        history = registry.version_history("agent-001")
        assert len(history) == 1
        assert history[0].version == "1.0.0"
        assert registry.get("agent-001").version == "2.0.0"

    def test_deprecate(self):
        registry = AgentRegistry()
        registry.register(make_manifest("agent-001"))
        assert registry.deprecate("agent-001")
        assert registry.get("agent-001").status == AgentStatus.DEPRECATED

    def test_stats(self):
        registry = AgentRegistry()
        registry.register(make_manifest("a1"))
        registry.register(make_manifest("a2"))
        stats = registry.stats()
        assert stats["total"] == 2

    def test_sqlite_backend_persists(self, tmp_path):
        db = str(tmp_path / "registry.db")
        backend = SQLiteBackend(db_path=db)
        registry = AgentRegistry(backend=backend)
        registry.register(make_manifest("agent-sqlite"))

        # Re-open
        backend2 = SQLiteBackend(db_path=db)
        registry2 = AgentRegistry(backend=backend2)
        assert registry2.get("agent-sqlite") is not None


class TestAgentDiscovery:
    def test_find_by_capability(self):
        registry = AgentRegistry()
        registry.register(make_manifest("a1"))
        discovery = AgentDiscovery(registry)
        result = discovery.find(DiscoveryQuery(capability="summarize"))
        assert result.total == 1
        assert result.best_match().agent_id == "a1"

    def test_no_match_returns_empty(self):
        registry = AgentRegistry()
        registry.register(make_manifest("a1"))
        discovery = AgentDiscovery(registry)
        result = discovery.find(DiscoveryQuery(capability="nonexistent"))
        assert result.total == 0
        assert result.best_match() is None

    def test_hitl_filter(self):
        registry = AgentRegistry()
        registry.register(make_manifest("a1"))
        discovery = AgentDiscovery(registry)

        # a1 has at least one hitl capability
        result = discovery.find(DiscoveryQuery(require_hitl=True))
        assert result.total == 1

        result_no_hitl = discovery.find(DiscoveryQuery(require_hitl=False))
        assert result_no_hitl.total == 0
