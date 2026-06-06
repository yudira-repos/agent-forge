"""Policy engine — evaluates a stack of policies and produces a final decision."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .policy import Policy, PolicyContext, PolicyEffect


@dataclass
class PolicyDecision:
    """
    The final result of evaluating all policies for a given context.

    Contains the composite effect (DENY beats ALLOW), the list of
    matched rules for audit purposes, and any supplementary metadata.

    Example::

        decision = engine.evaluate(context)
        if not decision.is_allowed:
            raise PermissionError(decision.reason)
        if decision.requires_hitl:
            await hitl.request_approval(context)
    """

    effect: PolicyEffect
    matched_rules: list[str]  # rule names that matched
    context: PolicyContext
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_allowed(self) -> bool:
        return self.effect in (PolicyEffect.ALLOW, PolicyEffect.AUDIT)

    @property
    def is_denied(self) -> bool:
        return self.effect == PolicyEffect.DENY

    @property
    def requires_hitl(self) -> bool:
        return self.effect == PolicyEffect.REQUIRE_HITL

    @property
    def reason(self) -> str:
        if self.is_denied:
            return f"Denied by rule(s): {', '.join(self.matched_rules)}"
        if self.requires_hitl:
            return f"Human review required (rule: {', '.join(self.matched_rules)})"
        return "Allowed"


class PolicyEngine:
    """
    Composite policy evaluator for enterprise agent governance.

    Evaluates an ordered list of policies and applies a strict
    precedence model:
      1. Any DENY result → final decision is DENY.
      2. Any REQUIRE_HITL (with no DENY) → final decision is REQUIRE_HITL.
      3. Any AUDIT (with no DENY/HITL) → final decision is AUDIT.
      4. Otherwise → ALLOW.

    Example::

        engine = PolicyEngine()
        engine.add_policy(Policy.env_lockdown(["delete"], env="production"))
        engine.add_policy(high_value_payments_policy)

        decision = engine.evaluate(PolicyContext(
            agent_id="agent-001",
            agent_roles=["operator"],
            action="delete",
            resource="invoices",
            environment="production",
        ))
        assert decision.is_denied
    """

    def __init__(self) -> None:
        self._policies: list[Policy] = []

    def add_policy(self, policy: Policy) -> "PolicyEngine":
        """Register a policy. Returns self for chaining."""
        self._policies.append(policy)
        return self

    def remove_policy(self, name: str) -> bool:
        before = len(self._policies)
        self._policies = [p for p in self._policies if p.name != name]
        return len(self._policies) < before

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        """
        Evaluate all registered policies against *context*.

        Returns a PolicyDecision with the composite effect.
        """
        effects: list[tuple[PolicyEffect, str]] = []  # (effect, rule_name)

        for policy in self._policies:
            if not policy.enabled:
                continue
            for rule in sorted(policy.rules, key=lambda r: r.priority):
                if rule.matches(context):
                    effects.append((rule.effect, f"{policy.name}/{rule.name}"))

        matched_rules = [name for _, name in effects]

        # Precedence: DENY > REQUIRE_HITL > AUDIT > ALLOW
        for effect, _ in effects:
            if effect == PolicyEffect.DENY:
                return PolicyDecision(
                    effect=PolicyEffect.DENY,
                    matched_rules=matched_rules,
                    context=context,
                )

        for effect, _ in effects:
            if effect == PolicyEffect.REQUIRE_HITL:
                return PolicyDecision(
                    effect=PolicyEffect.REQUIRE_HITL,
                    matched_rules=matched_rules,
                    context=context,
                )

        for effect, _ in effects:
            if effect == PolicyEffect.AUDIT:
                return PolicyDecision(
                    effect=PolicyEffect.AUDIT,
                    matched_rules=matched_rules,
                    context=context,
                )

        return PolicyDecision(
            effect=PolicyEffect.ALLOW,
            matched_rules=matched_rules,
            context=context,
        )

    def policy_count(self) -> int:
        return len(self._policies)

    def list_policies(self) -> list[str]:
        return [p.name for p in self._policies]
