"""
AIAM — Agent Identity & Authority Management
============================================
Provides cryptographically-signed agent identities, role-based access control,
authority scopes, and trust-chain delegation for enterprise multi-agent systems.
"""

from .authority import AgentAuthority, AuthorityScope, ScopeEffect
from .identity import AgentCredential, AgentIdentity
from .rbac import Permission, RBACPolicy, Role
from .trust import DelegationToken, TrustChain

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
