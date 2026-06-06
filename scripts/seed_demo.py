"""
AgentForge — Demo Data Seed Script
====================================
Loads realistic LifeOS demo data into a running AgentForge instance.

Usage:
    # Against local dev server (python)
    python3 scripts/seed_demo.py

    # Against Docker
    python3 scripts/seed_demo.py --url http://localhost

    # Against Railway / any deployed instance
    python3 scripts/seed_demo.py --url https://your-app.railway.app
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://localhost:8000", help="Base URL of AgentForge API")
args = parser.parse_args()
BASE = args.url.rstrip("/")

def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  ⚠  {path} → {e.code}: {err[:120]}")
        return None

def get(path):
    req = urllib.request.Request(f"{BASE}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ⚠  GET {path} → {e}")
        return None

def check_server():
    result = get("/health")
    if not result:
        print(f"\n❌  Cannot reach AgentForge at {BASE}")
        print("    Make sure the server is running:\n")
        print("    Local:  python3 ui/backend/main.py")
        print("    Docker: docker compose -f deploy/docker/docker-compose.yml up\n")
        sys.exit(1)
    print(f"✓  Connected to AgentForge {result.get('version','?')} at {BASE}\n")

# ── Seed data ─────────────────────────────────────────────────────────────────
AGENTS = [
    {
        "agent_id": "health-agent-001",
        "name": "Health Tracker",
        "version": "2.1.0",
        "description": "Tracks vitals, sleep, HRV and nutrition from wearables (Oura, Apple Health)",
        "owner": "lifeos-core",
        "runtime_adapter": "anthropic",
        "tags": ["health", "wearables"],
    },
    {
        "agent_id": "finance-agent-001",
        "name": "Finance Manager",
        "version": "1.8.0",
        "description": "Tracks spending, categorizes transactions, monitors budgets and investments",
        "owner": "lifeos-core",
        "runtime_adapter": "openai",
        "tags": ["finance", "banking"],
    },
    {
        "agent_id": "goals-agent-001",
        "name": "Goals Coach",
        "version": "1.2.0",
        "description": "Tracks OKRs, daily habits, and quarterly goals. Sends nudges.",
        "owner": "lifeos-core",
        "runtime_adapter": "anthropic",
        "tags": ["goals", "productivity"],
    },
    {
        "agent_id": "journal-agent-001",
        "name": "Journal Analyst",
        "version": "1.0.0",
        "description": "Analyzes journal entries for mood patterns and recurring themes",
        "owner": "lifeos-core",
        "runtime_adapter": "anthropic",
        "tags": ["journal", "wellbeing"],
    },
    {
        "agent_id": "scheduler-agent-001",
        "name": "Smart Scheduler",
        "version": "0.9.0",
        "description": "Manages calendar, meeting prep, and travel logistics",
        "owner": "lifeos-core",
        "runtime_adapter": "vertex",
        "tags": ["calendar", "scheduling"],
    },
]

HITL_REQUESTS = [
    {
        "agent_id": "finance-agent-001",
        "correlation_id": "run-mon-001",
        "action": "transfer_funds",
        "resource": "bank-account",
        "context": {
            "amount_usd": 4200,
            "payee": "Bay Area Properties LLC",
            "memo": "June rent",
            "flagged_reason": "Amount exceeds $500 threshold",
        },
    },
    {
        "agent_id": "scheduler-agent-001",
        "correlation_id": "run-wed-001",
        "action": "book_appointment",
        "resource": "google-calendar",
        "context": {
            "title": "Dentist Checkup",
            "time": "Fri Jun 7 2:00 PM",
            "duration_mins": 60,
            "location": "Dr. Smith Dental, 123 Main St",
        },
    },
    {
        "agent_id": "health-agent-001",
        "correlation_id": "run-thu-001",
        "action": "delete_health_record",
        "resource": "health-records",
        "context": {
            "record_type": "cardiac_data",
            "date_range": "2022-01-01 to 2023-12-31",
            "reason": "Agent requested cleanup of old records",
        },
    },
]

AUDIT_EVENTS = [
    # Monday morning run
    ("agent.started",    "health-agent-001",   "run-mon-001", "info",    {"trigger": "morning_routine"}),
    ("tool.invoked",     "health-agent-001",   "run-mon-001", "info",    {"tool": "log_vitals", "heart_rate": 68, "sleep_hours": 7.4, "hrv": 61}),
    ("tool.completed",   "health-agent-001",   "run-mon-001", "info",    {"tool": "log_vitals", "duration_ms": 312}),
    ("agent.started",    "finance-agent-001",  "run-mon-001", "info",    {"trigger": "morning_routine"}),
    ("tool.invoked",     "finance-agent-001",  "run-mon-001", "info",    {"tool": "categorize_transaction", "merchant": "Whole Foods", "amount": 67.43}),
    ("tool.completed",   "finance-agent-001",  "run-mon-001", "info",    {"tool": "categorize_transaction", "category": "Groceries", "duration_ms": 180}),
    ("policy.evaluated", "finance-agent-001",  "run-mon-001", "warning", {"action": "transfer_funds", "amount": 4200, "effect": "require_hitl"}),
    ("hitl.requested",   "finance-agent-001",  "run-mon-001", "warning", {"action": "transfer_funds", "amount": 4200, "payee": "Bay Area Properties LLC"}),
    ("policy.denied",    "finance-agent-001",  "run-mon-001", "error",   {"attempted": "delete health records", "reason": "finance-agent lacks permission"}),
    # Goals agent
    ("agent.started",    "goals-agent-001",    "run-mon-001", "info",    {"trigger": "morning_routine"}),
    ("tool.invoked",     "goals-agent-001",    "run-mon-001", "info",    {"tool": "suggest_priorities"}),
    ("tool.completed",   "goals-agent-001",    "run-mon-001", "info",    {"tool": "suggest_priorities", "priorities": ["Finish Q3 review", "30min walk", "Call dentist"]}),
    # Tuesday evening
    ("agent.started",    "journal-agent-001",  "run-tue-001", "info",    {"trigger": "evening_reflection"}),
    ("tool.invoked",     "journal-agent-001",  "run-tue-001", "info",    {"tool": "analyze_mood"}),
    ("tool.completed",   "journal-agent-001",  "run-tue-001", "info",    {"tool": "analyze_mood", "mood_score": 7.2, "theme": "productive but stressed"}),
    # Wednesday calendar
    ("agent.started",    "scheduler-agent-001","run-wed-001", "info",    {"trigger": "calendar_sync"}),
    ("hitl.requested",   "scheduler-agent-001","run-wed-001", "warning", {"action": "book_appointment", "title": "Dentist Checkup", "time": "Fri Jun 7 2:00 PM"}),
    # Thursday health check
    ("agent.started",    "health-agent-001",   "run-thu-001", "info",    {"trigger": "weekly_cleanup"}),
    ("hitl.requested",   "health-agent-001",   "run-thu-001", "warning", {"action": "delete_health_record", "record_type": "cardiac_data", "years": 2}),
    # Credential events
    ("credential.issued","finance-agent-001",  "run-mon-001", "info",    {"scopes": ["transactions:read", "payments:initiate"], "ttl": 300}),
    ("agent.registered", "health-agent-001",   "setup-001",   "info",    {"version": "2.1.0", "capabilities": 3}),
]


# ── Runner ────────────────────────────────────────────────────────────────────
def seed():
    check_server()

    # 1. Register agents
    print("── Registering agents ────────────────────────────────")
    for agent in AGENTS:
        result = post("/api/agents", agent)
        status = "✓" if result else "⚠ (may already exist)"
        print(f"  {status}  {agent['name']} ({agent['agent_id']})")

    time.sleep(0.5)

    # 2. Seed audit events via /api/dev/seed
    print("\n── Seeding audit events ──────────────────────────────")
    result = post("/api/dev/seed-events", {"events": [
        {
            "event_type": e[0],
            "agent_id": e[1],
            "correlation_id": e[2],
            "severity": e[3],
            "payload": e[4],
        }
        for e in AUDIT_EVENTS
    ]})
    if result:
        print(f"  ✓  {result.get('created', len(AUDIT_EVENTS))} audit events created")
    else:
        print("  ⚠  Audit events endpoint not available — events will appear as agents run")

    time.sleep(0.5)

    # 3. Seed HITL requests via /api/dev/seed
    print("\n── Creating HITL approval requests ───────────────────")
    result = post("/api/dev/seed-hitl", {"requests": HITL_REQUESTS})
    if result:
        print(f"  ✓  {result.get('created', len(HITL_REQUESTS))} HITL requests pending")
    else:
        print("  ⚠  HITL seed endpoint not available")

    time.sleep(0.3)

    # 4. Verify
    print("\n── Verification ──────────────────────────────────────")
    dashboard = get("/api/dashboard")
    if dashboard:
        print(f"  ✓  Agents:        {dashboard['agents']['total']}")
        print(f"  ✓  HITL pending:  {dashboard['hitl_queue_depth']}")
        print(f"  ✓  Audit events:  {dashboard['audit']['total_events']}")
        print(f"  ✓  Violations:    {dashboard['audit']['violations']}")

    print(f"\n🎉  Demo data loaded. Open {BASE} to see it.\n")


if __name__ == "__main__":
    seed()
