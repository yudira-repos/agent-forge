"""Policy definition — rules, conditions, and evaluation context."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HITL = "require_hitl"  # escalate to human review
    AUDIT = "audit"               # allow but record


@dataclass
class PolicyContext:
    """
    Runtime context passed to a policy for evaluation.

    Captures everything a policy might need: who the agent is,
    what it's trying to do, and the broader request metadata.

    Example::

        ctx = PolicyContext(
            agent_id="invoice-processor-001",
            agent_roles=["operator"],
            action="write",
            resource="payment-records",
            environment="production",
            metadata={"amount_usd": 50000},
        )
    """

    agent_id: str
    agent_roles: list[str]
    action: str
    resource: str
    environment: str = "production"
    metadata: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)


# A condition is a callable that takes a PolicyContext and returns bool.
Condition = Callable[[PolicyContext], bool]


@dataclass
class PolicyRule:
    """
    A single rule within a policy.

    A rule matches if *all* its conditions evaluate to True.
    When a rule matches, its ``effect`` is returned.

    Example::

        high_value_rule = PolicyRule(
            name="require-hitl-for-high-value-payments",
            effect=PolicyEffect.REQUIRE_HITL,
            conditions=[
                lambda ctx: ctx.resource == "payments",
                lambda ctx: ctx.get("amount_usd", 0) > 10_000,
            ],
            description="Payments over $10k require human approval",
        )
    """

    name: str
    effect: PolicyEffect
    conditions: list[Condition] = field(default_factory=list)
    description: str = ""
    priority: int = 100  # lower = evaluated first

    def matches(self, context: PolicyContext) -> bool:
        """True if ALL conditions are satisfied."""
        return all(cond(context) for cond in self.conditions)


@dataclass
class Policy:
    """
    Named collection of rules evaluated against a PolicyContext.

    Rules are evaluated in priority order (lowest first).  The first
    matching rule's effect is returned.  If no rule matches, the
    ``default_effect`` is used.

    Example::

        policy = Policy(
            name="payment-governance",
            rules=[high_value_rule, block_external_rule],
            default_effect=PolicyEffect.ALLOW,
        )
    """

    name: str
    rules: list[PolicyRule] = field(default_factory=list)
    default_effect: PolicyEffect = PolicyEffect.ALLOW
    description: str = ""
    version: str = "1.0.0"
    enabled: bool = True

    def evaluate(self, context: PolicyContext) -> PolicyEffect:
        """Evaluate rules in priority order and return the matched effect."""
        if not self.enabled:
            return PolicyEffect.ALLOW

        sorted_rules = sorted(self.rules, key=lambda r: r.priority)
        for rule in sorted_rules:
            if rule.matches(context):
                return rule.effect
        return self.default_effect

    def add_rule(self, rule: PolicyRule) -> "Policy":
        self.rules.append(rule)
        return self

    # ------------------------------------------------------------------
    # Convenience factory methods for common enterprise patterns
    # ------------------------------------------------------------------

    @classmethod
    def env_lockdown(cls, blocked_actions: list[str], env: str = "production") -> "Policy":
        """
        Deny specific actions in a given environment.

        Example::

            policy = Policy.env_lockdown(["delete", "purge"], env="production")
        """
        rules = [
            PolicyRule(
                name=f"block-{action}-in-{env}",
                effect=PolicyEffect.DENY,
                conditions=[
                    lambda ctx, a=action: ctx.action == a,
                    lambda ctx, e=env: ctx.environment == e,
                ],
                description=f"Block '{action}' in {env}",
                priority=10,
            )
            for action in blocked_actions
        ]
        return cls(name=f"{env}-lockdown", rules=rules)

    @classmethod
    def resource_allowlist(cls, allowed_resources: list[str]) -> "Policy":
        """Only allow access to explicitly listed resources."""
        pattern = re.compile(
            "^(" + "|".join(re.escape(r) for r in allowed_resources) + ")$"
        )
        rule = PolicyRule(
            name="deny-unlisted-resources",
            effect=PolicyEffect.DENY,
            conditions=[lambda ctx: not pattern.match(ctx.resource)],
            description="Deny access to any resource not in the allowlist",
            priority=5,
        )
        return cls(name="resource-allowlist", rules=[rule], default_effect=PolicyEffect.ALLOW)
