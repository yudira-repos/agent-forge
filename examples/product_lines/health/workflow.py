"""
Health Workflow — Clinical Decision Support
============================================
Product line: HEALTH

Agents in this workflow:

  1. SymptomTriageAgent      — classifies symptom severity (always runs autonomously)
  2. MedicationAgent         — suggests medication + dosage  (HITL if risk ≥ medium OR
                               controlled substance)
  3. SpecialistReferralAgent — recommends specialist referral (HITL always)
  4. ClinicalDecisionAgent   — final treatment plan          (HITL always)

Compliance:  HIPAA profile (from AgentForge governance)
Escalation:  clinical-review policy (attending → dept-head → CMO)
Channel:     slack:#clinical-approvals

HITL contract
─────────────
Every HITL call goes through the shared HITLRegistry — agents only know the
ProductLine and the structured context dict.  The registry owns all routing,
escalation, and notification logic.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from enum import Enum
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

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


CONTROLLED_SUBSTANCES = {
    "oxycodone", "hydrocodone", "alprazolam", "diazepam",
    "methylphenidate", "amphetamine", "fentanyl", "morphine",
}


@dataclass
class PatientCase:
    patient_id: str
    age: int
    symptoms: list[str]
    medical_history: list[str]
    allergies: list[str]


@dataclass
class ClinicalDecision:
    action: str
    details: dict[str, Any]
    risk_level: RiskLevel
    requires_hitl: bool
    approved: bool = False
    reviewer_id: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# HIPAA governance profile
# ─────────────────────────────────────────────────────────────────────────────

def _hipaa_engine() -> PolicyEngine:
    """
    Minimal HIPAA policy engine for the health product line.

    Rules:
    - Any write to EHR in production requires HITL.
    - Controlled substance prescriptions require HITL regardless of env.
    - Audit logging is mandatory for all PHI access.
    """
    engine = PolicyEngine()

    ehr_write = Policy(
        name="hipaa-ehr-write",
        description="HIPAA: EHR writes require human approval",
    )
    ehr_write.add_rule(PolicyRule(
        name="ehr-write-hitl",
        effect=PolicyEffect.REQUIRE_HITL,
        conditions=[
            lambda ctx: ctx.action in ("prescribe", "refer", "treat", "update_ehr"),
            lambda ctx: ctx.environment == "production",
        ],
    ))

    controlled_sub = Policy(
        name="hipaa-controlled-substance",
        description="HIPAA: Controlled substance prescriptions always require HITL",
    )
    controlled_sub.add_rule(PolicyRule(
        name="controlled-sub-hitl",
        effect=PolicyEffect.REQUIRE_HITL,
        conditions=[
            lambda ctx: ctx.metadata.get("is_controlled_substance", False),
        ],
    ))

    engine.add_policy(ehr_write)
    engine.add_policy(controlled_sub)
    return engine


# ─────────────────────────────────────────────────────────────────────────────
# Agents
# ─────────────────────────────────────────────────────────────────────────────

class SymptomTriageAgent:
    """
    Classifies symptom severity autonomously — no HITL needed.
    In production this calls an LLM; here we simulate the classification.
    """

    async def triage(self, case: PatientCase) -> tuple[RiskLevel, str]:
        await asyncio.sleep(0.02)  # simulate LLM call
        critical_symptoms = {"chest pain", "difficulty breathing", "stroke symptoms"}
        high_symptoms = {"severe pain", "high fever", "loss of consciousness"}
        medium_symptoms = {"persistent cough", "moderate pain", "rash", "dizziness"}

        symptom_set = {s.lower() for s in case.symptoms}
        if symptom_set & critical_symptoms:
            return RiskLevel.CRITICAL, "Emergency symptoms detected — immediate attention required"
        elif symptom_set & high_symptoms:
            return RiskLevel.HIGH, "High-risk symptoms — same-day clinical review required"
        elif symptom_set & medium_symptoms:
            return RiskLevel.MEDIUM, "Moderate symptoms — routine clinical evaluation needed"
        else:
            return RiskLevel.LOW, "Low-risk presentation — self-care guidance appropriate"


class MedicationAgent:
    """
    Suggests medication + dosage.

    HITL triggers:
    - Risk level ≥ MEDIUM
    - Medication is a controlled substance
    - Patient has a relevant allergy (blocks entirely)
    """

    async def suggest(
        self,
        case: PatientCase,
        risk_level: RiskLevel,
        hitl_registry: HITLRegistry,
        correlation_id: str,
        logger: AuditLogger,
        gov_engine: PolicyEngine,
    ) -> ClinicalDecision:
        await asyncio.sleep(0.02)

        # Simulate medication suggestion
        med_map = {
            "chest pain": ("nitroglycerin", "0.4mg sublingual"),
            "high fever": ("acetaminophen", "650mg q6h"),
            "persistent cough": ("dextromethorphan", "30mg q4h"),
            "moderate pain": ("ibuprofen", "400mg q8h"),
            "severe pain": ("oxycodone", "5mg q4h PRN"),   # controlled
        }
        medication, dosage = "acetaminophen", "500mg q6h"
        for symptom in case.symptoms:
            if symptom.lower() in med_map:
                medication, dosage = med_map[symptom.lower()]
                break

        is_controlled = medication.lower() in CONTROLLED_SUBSTANCES
        allergy_conflict = medication.lower() in [a.lower() for a in case.allergies]

        if allergy_conflict:
            return ClinicalDecision(
                action="prescribe_medication",
                details={"blocked_reason": f"Patient allergic to {medication}"},
                risk_level=RiskLevel.CRITICAL,
                requires_hitl=False,
                approved=False,
            )

        # Evaluate policy
        ctx = PolicyContext(
            agent_id="medication-agent",
            agent_roles=["clinical-agent"],
            action="prescribe",
            resource="ehr-system",
            environment="production",
            metadata={"is_controlled_substance": is_controlled},
        )
        gov_decision = gov_engine.evaluate(ctx)

        needs_hitl = (
            gov_decision.requires_hitl
            or risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
            or is_controlled
        )

        logger.log(EventType.POLICY_EVALUATED, "medication-agent", correlation_id,
                   payload={"effect": gov_decision.effect.value, "medication": medication})

        if needs_hitl:
            print(f"\n  [MedicationAgent] HITL required — {medication} ({risk_level.value} risk"
                  + (", controlled substance" if is_controlled else "") + ")")

            hitl_decision = await hitl_registry.request_approval(
                product_line=ProductLine.HEALTH,
                agent_id="medication-agent",
                correlation_id=correlation_id,
                action="prescribe_medication",
                resource="ehr-system",
                context={
                    "patient_id": case.patient_id,
                    "clinical_action": "prescribe_medication",
                    "medication": medication,
                    "dosage": dosage,
                    "risk_level": risk_level.value,
                    "is_controlled_substance": is_controlled,
                    "rationale": f"Indicated for: {', '.join(case.symptoms)}",
                },
                timeout_seconds=30,  # short for demo
            )
            return ClinicalDecision(
                action="prescribe_medication",
                details={"medication": medication, "dosage": dosage},
                risk_level=risk_level,
                requires_hitl=True,
                approved=hitl_decision.approved,
                reviewer_id=hitl_decision.reviewer_id,
            )

        return ClinicalDecision(
            action="prescribe_medication",
            details={"medication": medication, "dosage": dosage},
            risk_level=risk_level,
            requires_hitl=False,
            approved=True,
        )


class SpecialistReferralAgent:
    """
    Recommends specialist referral.  Always requires HITL — a clinician
    must confirm before routing the patient.
    """

    SPECIALTY_MAP = {
        RiskLevel.CRITICAL: "emergency-medicine",
        RiskLevel.HIGH:     "internal-medicine",
        RiskLevel.MEDIUM:   "general-practitioner",
        RiskLevel.LOW:      None,   # no referral needed
    }

    async def refer(
        self,
        case: PatientCase,
        risk_level: RiskLevel,
        hitl_registry: HITLRegistry,
        correlation_id: str,
        logger: AuditLogger,
    ) -> ClinicalDecision:
        await asyncio.sleep(0.02)

        specialty = self.SPECIALTY_MAP.get(risk_level)
        if specialty is None:
            return ClinicalDecision(
                action="specialist_referral",
                details={"referral": "none_required"},
                risk_level=risk_level,
                requires_hitl=False,
                approved=True,
            )

        print(f"\n  [SpecialistReferralAgent] HITL required — referral to {specialty}")

        hitl_decision = await hitl_registry.request_approval(
            product_line=ProductLine.HEALTH,
            agent_id="referral-agent",
            correlation_id=correlation_id,
            action="specialist_referral",
            resource="referral-system",
            context={
                "patient_id": case.patient_id,
                "clinical_action": "specialist_referral",
                "referral_specialty": specialty,
                "risk_level": risk_level.value,
                "rationale": f"Risk level {risk_level.value} warrants {specialty} evaluation",
            },
            timeout_seconds=30,
        )

        logger.log(
            EventType.HITL_APPROVED if hitl_decision.approved else EventType.HITL_REJECTED,
            "referral-agent", correlation_id,
            payload={"specialty": specialty, "reviewer": hitl_decision.reviewer_id},
        )

        return ClinicalDecision(
            action="specialist_referral",
            details={"specialty": specialty},
            risk_level=risk_level,
            requires_hitl=True,
            approved=hitl_decision.approved,
            reviewer_id=hitl_decision.reviewer_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Workflow orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def _simulate_clinical_approval(
    hitl_registry: HITLRegistry,
    approve: bool = True,
    delay: float = 0.15,
) -> None:
    """Simulates a clinician responding to ALL pending/escalated health approvals."""
    await asyncio.sleep(delay)
    orchestrator = hitl_registry._orchestrators.get(ProductLine.HEALTH)
    if not orchestrator:
        return
    # Use _pending directly so we catch ESCALATED requests too (the monitor
    # may have escalated a request before the simulated reviewer fires).
    for req in list(orchestrator._pending.values()):
        if not req.is_resolved:
            await orchestrator.decide(ApprovalDecision(
                request_id=req.request_id,
                reviewer_id="dr.chen@hospital.org",
                approved=approve,
                reason="Clinically appropriate — approved." if approve else "Not indicated at this time.",
            ))


async def run_health_workflow(
    case: PatientCase,
    hitl_registry: HITLRegistry,
    clinician_approves: bool | None = None,  # None = real HITL; True/False = simulate
) -> dict[str, Any]:
    """
    Run a full clinical decision workflow for one patient case.

    Returns a summary dict suitable for audit / dashboard display.
    """
    correlation_id = f"health-run-{uuid.uuid4().hex[:8]}"
    sink = InMemoryAuditSink()
    logger = AuditLogger(sinks=[sink])
    trail = AuditTrail(sink)
    gov_engine = _hipaa_engine()

    print(f"\n{'═'*60}")
    print(f"  HEALTH WORKFLOW  |  Patient: {case.patient_id}")
    print(f"  Symptoms: {', '.join(case.symptoms)}")
    print(f"{'═'*60}")

    logger.log(EventType.AGENT_STARTED, "health-workflow", correlation_id,
               payload={"patient_id": case.patient_id})

    # Step 1: Triage (autonomous — no HITL)
    triage = SymptomTriageAgent()
    risk_level, triage_summary = await triage.triage(case)
    print(f"\n  [SymptomTriage] Risk: {risk_level.value.upper()} — {triage_summary}")
    logger.log(EventType.TOOL_COMPLETED, "triage-agent", correlation_id,
               payload={"risk_level": risk_level.value})

    # Start simulating clinician approvals in background (only in simulation mode)
    approval_sim = (
        asyncio.create_task(_simulate_clinical_approval(hitl_registry, approve=clinician_approves))
        if clinician_approves is not None else None
    )

    # Step 2: Medication suggestion (HITL if risk ≥ medium or controlled)
    med_agent = MedicationAgent()
    med_decision = await med_agent.suggest(
        case, risk_level, hitl_registry, correlation_id, logger, gov_engine
    )
    _print_decision("Medication", med_decision)

    # Restart simulator for referral step (only in simulation mode)
    await asyncio.sleep(0.05)
    approval_sim2 = (
        asyncio.create_task(_simulate_clinical_approval(hitl_registry, approve=clinician_approves))
        if clinician_approves is not None else None
    )

    # Step 3: Specialist referral (HITL always, unless low-risk)
    ref_agent = SpecialistReferralAgent()
    ref_decision = await ref_agent.refer(
        case, risk_level, hitl_registry, correlation_id, logger
    )
    _print_decision("Referral", ref_decision)

    if approval_sim is not None:
        await approval_sim
    if approval_sim2 is not None:
        await approval_sim2

    # Audit trail
    steps = trail.replay(correlation_id)
    print(f"\n  [Audit] {len(steps)} events recorded for {correlation_id}")

    return {
        "correlation_id": correlation_id,
        "patient_id": case.patient_id,
        "risk_level": risk_level.value,
        "medication": med_decision.details,
        "medication_approved": med_decision.approved,
        "medication_reviewer": med_decision.reviewer_id,
        "referral": ref_decision.details,
        "referral_approved": ref_decision.approved,
        "referral_reviewer": ref_decision.reviewer_id,
        "audit_events": len(steps),
    }


def _print_decision(label: str, d: ClinicalDecision) -> None:
    status = "✓ APPROVED" if d.approved else "✗ REJECTED/BLOCKED"
    hitl_tag = " (HITL)" if d.requires_hitl else " (auto)"
    reviewer = f" by {d.reviewer_id}" if d.reviewer_id else ""
    print(f"\n  [{label}]{hitl_tag} {status}{reviewer}")
    print(f"    Details: {d.details}")
