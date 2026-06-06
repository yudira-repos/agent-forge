"""Authority scopes — fine-grained permission tokens for agent actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ScopeEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class AuthorityScope:
    """
    A single permission scope that an agent may hold.

    Scopes follow the pattern ``resource:action``, e.g.::

        AuthorityScope("documents:read")
        AuthorityScope("invoices:write")
        AuthorityScope("*:read")          # wildcard action
        AuthorityScope("*:*")             # super-user (use sparingly)

    Example::

        scope = AuthorityScope("payments:initiate")
        assert scope.matches("payments", "initiate")   # True
        assert scope.matches("payments", "read")       # False
    """

    scope: str
    effect: ScopeEffect = ScopeEffect.ALLOW
    conditions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parts = self.scope.split(":")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid scope '{self.scope}'. Must be 'resource:action'."
            )

    @property
    def resource(self) -> str:
        return self.scope.split(":")[0]

    @property
    def action(self) -> str:
        return self.scope.split(":")[1]

    def matches(self, resource: str, action: str) -> bool:
        """True if this scope grants access to resource+action."""
        r_match = self.resource in (resource, "*")
        a_match = self.action in (action, "*")
        return r_match and a_match

    def __str__(self) -> str:
        return f"{self.scope}[{self.effect.value}]"


@dataclass
class AgentAuthority:
    """
    The complete set of authority scopes held by an agent.

    Implements an explicit-deny-wins evaluation model: if any scope
    with ``effect=DENY`` matches, access is refused regardless of
    allow scopes.

    Example::

        authority = AgentAuthority(agent_id="agent-123", scopes=[
            AuthorityScope("documents:read"),
            AuthorityScope("documents:write"),
            AuthorityScope("payments:*", effect=ScopeEffect.DENY),
        ])

        authority.can("documents", "read")    # True
        authority.can("payments", "initiate") # False  — explicit deny
        authority.can("users", "list")        # False  — no allow scope
    """

    agent_id: str
    scopes: list[AuthorityScope] = field(default_factory=list)

    def can(self, resource: str, action: str) -> bool:
        """Evaluate whether this authority permits resource:action."""
        # Explicit deny wins
        for scope in self.scopes:
            if scope.effect == ScopeEffect.DENY and scope.matches(resource, action):
                return False
        # At least one allow must match
        return any(
            s.effect == ScopeEffect.ALLOW and s.matches(resource, action)
            for s in self.scopes
        )

    def grant(self, *scopes: AuthorityScope) -> "AgentAuthority":
        """Return a new authority with additional scopes added."""
        return AgentAuthority(
            agent_id=self.agent_id,
            scopes=list(self.scopes) + list(scopes),
        )

    def revoke(self, scope_str: str) -> "AgentAuthority":
        """Return a new authority with the named scope removed."""
        return AgentAuthority(
            agent_id=self.agent_id,
            scopes=[s for s in self.scopes if s.scope != scope_str],
        )

    def allowed_scopes(self) -> list[str]:
        return [s.scope for s in self.scopes if s.effect == ScopeEffect.ALLOW]
