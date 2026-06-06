"""Tests for AIAM — Agent Identity & Authority Management."""

import time

import pytest

from agentforge.aiam import (
    AgentCredential,
    AgentIdentity,
    AgentAuthority,
    AuthorityScope,
    DelegationToken,
    Permission,
    RBACPolicy,
    Role,
    ScopeEffect,
    TrustChain,
)


# ---------------------------------------------------------------------------
# AgentIdentity
# ---------------------------------------------------------------------------

class TestAgentIdentity:
    def test_create_assigns_stable_uuid(self):
        a = AgentIdentity.create("my-agent", roles=["operator"], owner="team-a")
        b = AgentIdentity.create("my-agent", roles=["operator"], owner="team-a")
        assert a.agent_id != b.agent_id  # each call produces a unique ID

    def test_roundtrip(self):
        original = AgentIdentity.create("invoice-agent", ["admin"], "finance")
        restored = AgentIdentity.from_dict(original.to_dict())
        assert restored.agent_id == original.agent_id
        assert restored.roles == original.roles


# ---------------------------------------------------------------------------
# AgentCredential
# ---------------------------------------------------------------------------

class TestAgentCredential:
    SECRET = b"test-signing-key"

    def test_issue_and_verify(self):
        identity = AgentIdentity.create("agent", ["viewer"], "team")
        cred = AgentCredential.issue(identity, scopes=["docs:read"], ttl=60, secret=self.SECRET)
        assert cred.verify(self.SECRET)

    def test_expired_credential_fails_verify(self):
        identity = AgentIdentity.create("agent", ["viewer"], "team")
        cred = AgentCredential.issue(identity, scopes=["docs:read"], ttl=-1, secret=self.SECRET)
        assert not cred.verify(self.SECRET)
        assert cred.is_expired

    def test_wrong_secret_fails_verify(self):
        identity = AgentIdentity.create("agent", ["viewer"], "team")
        cred = AgentCredential.issue(identity, scopes=["docs:read"], ttl=60, secret=self.SECRET)
        assert not cred.verify(b"wrong-key")


# ---------------------------------------------------------------------------
# AuthorityScope & AgentAuthority
# ---------------------------------------------------------------------------

class TestAgentAuthority:
    def test_allow_scope_grants_access(self):
        auth = AgentAuthority(
            agent_id="x",
            scopes=[AuthorityScope("documents:read")],
        )
        assert auth.can("documents", "read")
        assert not auth.can("documents", "write")

    def test_explicit_deny_overrides_allow(self):
        auth = AgentAuthority(
            agent_id="x",
            scopes=[
                AuthorityScope("payments:*"),
                AuthorityScope("payments:initiate", effect=ScopeEffect.DENY),
            ],
        )
        assert not auth.can("payments", "initiate")
        assert auth.can("payments", "read")

    def test_wildcard_scope(self):
        auth = AgentAuthority(agent_id="x", scopes=[AuthorityScope("*:*")])
        assert auth.can("anything", "anywhere")

    def test_grant_returns_new_authority(self):
        a = AgentAuthority(agent_id="x", scopes=[])
        b = a.grant(AuthorityScope("users:read"))
        assert not a.can("users", "read")
        assert b.can("users", "read")


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class TestRBACPolicy:
    def test_baseline_viewer_can_read_agents(self):
        policy = RBACPolicy.enterprise_baseline()
        assert policy.agent_has_permission(["viewer"], Permission("agents", "read"))

    def test_admin_inherits_all(self):
        policy = RBACPolicy.enterprise_baseline()
        # Admin inherits supervisor → operator → viewer
        assert policy.agent_has_permission(["admin"], Permission("agents", "list"))
        assert policy.agent_has_permission(["admin"], Permission("agents", "invoke"))
        assert policy.agent_has_permission(["admin"], Permission("hitl", "approve"))
        assert policy.agent_has_permission(["admin"], Permission("agents", "register"))

    def test_viewer_cannot_register(self):
        policy = RBACPolicy.enterprise_baseline()
        assert not policy.agent_has_permission(["viewer"], Permission("agents", "register"))

    def test_cycle_guard(self):
        policy = RBACPolicy()
        policy.define_role(Role("a", extends=["b"]))
        policy.define_role(Role("b", extends=["a"]))
        # Should not infinite-loop; returns empty set for cycled roles
        perms = policy.effective_permissions("a")
        assert isinstance(perms, set)


# ---------------------------------------------------------------------------
# TrustChain
# ---------------------------------------------------------------------------

class TestTrustChain:
    SECRET = b"chain-secret"

    def _token(self, delegator: str, delegate: str, scopes: list[str]) -> DelegationToken:
        return DelegationToken.create(delegator, delegate, scopes, ttl=300, secret=self.SECRET)

    def test_valid_chain(self):
        t1 = self._token("root", "mid", ["docs:read", "docs:write"])
        t2 = self._token("mid", "leaf", ["docs:read"])
        chain = TrustChain.build([t1, t2], self.SECRET)
        assert chain.root_agent == "root"
        assert chain.leaf_agent == "leaf"
        assert chain.effective_scopes() == {"docs:read"}  # intersection

    def test_broken_chain_raises(self):
        t1 = self._token("root", "mid", ["docs:read"])
        t2 = self._token("other", "leaf", ["docs:read"])
        with pytest.raises(ValueError, match="Chain broken"):
            TrustChain.build([t1, t2], self.SECRET)

    def test_expired_token_raises(self):
        expired = DelegationToken.create("root", "leaf", ["docs:read"], ttl=-1, secret=self.SECRET)
        with pytest.raises(ValueError, match="invalid signature or is expired"):
            TrustChain.build([expired], self.SECRET)
