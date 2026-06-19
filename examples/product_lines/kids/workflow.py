"""
Kids Activities Workflow — Activity Discovery & Booking
========================================================
Product line: KIDS

Agents in this workflow:

  1. ActivityDiscoveryAgent  — finds age-appropriate activities (autonomous)
  2. ScheduleConflictAgent   — checks the child's calendar (autonomous, flags
                               soft conflicts for HITL)
  3. SafetyScreeningAgent    — validates age-appropriateness + safety flags
                               (HITL if any safety flag raised)
  4. BookingAgent            — confirms the booking (HITL always —
                               parental consent required)

Compliance:  COPPA-aligned rules (never auto-book for a minor without consent)
Escalation:  parental-consent policy (primary parent → secondary guardian)
Channel:     sms + app-push notification

HITL contract
─────────────
All HITL calls route through the shared HITLRegistry with ProductLine.KIDS.
Parents are the reviewers — the context renderer surfaces the child name, age,
activity details, cost, and any safety flags in a parent-friendly format.
The system NEVER books without explicit parental approval.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from agentforge.audit import AuditLogger, AuditTrail, EventSeverity, EventType
from agentforge.audit.logger import InMemoryAuditSink
from agentforge.governance import PolicyContext, PolicyEngine
from agentforge.governance.policy import Policy, PolicyEffect, PolicyRule
from agentforge.hitl import ApprovalDecision

from ..shared.hitl_registry import HITLRegistry, ProductLine


# ─────────────────────────────────────────────────────────────────────────────
# Domain models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Child:
    child_id: str
    name: str
    age: int
    allergies: list[str] = field(default_factory=list)
    existing_schedule: list[str] = field(default_factory=list)  # day strings


@dataclass
class Activity:
    activity_id: str
    name: str
    provider: str
    min_age: int
    max_age: int
    cost_usd: float
    scheduled_day: str
    duration_hours: float
    safety_notes: list[str] = field(default_factory=list)
    requires_equipment: list[str] = field(default_factory=list)


@dataclass
class BookingDecision:
    activity: Activity
    child: Child
    safety_flags: list[str]
    schedule_conflict: bool
    requires_hitl: bool
    approved: bool = False
    reviewer_id: str | None = None
    rejection_reason: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# COPPA-aligned governance policy
# ─────────────────────────────────────────────────────────────────────────────

def _coppa_engine() -> PolicyEngine:
    """
    COPPA-aligned policy: any booking for a minor requires parental consent
    (HITL).  Safety flags escalate to immediate HITL regardless of cost.
    """
    engine = PolicyEngine()

    consent_policy = Policy(
        name="coppa-parental-consent",
        description="COPPA: All bookings for minors require parental consent",
    )
    consent_policy.add_rule(PolicyRule(
        name="minor-booking-hitl",
        effect=PolicyEffect.REQUIRE_HITL,
        conditions=[
            lambda ctx: ctx.action == "book_activity",
            lambda ctx: ctx.metadata.get("child_age", 99) < 18,
        ],
    ))

    safety_policy = Policy(
        name="coppa-safety-flag",
        description="COPPA: Safety flags always require parental review",
    )
    safety_policy.add_rule(PolicyRule(
        name="safety-flag-hitl",
        effect=PolicyEffect.REQUIRE_HITL,
        conditions=[
            lambda ctx: len(ctx.metadata.get("safety_flags", [])) > 0,
        ],
    ))

    engine.add_policy(consent_policy)
    engine.add_policy(safety_policy)
    return engine


# ─────────────────────────────────────────────────────────────────────────────
# Agents
# ─────────────────────────────────────────────────────────────────────────────

class ActivityDiscoveryAgent:
    """
    Finds age-appropriate activities from the activity catalog.
    Fully autonomous — pure read, no side effects.
    """

    async def discover(self, child: Child, preferences: list[str]) -> list[Activity]:
        await asyncio.sleep(0.02)
        # Simulated activity catalog
        catalog = [
            Activity("ACT-001", "Junior Soccer League", "City Parks & Rec",
                     min_age=6, max_age=12, cost_usd=45.00,
                     scheduled_day="Saturday", duration_hours=1.5,
                     safety_notes=["Cleats required", "Shin guards required"],
                     requires_equipment=["cleats", "shin guards"]),
            Activity("ACT-002", "Creative Arts Workshop", "Studio 44",
                     min_age=5, max_age=14, cost_usd=25.00,
                     scheduled_day="Wednesday", duration_hours=2.0),
            Activity("ACT-003", "Kids Robotics Club", "TechKidz",
                     min_age=8, max_age=15, cost_usd=60.00,
                     scheduled_day="Thursday", duration_hours=1.5,
                     safety_notes=["Adult supervision required for soldering"]),
            Activity("ACT-004", "Rock Climbing — Intro", "Summit Gym",
                     min_age=10, max_age=16, cost_usd=35.00,
                     scheduled_day="Saturday", duration_hours=2.0,
                     safety_notes=["Harness training required", "Waiver required"]),
        ]
        return [
            a for a in catalog
            if a.min_age <= child.age <= a.max_age
        ]


class ScheduleConflictAgent:
    """
    Checks the child's existing schedule for conflicts.
    Autonomous — flags conflicts but does not block; parent decides via HITL.
    """

    async def check(self, child: Child, activity: Activity) -> bool:
        await asyncio.sleep(0.02)
        return activity.scheduled_day in child.existing_schedule


class SafetyScreeningAgent:
    """
    Validates age-appropriateness and surfaces safety flags.

    HITL triggers:
    - Any safety note present (parent must be informed before consent)
    - Allergy conflicts with required equipment materials
    - Age is at the lower bound of the activity's range
    """

    async def screen(self, child: Child, activity: Activity) -> list[str]:
        await asyncio.sleep(0.02)
        flags: list[str] = []

        if activity.safety_notes:
            flags.extend(activity.safety_notes)

        # Check equipment allergy conflicts (e.g., latex in harness)
        latex_items = {"harness", "gloves"}
        if "latex" in child.allergies:
            conflicts = latex_items & set(activity.requires_equipment)
            if conflicts:
                flags.append(f"⚠ Possible latex allergy conflict: {', '.join(conflicts)}")

        # Flag if child is at the youngest allowed age
        if child.age == activity.min_age:
            flags.append(f"Child is at minimum age ({child.age}) — verify readiness")

        return flags


class BookingAgent:
    """
    Confirms the activity booking.

    HITL triggers:
    - ALWAYS: parental consent is non-negotiable for any booking
    - Context includes safety flags and schedule conflicts so parent
      has full picture before approving
    """

    async def book(
        self,
        child: Child,
        activity: Activity,
        safety_flags: list[str],
        schedule_conflict: bool,
        hitl_registry: HITLRegistry,
        correlation_id: str,
        logger: AuditLogger,
        gov_engine: PolicyEngine,
    ) -> BookingDecision:
        await asyncio.sleep(0.02)

        # Evaluate COPPA policy
        ctx = PolicyContext(
            agent_id="booking-agent",
            agent_roles=["kids-agent"],
            action="book_activity",
            resource="activity-booking-system",
            environment="production",
            metadata={
                "child_age": child.age,
                "safety_flags": safety_flags,
                "cost_usd": activity.cost_usd,
            },
        )
        gov_decision = gov_engine.evaluate(ctx)

        logger.log(EventType.POLICY_EVALUATED, "booking-agent", correlation_id,
                   payload={"effect": gov_decision.effect.value, "activity": activity.name})

        # Always requires HITL for minors (COPPA)
        conflict_note = " [SCHEDULE CONFLICT]" if schedule_conflict else ""
        print(
            f"\n  [BookingAgent] HITL required — parental consent needed"
            f"\n    Activity: {activity.name}{conflict_note}"
            f"\n    Cost: ${activity.cost_usd:.2f} | Day: {activity.scheduled_day}"
        )
        if safety_flags:
            print(f"    Safety flags: {len(safety_flags)} item(s)")

        context_for_parent = {
            "child_name": child.name,
            "child_age": child.age,
            "activity_name": activity.name,
            "provider": activity.provider,
            "scheduled_time": activity.scheduled_day,
            "cost_usd": activity.cost_usd,
            "safety_flags": safety_flags,
        }
        if schedule_conflict:
            context_for_parent["schedule_conflict"] = (
                f"Child already has something on {activity.scheduled_day}"
            )

        hitl_decision = await hitl_registry.request_approval(
            product_line=ProductLine.KIDS,
            agent_id="booking-agent",
            correlation_id=correlation_id,
            action="book_activity",
            resource="activity-booking-system",
            context=context_for_parent,
            timeout_seconds=30,
        )

        logger.log(
            EventType.HITL_APPROVED if hitl_decision.approved else EventType.HITL_REJECTED,
            "booking-agent", correlation_id,
            payload={"activity": activity.name, "parent": hitl_decision.reviewer_id},
        )

        return BookingDecision(
            activity=activity,
            child=child,
            safety_flags=safety_flags,
            schedule_conflict=schedule_conflict,
            requires_hitl=True,
            approved=hitl_decision.approved,
            reviewer_id=hitl_decision.reviewer_id,
            rejection_reason=hitl_decision.reason if not hitl_decision.approved else None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Workflow orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def _simulate_parent_approval(
    hitl_registry: HITLRegistry,
    approve: bool = True,
    parent_name: str = "Parent/Guardian",
    delay: float = 0.15,
) -> None:
    """Simulates a parent responding to the booking consent request."""
    await asyncio.sleep(delay)
    orchestrator = hitl_registry._orchestrators.get(ProductLine.KIDS)
    if not orchestrator:
        return
    # Use _pending directly to catch ESCALATED requests as well.
    for req in list(orchestrator._pending.values()):
        if not req.is_resolved:
            await orchestrator.decide(ApprovalDecision(
                request_id=req.request_id,
                reviewer_id=parent_name,
                approved=approve,
                reason=(
                    "Looks great — please book it!" if approve
                    else "Schedule doesn't work for us this month."
                ),
            ))


async def run_kids_workflow(
    child: Child,
    preferences: list[str],
    preferred_activity_index: int,
    hitl_registry: HITLRegistry,
    parent_approves: bool | None = None,  # None = real HITL; True/False = simulate
) -> dict[str, Any]:
    """
    Run a full kids activity discovery → safety check → booking workflow.

    The booking agent always suspends for parental consent.  If there are
    safety flags, they are prominently surfaced to the parent in the HITL
    notification before they are asked to approve.
    """
    correlation_id = f"kids-run-{uuid.uuid4().hex[:8]}"
    sink = InMemoryAuditSink()
    logger = AuditLogger(sinks=[sink])
    trail = AuditTrail(sink)
    gov_engine = _coppa_engine()

    print(f"\n{'═'*60}")
    print(f"  KIDS WORKFLOW  |  Child: {child.name} (age {child.age})")
    print(f"  Preferences: {', '.join(preferences) or 'any'}")
    print(f"{'═'*60}")

    logger.log(EventType.AGENT_STARTED, "kids-workflow", correlation_id,
               payload={"child_id": child.child_id, "child_age": child.age})

    # Step 1: Activity discovery (autonomous)
    discovery = ActivityDiscoveryAgent()
    activities = await discovery.discover(child, preferences)
    print(f"\n  [ActivityDiscovery] Found {len(activities)} age-appropriate activities")
    for i, a in enumerate(activities):
        print(f"    [{i}] {a.name} — ${a.cost_usd:.0f} ({a.scheduled_day})")

    if not activities:
        print("\n  [Workflow] No activities found — workflow complete.")
        return {"correlation_id": correlation_id, "result": "no_activities_found"}

    chosen = activities[min(preferred_activity_index, len(activities) - 1)]
    print(f"\n  [Workflow] Selected: {chosen.name}")

    # Step 2: Schedule conflict check (autonomous)
    schedule_agent = ScheduleConflictAgent()
    conflict = await schedule_agent.check(child, chosen)
    if conflict:
        print(f"\n  [ScheduleCheck] ⚠ Conflict on {chosen.scheduled_day} — will surface to parent")
    else:
        print(f"\n  [ScheduleCheck] ✓ No conflict on {chosen.scheduled_day}")

    # Step 3: Safety screening (autonomous — flags surface to parent in HITL)
    safety_agent = SafetyScreeningAgent()
    safety_flags = await safety_agent.screen(child, chosen)
    if safety_flags:
        print(f"\n  [SafetyScreening] {len(safety_flags)} safety flag(s) flagged:")
        for f in safety_flags:
            print(f"    • {f}")
    else:
        print(f"\n  [SafetyScreening] ✓ No safety flags")

    # Step 4: Booking (HITL always — parental consent)
    sim = (
        asyncio.create_task(
            _simulate_parent_approval(
                hitl_registry,
                approve=parent_approves,
                parent_name=f"{child.name}'s parent",
            )
        )
        if parent_approves is not None else None
    )
    booking_agent = BookingAgent()
    booking = await booking_agent.book(
        child, chosen, safety_flags, conflict,
        hitl_registry, correlation_id, logger, gov_engine,
    )
    if sim is not None:
        await sim

    _print_booking(booking)

    steps = trail.replay(correlation_id)
    print(f"\n  [Audit] {len(steps)} events recorded for {correlation_id}")

    return {
        "correlation_id": correlation_id,
        "child": child.name,
        "activity": chosen.name,
        "provider": chosen.provider,
        "cost_usd": chosen.cost_usd,
        "schedule_conflict": conflict,
        "safety_flags": safety_flags,
        "booked": booking.approved,
        "approved_by": booking.reviewer_id,
        "rejection_reason": booking.rejection_reason,
        "audit_events": len(steps),
    }


def _print_booking(b: BookingDecision) -> None:
    status = "✓ BOOKED" if b.approved else "✗ NOT BOOKED"
    reviewer = f" by {b.reviewer_id}" if b.reviewer_id else ""
    print(f"\n  [Booking] (HITL) {status}{reviewer}")
    if b.rejection_reason:
        print(f"    Reason: {b.rejection_reason}")
    if b.safety_flags:
        print(f"    Safety flags disclosed to parent: {len(b.safety_flags)}")
