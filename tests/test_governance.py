"""Tests for the Governance framework."""

import pytest

from agentforge.governance import (
    GDPRProfile,
    HIPAAProfile,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    SOC2Profile,
)
from agentforge.governance.policy import Policy


def ctx(action: str, resource: str, roles: list[str] | None = None, env: str = "production", **meta) -> PolicyContext:
    return PolicyContext(
        agent_id="test-agent",
        agent_roles=roles or ["operator"],
        action=action,
        resource=resource,
        environment=env,
        metadata=meta,
    )


class TestPolicyEngine:
    def test_empty_engine_allows_everything(self):
        engine = PolicyEngine()
        decision = engine.evaluate(ctx("delete", "invoices"))
        assert decision.is_allowed

    def test_deny_rule_blocks(self):
        engine = PolicyEngine()
        policy = Policy("block-delete")
        policy.add_rule(PolicyRule(
            name="deny-all-delete",
            effect=PolicyEffect.DENY,
            conditions=[lambda c: c.action == "delete"],
        ))
        engine.add_policy(policy)
        decision = engine.evaluate(ctx("delete", "invoices"))
        assert decision.is_denied

    def test_hitl_rule_triggers(self):
        engine = PolicyEngine()
        policy = Policy("hitl-large-amounts")
        policy.add_rule(PolicyRule(
            name="hitl-over-10k",
            effect=PolicyEffect.REQUIRE_HITL,
            conditions=[
                lambda c: c.action == "initiate",
                lambda c: c.get("amount_usd", 0) > 10_000,
            ],
        ))
        engine.add_policy(policy)

        low = ctx("initiate", "payments", amount_usd=5_000)
        high = ctx("initiate", "payments", amount_usd=50_000)
        assert engine.evaluate(low).is_allowed
        assert engine.evaluate(high).requires_hitl

    def test_deny_beats_hitl(self):
        engine = PolicyEngine()
        p1 = Policy("deny")
        p1.add_rule(PolicyRule("always-deny", PolicyEffect.DENY, [lambda _: True], priority=10))
        p2 = Policy("hitl")
        p2.add_rule(PolicyRule("always-hitl", PolicyEffect.REQUIRE_HITL, [lambda _: True], priority=20))
        engine.add_policy(p1)
        engine.add_policy(p2)
        assert engine.evaluate(ctx("read", "anything")).is_denied

    def test_env_lockdown(self):
        engine = PolicyEngine()
        engine.add_policy(Policy.env_lockdown(["delete", "purge"]))
        assert engine.evaluate(ctx("delete", "x", env="production")).is_denied
        assert engine.evaluate(ctx("delete", "x", env="staging")).is_allowed

    def test_remove_policy(self):
        engine = PolicyEngine()
        p = Policy("temp")
        p.add_rule(PolicyRule("d", PolicyEffect.DENY, [lambda _: True]))
        engine.add_policy(p)
        assert engine.evaluate(ctx("read", "x")).is_denied
        engine.remove_policy("temp")
        assert engine.evaluate(ctx("read", "x")).is_allowed


class TestSOC2Profile:
    def test_production_delete_requires_hitl(self):
        engine = SOC2Profile.engine()
        decision = engine.evaluate(ctx("delete", "invoices", env="production"))
        assert decision.requires_hitl

    def test_staging_delete_allowed(self):
        engine = SOC2Profile.engine()
        decision = engine.evaluate(ctx("delete", "invoices", env="staging"))
        assert decision.is_allowed

    def test_pii_denied_without_role(self):
        engine = SOC2Profile.engine()
        decision = engine.evaluate(ctx("read", "pii-records", roles=["operator"]))
        assert decision.is_denied

    def test_pii_allowed_with_compliance_role(self):
        engine = SOC2Profile.engine()
        decision = engine.evaluate(ctx("read", "pii-records", roles=["compliance"]))
        assert decision.is_allowed


class TestHIPAAProfile:
    def test_phi_denied_without_healthcare_role(self):
        engine = HIPAAProfile.engine()
        decision = engine.evaluate(ctx("read", "patient-records", roles=["operator"]))
        assert decision.is_denied

    def test_phi_allowed_with_healthcare_role(self):
        engine = HIPAAProfile.engine()
        decision = engine.evaluate(ctx("read", "patient-records", roles=["healthcare-provider"]))
        assert decision.is_allowed

    def test_phi_delete_always_requires_hitl(self):
        engine = HIPAAProfile.engine()
        decision = engine.evaluate(ctx("delete", "phi-records", roles=["admin"]))
        assert decision.requires_hitl


class TestGDPRProfile:
    def test_erasure_requires_hitl(self):
        engine = GDPRProfile.engine()
        decision = engine.evaluate(ctx("erase", "personal-data"))
        assert decision.requires_hitl

    def test_cross_border_transfer_is_audited(self):
        engine = GDPRProfile.engine()
        c = PolicyContext(
            agent_id="x", agent_roles=["operator"],
            action="transfer", resource="user-records",
            environment="production",
            metadata={"cross_border_transfer": True},
        )
        decision = engine.evaluate(c)
        assert decision.effect == PolicyEffect.AUDIT
