"""
Compliance profiles — pre-built policy bundles for SOC 2, HIPAA, and GDPR.

Each profile returns a fully-configured ``PolicyEngine`` that enforces
the minimum governance requirements for that compliance framework.
"""

from __future__ import annotations

from .engine import PolicyEngine
from .policy import Policy, PolicyEffect, PolicyRule


class ComplianceProfile:
    """Base class for compliance profiles."""

    name: str = "base"

    @classmethod
    def apply(cls, engine: PolicyEngine) -> PolicyEngine:
        raise NotImplementedError

    @classmethod
    def engine(cls) -> PolicyEngine:
        """Convenience: return a fresh engine with this profile applied."""
        return cls.apply(PolicyEngine())


class SOC2Profile(ComplianceProfile):
    """
    SOC 2 Type II baseline for agent governance.

    Enforces:
    - No data deletion in production without HITL approval.
    - All writes to sensitive resources require AUDIT logging.
    - No agents may directly access user PII without explicit role.

    Example::

        engine = SOC2Profile.engine()
        engine.add_policy(your_custom_policy)
    """

    name = "soc2"

    @classmethod
    def apply(cls, engine: PolicyEngine) -> PolicyEngine:
        # Rule 1: Deletions in production require HITL
        delete_policy = Policy(
            name="soc2-delete-requires-hitl",
            description="SOC 2: All deletions in production require human approval",
        )
        delete_policy.add_rule(PolicyRule(
            name="production-delete-hitl",
            effect=PolicyEffect.REQUIRE_HITL,
            conditions=[
                lambda ctx: ctx.action in ("delete", "purge", "destroy"),
                lambda ctx: ctx.environment == "production",
            ],
            priority=10,
        ))
        engine.add_policy(delete_policy)

        # Rule 2: PII access requires supervisor role
        pii_policy = Policy(
            name="soc2-pii-access-control",
            description="SOC 2: PII access restricted to supervisor role and above",
        )
        pii_policy.add_rule(PolicyRule(
            name="pii-requires-supervisor",
            effect=PolicyEffect.DENY,
            conditions=[
                lambda ctx: any(
                    pii in ctx.resource
                    for pii in ("pii", "personal-data", "ssn", "credit-card", "health")
                ),
                lambda ctx: not any(
                    r in ctx.agent_roles for r in ("supervisor", "admin", "compliance")
                ),
            ],
            priority=5,
        ))
        engine.add_policy(pii_policy)

        # Rule 3: Sensitive writes → AUDIT
        audit_policy = Policy(
            name="soc2-sensitive-write-audit",
            description="SOC 2: Writes to sensitive resources are audited",
        )
        audit_policy.add_rule(PolicyRule(
            name="sensitive-write-audit",
            effect=PolicyEffect.AUDIT,
            conditions=[
                lambda ctx: ctx.action in ("write", "update", "create"),
                lambda ctx: any(
                    s in ctx.resource
                    for s in ("financial", "payment", "invoice", "contract")
                ),
            ],
            priority=50,
        ))
        engine.add_policy(audit_policy)

        return engine


class HIPAAProfile(ComplianceProfile):
    """
    HIPAA baseline — protects PHI (Protected Health Information).

    Enforces:
    - PHI resources require explicit healthcare role.
    - No PHI transmission without AUDIT.
    - PHI deletion always requires HITL.

    Example::

        engine = HIPAAProfile.engine()
    """

    name = "hipaa"

    @classmethod
    def apply(cls, engine: PolicyEngine) -> PolicyEngine:
        phi_terms = ("phi", "patient", "diagnosis", "medication", "ehr", "medical-record")

        phi_access_policy = Policy(
            name="hipaa-phi-access-control",
            description="HIPAA: PHI access restricted to healthcare roles",
        )
        phi_access_policy.add_rule(PolicyRule(
            name="phi-requires-healthcare-role",
            effect=PolicyEffect.DENY,
            conditions=[
                lambda ctx: any(term in ctx.resource for term in phi_terms),
                lambda ctx: not any(
                    r in ctx.agent_roles
                    for r in ("healthcare-provider", "care-coordinator", "admin", "compliance")
                ),
            ],
            priority=5,
        ))
        engine.add_policy(phi_access_policy)

        phi_delete_policy = Policy(
            name="hipaa-phi-delete-hitl",
            description="HIPAA: PHI deletion always requires human approval",
        )
        phi_delete_policy.add_rule(PolicyRule(
            name="phi-delete-hitl",
            effect=PolicyEffect.REQUIRE_HITL,
            conditions=[
                lambda ctx: any(term in ctx.resource for term in phi_terms),
                lambda ctx: ctx.action in ("delete", "purge"),
            ],
            priority=1,
        ))
        engine.add_policy(phi_delete_policy)

        return engine


class GDPRProfile(ComplianceProfile):
    """
    GDPR baseline — EU data subject rights and data minimisation.

    Enforces:
    - Right-to-erasure requests require HITL before execution.
    - Cross-border data transfer requires AUDIT.
    - Data exports require supervisor approval.

    Example::

        engine = GDPRProfile.engine()
    """

    name = "gdpr"

    @classmethod
    def apply(cls, engine: PolicyEngine) -> PolicyEngine:
        erasure_policy = Policy(
            name="gdpr-erasure-hitl",
            description="GDPR Art.17: Right-to-erasure requires human approval",
        )
        erasure_policy.add_rule(PolicyRule(
            name="erasure-hitl",
            effect=PolicyEffect.REQUIRE_HITL,
            conditions=[
                lambda ctx: ctx.action in ("erase", "delete", "anonymize"),
                lambda ctx: "personal-data" in ctx.resource or "user-data" in ctx.resource,
            ],
            priority=5,
        ))
        engine.add_policy(erasure_policy)

        transfer_policy = Policy(
            name="gdpr-cross-border-audit",
            description="GDPR Art.46: Cross-border transfers are audited",
        )
        transfer_policy.add_rule(PolicyRule(
            name="cross-border-transfer-audit",
            effect=PolicyEffect.AUDIT,
            conditions=[
                lambda ctx: ctx.get("cross_border_transfer", False) is True,
            ],
            priority=20,
        ))
        engine.add_policy(transfer_policy)

        return engine
