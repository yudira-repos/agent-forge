"""Service-discovery helpers for finding agents by capability or contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import AgentManifest, AgentStatus


@dataclass
class DiscoveryQuery:
    """
    Structured query for the agent registry.

    Example::

        query = DiscoveryQuery(
            tags=["finance"],
            capability="extract_invoice",
            status=AgentStatus.ACTIVE,
            require_hitl=False,
        )
    """

    tags: list[str] = field(default_factory=list)
    capability: str | None = None
    status: AgentStatus = AgentStatus.ACTIVE
    owner: str | None = None
    runtime_adapter: str | None = None
    require_hitl: bool | None = None  # None = don't filter


@dataclass
class DiscoveryResult:
    """Ranked list of agents matching a discovery query."""

    query: DiscoveryQuery
    matches: list[AgentManifest]
    total: int

    def best_match(self) -> AgentManifest | None:
        """Return the highest-ranked match, or None if no results."""
        return self.matches[0] if self.matches else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "matches": [m.to_dict() for m in self.matches],
        }


class AgentDiscovery:
    """
    High-level discovery facade that wraps an ``AgentRegistry``.

    Adds capability-level filtering and HITL-awareness on top of the
    raw registry queries.

    Example::

        discovery = AgentDiscovery(registry)
        result = discovery.find(DiscoveryQuery(
            capability="summarize_document",
            tags=["nlp"],
        ))
        agent = result.best_match()
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def find(self, query: DiscoveryQuery) -> DiscoveryResult:
        manifests = self._registry.find(
            tags=query.tags or None,
            status=query.status,
            owner=query.owner,
            runtime_adapter=query.runtime_adapter,
            capability=query.capability,
        )

        if query.require_hitl is not None:
            manifests = [m for m in manifests if self._has_hitl_capability(m) == query.require_hitl]

        return DiscoveryResult(
            query=query,
            matches=manifests,
            total=len(manifests),
        )

    @staticmethod
    def _has_hitl_capability(manifest: AgentManifest) -> bool:
        return any(c.requires_hitl for c in manifest.capabilities)
