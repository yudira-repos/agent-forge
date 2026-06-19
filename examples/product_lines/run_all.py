"""
AgentForge — Multi-Product-Line Demo
======================================
Runs all three product lines concurrently against a single shared
HITLRegistry, demonstrating how human-in-the-loop approval flows
elegantly across entirely different domains.

    cd <repo-root> && python examples/product_lines/run_all.py

What to observe
───────────────
1. ALL three product lines share ONE HITLRegistry instance.
2. Each product line has its own escalation policy, notification channel,
   reviewer group, and context renderer — but the same underlying
   HITLOrchestrator primitives.
3. HITL triggers are domain-appropriate:
     Health     : medication risk + all specialist referrals
     Investment : every trade > $10k + dual-control execution gate
     Kids       : every booking (COPPA — parental consent always required)
4. Notifications look completely different per domain because each product
   line registered its own ContextRenderer.
5. Auto-reject on timeout across all domains — no silent approvals.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

# Ensure the examples/ directory is on sys.path so `product_lines` is importable
# regardless of where the script is invoked from.
_here = os.path.dirname(os.path.abspath(__file__))
_examples_dir = os.path.dirname(_here)
if _examples_dir not in sys.path:
    sys.path.insert(0, _examples_dir)

from product_lines.health.workflow import PatientCase, run_health_workflow  # noqa: E402
from product_lines.investment.workflow import Portfolio, TradeOrder, TradeType, run_investment_workflow  # noqa: E402
from product_lines.kids.workflow import Child, run_kids_workflow  # noqa: E402
from product_lines.shared.hitl_registry import HITLRegistry, ProductLine  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Sample domain data
# ─────────────────────────────────────────────────────────────────────────────

def _health_cases() -> list[tuple[PatientCase, bool]]:
    """Returns (case, clinician_approves) pairs."""
    return [
        (
            PatientCase(
                patient_id="P-4421",
                age=34,
                symptoms=["severe pain", "persistent cough"],
                medical_history=["hypertension"],
                allergies=["penicillin"],
            ),
            True,   # clinician approves
        ),
        (
            PatientCase(
                patient_id="P-8893",
                age=67,
                symptoms=["chest pain", "difficulty breathing"],
                medical_history=["diabetes", "COPD"],
                allergies=[],
            ),
            True,   # clinician approves emergency case
        ),
    ]


def _investment_scenarios() -> list[tuple[Portfolio, TradeOrder, bool]]:
    """Returns (portfolio, trade, advisor_approves) triples."""
    portfolio = Portfolio(
        account_id="ACC-7712",
        total_value_usd=250_000.0,
        holdings={
            "AAPL": 80_000.0,
            "MSFT": 60_000.0,
            "BND":  50_000.0,
            "VTI":  60_000.0,
        },
    )
    return [
        (
            portfolio,
            TradeOrder(
                trade_type=TradeType.BUY,
                ticker="BND",
                amount_usd=25_000.0,
                rationale="Rebalance equity-heavy portfolio toward fixed income target",
            ),
            True,   # advisor approves
        ),
        (
            portfolio,
            TradeOrder(
                trade_type=TradeType.SELL,
                ticker="AAPL",
                amount_usd=35_000.0,
                rationale="Trim overweight position — exceeds IPS limit",
            ),
            False,  # advisor rejects (too large a cut)
        ),
    ]


def _kids_scenarios() -> list[tuple[Child, list[str], int, bool]]:
    """Returns (child, preferences, activity_index, parent_approves) tuples."""
    return [
        (
            Child(
                child_id="KID-001",
                name="Alex",
                age=9,
                allergies=[],
                existing_schedule=["Monday", "Wednesday"],
            ),
            ["sports", "outdoor"],
            0,   # Soccer League
            True,
        ),
        (
            Child(
                child_id="KID-002",
                name="Jordan",
                age=11,
                allergies=["latex"],
                existing_schedule=["Saturday"],  # conflict with rock climbing
            ),
            ["technology", "adventure"],
            2,   # Rock Climbing (has safety flags + schedule conflict)
            True,   # parent still approves after seeing flags
        ),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-product dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _print_dashboard(
    health_results: list[dict[str, Any]],
    investment_results: list[dict[str, Any]],
    kids_results: list[dict[str, Any]],
    hitl_registry: HITLRegistry,
    elapsed_sec: float,
) -> None:
    print(f"\n\n{'★'*60}")
    print(f"  AGENTFORGE — CROSS-PRODUCT HITL DASHBOARD")
    print(f"{'★'*60}")

    print(f"\n  ⏱  Total elapsed: {elapsed_sec:.2f}s")
    print(f"  📬 Total HITL notifications sent: {len(hitl_registry.notifications)}")

    # Notification breakdown by product line
    by_pl: dict[str, int] = {}
    for n in hitl_registry.notifications:
        pl = n["product_line"]
        by_pl[pl] = by_pl.get(pl, 0) + 1
    for pl, count in sorted(by_pl.items()):
        print(f"     {pl.upper():<12}: {count} notification(s)")

    print(f"\n  ──── HEALTH ────────────────────────────────")
    for r in health_results:
        med_status = "✓" if r.get("medication_approved") else "✗"
        ref_status = "✓" if r.get("referral_approved") else "✗"
        print(
            f"  Patient {r['patient_id']} | Risk: {r['risk_level'].upper():8} "
            f"| Med: {med_status} | Referral: {ref_status} "
            f"| Audit: {r.get('audit_events', 0)} events"
        )

    print(f"\n  ──── INVESTMENT ────────────────────────────")
    for r in investment_results:
        exec_status = "EXECUTED ✓" if r.get("trade_executed") else "BLOCKED ✗ "
        print(
            f"  Acct {r['account_id']} | "
            f"{r['trade']['type'].upper()} ${r['trade']['amount_usd']:>8,.0f} {r['trade']['ticker']:5} "
            f"| Risk: {r['risk_band']:12} | {exec_status} | Audit: {r.get('audit_events', 0)} events"
        )

    print(f"\n  ──── KIDS ACTIVITIES ───────────────────────")
    for r in kids_results:
        booked = "BOOKED ✓" if r.get("booked") else "NOT BOOKED ✗"
        flags = f" | ⚠ {len(r.get('safety_flags', []))} flag(s)" if r.get("safety_flags") else ""
        conflict = " | ⚠ schedule conflict" if r.get("schedule_conflict") else ""
        print(
            f"  {r.get('child'):6} | {r.get('activity', 'N/A'):30} | "
            f"{booked}{flags}{conflict}"
        )

    print(f"\n  ──── HITL ARCHITECTURE SUMMARY ─────────────")
    print("  All 3 product lines share ONE HITLRegistry.")
    print("  Each has its own:")
    print("    • Escalation policy  (clinical / trade-compliance / parental-consent)")
    print("    • Notification channel (slack / slack / sms+push)")
    print("    • Context renderer   (clinical / financial / parent-friendly)")
    print("    • Reviewer groups    (physicians / advisors / parents)")
    print("  Adding a 4th product line = 1 registry.register() call.")
    print(f"\n{'★'*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("\n" + "╔" + "═"*58 + "╗")
    print("║   AgentForge — Multi-Product-Line HITL Demo              ║")
    print("╚" + "═"*58 + "╝")
    print("\n  Creating shared HITLRegistry for all product lines…")

    # ONE registry, shared across all product lines
    hitl_registry = HITLRegistry.default()

    print("  ✓ Registry configured:")
    print("    • health     → clinical-review escalation → slack:#clinical-approvals")
    print("    • investment → trade-compliance escalation → slack:#trade-approvals")
    print("    • kids       → parental-consent escalation → sms+push")

    start = time.perf_counter()

    # Run all three product lines (interleaved via asyncio)
    health_tasks = [
        run_health_workflow(case, hitl_registry, approves)
        for case, approves in _health_cases()
    ]
    investment_tasks = [
        run_investment_workflow(portfolio, trade, hitl_registry, approves)
        for portfolio, trade, approves in _investment_scenarios()
    ]
    kids_tasks = [
        run_kids_workflow(child, prefs, idx, hitl_registry, approves)
        for child, prefs, idx, approves in _kids_scenarios()
    ]

    # Run all sequentially within each product line for readable output,
    # but all three product lines interleave naturally via asyncio.
    all_results = await asyncio.gather(
        *health_tasks,
        *investment_tasks,
        *kids_tasks,
    )

    elapsed = time.perf_counter() - start

    health_results     = list(all_results[:len(health_tasks)])
    investment_results = list(all_results[len(health_tasks):len(health_tasks)+len(investment_tasks)])
    kids_results       = list(all_results[len(health_tasks)+len(investment_tasks):])

    _print_dashboard(
        health_results,
        investment_results,
        kids_results,
        hitl_registry,
        elapsed,
    )


if __name__ == "__main__":
    import sys
    import os
    # Add examples/ to path so `product_lines` package is importable
    _examples_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _examples_dir not in sys.path:
        sys.path.insert(0, _examples_dir)
    asyncio.run(main())
