"""Role-Based Access Control (RBAC) for enterprise agent systems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Permission:
    """Atomic permission: a resource + action pair."""

    resource: str
    action: str

    def __str__(self) -> str:
        return f"{self.resource}:{self.action}"

    @classmethod
    def from_str(cls, s: str) -> "Permission":
        parts = s.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid permission string: '{s}'")
        return cls(resource=parts[0], action=parts[1])


@dataclass
class Role:
    """
    A named collection of permissions.

    Roles support inheritance: a role may ``extend`` parent roles and
    automatically inherits all their permissions.

    Example::

        reader = Role("reader", permissions={Permission("docs", "read")})
        editor = Role("editor", permissions={Permission("docs", "write")}, extends=["reader"])
        policy = RBACPolicy(roles=[reader, editor])
        assert policy.role_has_permission("editor", Permission("docs", "read"))
    """

    name: str
    permissions: set[Permission] = field(default_factory=set)
    extends: list[str] = field(default_factory=list)
    description: str = ""

    def add_permission(self, permission: Permission) -> None:
        self.permissions.add(permission)


@dataclass
class RBACPolicy:
    """
    RBAC policy store for an AgentForge deployment.

    Resolves role inheritance and evaluates whether an agent (identified
    by its roles) has a given permission.

    Example::

        policy = RBACPolicy()
        policy.define_role(Role("auditor", {Permission("audit-logs", "read")}))
        policy.define_role(Role("admin", extends=["auditor"]))

        policy.agent_has_permission(
            agent_roles=["admin"],
            permission=Permission("audit-logs", "read"),
        )  # True — inherited
    """

    _roles: dict[str, Role] = field(default_factory=dict)

    def define_role(self, role: Role) -> None:
        """Register a role in this policy."""
        self._roles[role.name] = role

    def get_role(self, name: str) -> Role | None:
        return self._roles.get(name)

    def effective_permissions(
        self, role_name: str, _visited: set[str] | None = None
    ) -> set[Permission]:
        """Resolve all permissions for a role, including inherited ones."""
        visited = _visited or set()
        if role_name in visited:
            return set()  # cycle guard
        visited.add(role_name)

        role = self._roles.get(role_name)
        if role is None:
            return set()

        perms: set[Permission] = set(role.permissions)
        for parent in role.extends:
            perms |= self.effective_permissions(parent, visited)
        return perms

    def role_has_permission(self, role_name: str, permission: Permission) -> bool:
        return permission in self.effective_permissions(role_name)

    def agent_has_permission(
        self,
        agent_roles: Iterable[str],
        permission: Permission,
    ) -> bool:
        """True if *any* of the agent's roles grants the permission."""
        return any(self.role_has_permission(r, permission) for r in agent_roles)

    def all_roles(self) -> list[Role]:
        return list(self._roles.values())

    # ------------------------------------------------------------------
    # Built-in enterprise baseline roles
    # ------------------------------------------------------------------

    @classmethod
    def enterprise_baseline(cls) -> "RBACPolicy":
        """
        Return a pre-built policy with sensible enterprise defaults.

        Roles: viewer → operator → supervisor → admin
        """
        policy = cls()
        policy.define_role(
            Role(
                "viewer",
                permissions={
                    Permission("agents", "list"),
                    Permission("agents", "read"),
                    Permission("audit-logs", "read"),
                },
                description="Read-only access to agent state and audit logs",
            )
        )
        policy.define_role(
            Role(
                "operator",
                permissions={
                    Permission("agents", "invoke"),
                    Permission("agents", "pause"),
                },
                extends=["viewer"],
                description="Can invoke and pause agents",
            )
        )
        policy.define_role(
            Role(
                "supervisor",
                permissions={
                    Permission("hitl", "approve"),
                    Permission("hitl", "reject"),
                    Permission("agents", "terminate"),
                },
                extends=["operator"],
                description="Can make HITL decisions and terminate agents",
            )
        )
        policy.define_role(
            Role(
                "admin",
                permissions={
                    Permission("agents", "register"),
                    Permission("agents", "delete"),
                    Permission("policy", "write"),
                    Permission("registry", "write"),
                },
                extends=["supervisor"],
                description="Full administrative access",
            )
        )
        return policy
