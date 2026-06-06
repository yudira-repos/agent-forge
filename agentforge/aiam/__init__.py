"""
AIAM — Agent Identity & Authority Management
============================================
Provides cryptographically-signed agent identities, role-based access control,
authority scopes, and trust-chain delegation for enterprise multi-agent systems.
"""

from .identity import AgentIdentity, AgentCredential
from .authority import AuthorityScope, AgentAuthority, ScopeEffect
from .rbac import Role, Permission, RBACPolicy
from .trust import TrustChain, DelegationToken

__all__ = [
    "AgentIdentity",
    "AgentCredential",
    "AuthorityScope",
    "AgentAuthority",
    "ScopeEffect",
    "Role",
    "Permission",
    "RBACPolicy",
    "TrustChain",
    "DelegationToken",
]
