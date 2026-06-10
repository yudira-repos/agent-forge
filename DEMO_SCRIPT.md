# AgentForge Demo Script
### Engineering Team Walkthrough — ~15 minutes

---

## What you're showing

AgentForge is an enterprise agent framework with three pillars:

1. **Identity & governance** — every agent has a manifest; policy enforces who can do what
2. **Multi-agent orchestration** — real LLM calls (Claude + OpenAI) in a LangGraph DAG, OTEL spans to Honeycomb
3. **Agent-level HITL** — when an AI is uncertain mid-execution, it pauses itself, preserves session state, waits for a human, then resumes from the exact same point

---

## Setup (before the room fills)

- Open three browser tabs:
  - Tab 1: `https://<your-railway-url>/orchestration`
  - Tab 2: `https://<your-railway-url>/escalation`
  - Tab 3: `https://<your-railway-url>/workflow`
- Have Honeycomb open in a fourth tab (optional — for the OTEL moment)
- Check health: `https://<your-railway-url>/health` — should show `"status":"ok"`

---

## Act 1 — Architecture walk (~3 min)

**Open Tab 3 (Workflow Designer). Point at the canvas.**

> "This is the workflow designer. Each node is a real AI agent — not a mock.
> The blue ones call Claude via the Anthropic API. The teal ones call GPT-4o-mini.
> The graph is compiled at runtime into a LangGraph StateGraph — that gives us
> checkpoint/resume built-in, not something we had to build."

**Click the `wf-demo-001` workflow — 'Support Ticket AI Triage'.**

> "Four nodes: Trigger injects a support ticket, Claude classifies it, OpenAI drafts
> the response, Event emits the result. No HITL here — this is the happy path."

**Point at node configs.**

> "Each node has an Agent ID that maps to a manifest in the registry — model, system
> prompt, cost per token. The workflow designer doesn't know or care which LLM is
> behind each agent. It just knows the Agent ID."

---

## Act 2 — End-to-end execution (~4 min)

**Stay on the Workflow Designer. Click the Execute button for `wf-demo-001`.**

> "Executing now. Real calls — no mocks."

**Switch to Tab 1 (Orchestration). Watch the run appear.**

> "The orchestration dashboard is live SSE — no polling. You can see the run status,
> which node is executing, latency per step, tokens, cost."

**Wait for completion (~5-10 seconds). Click into the run.**

> "Claude classified the ticket as billing, high severity. OpenAI drafted the
> customer response. Total: two LLM calls, one workflow, end-to-end in under 10 seconds."

**(Optional) Switch to Honeycomb.**

> "Every span ships to Honeycomb via OTLP. You can trace any agent call — model,
> latency, tokens in/out, cost, error. This is the observability story for prod."

---

## Act 3 — Agent-level HITL (the main event) (~6 min)

**Switch to Tab 3. Click `wf-escalation-demo-001` — 'Agent Escalation: Human-in-the-Loop Classification'.**

> "This is the scenario that's hard to solve cleanly. A customer ticket comes in
> that's genuinely ambiguous — billing dispute AND permissions error AND app crash.
> The OpenAI classifier can't assign a category with confidence above 70%.
> What should it do?"

**Point at the flow: Trigger → OpenAI Classifier → Claude Responder → Event.**

> "The pattern we implemented: the agent signals escalation in its JSON output —
> `escalate: true`, reason, confidence, candidate categories. That's it.
> The agent doesn't know about the HITL system. It just returns JSON."

**Execute the workflow.**

> "Executing the ambiguous ticket now."

**Switch to Tab 1 (Orchestration). Watch the run status change to `waiting_agent_escalation`.**

> "The run is paused. LangGraph called `interrupt()` which serialized the entire
> graph state to the checkpointer — Postgres on Railway, MemorySaver locally.
> The agent's identity, session, conversation history — all preserved. Nothing lost."

**Switch to Tab 2 (Escalation Review).**

> "This is the review queue. Any human on the team can come here. Look at what
> they see:"

**Click the pending escalation.**

> "The agent's reasoning — the full system prompt, the ticket, the LLM's response
> before it escalated. Confidence was 42%. It identified three candidates: billing,
> technical, account.
>
> The human can read the exact conversation the agent had with itself.
> This is session reconstruction — we store the conversation at escalation time
> so the reviewer has full context."

**Click 'billing' chip, add a note: "Charge discrepancy is the blocker — route to billing team", set reviewer ID to your name.**

**Click "Resolve & Resume Workflow".**

**Switch to Tab 1 (Orchestration). Watch the run resume.**

> "The workflow continued from exactly where it stopped. LangGraph resumed from
> the `interrupt()` call site — not from the top of the function, not from the
> start of the node. The human's decision was injected into the context as
> `_human_resolution`. Claude Responder now sees the human-assigned category
> and drafts the final response."

**Wait for completion. Click into the completed run to show the final output.**

> "Claude drafted a response using the human-assigned category. It knows a human
> reviewed this — the prompt tells it 'a reviewer assigned billing, do not escalate again.'
> The escalation note in the output is preserved for the audit trail."

---

## Act 4 — Why this matters (~2 min)

> "Three things that are usually hard that this demo shows working:
>
> **1. Agent identity.** Every agent has a manifest — model, version, owner, cost
> per token, capability schema. You can swap models without touching the workflow.
>
> **2. Mid-execution pause.** This isn't workflow-level HITL — a gate before or after
> a node. This is the agent itself deciding it needs human input, mid-execution,
> preserving its own session state. That's a different class of problem.
>
> **3. Observability.** Every agent call is a span. You get latency, tokens, cost,
> error rate per agent — not just per workflow. That's what you need to run this in prod."

---

## If things go wrong

| Problem | Recovery |
|---|---|
| OpenAI 429 | Switch to `wf-demo-001` (Claude only). Explain OpenAI quota — the arch still stands |
| Escalation doesn't appear | Refresh `/escalation` — it also auto-polls every 15s |
| Run shows `failed` | Click into the run, read the error. Usually a missing env var |
| Honeycomb shows no data | Confirm `OTEL_EXPORTER_OTLP_HEADERS` is set in Railway Variables |

---

## Key URLs

```
/ ..................... Dashboard (agent registry, audit trail, HITL approvals)
/workflow ............. Workflow Designer
/orchestration ........ Live runs + OTEL spans
/escalation ........... Agent escalation review queue

/api/escalations ............... List pending escalations (JSON)
/health ........................ Health + OTEL status
/api/agents .................... Agent registry
/api/workflows/runs ............ All workflow runs
```

---

## The one-sentence summary

> "AgentForge gives you the three things enterprises need before they can trust an AI agent in production: **identity** so you know which model did what, **governance** so humans stay in the loop when the AI is uncertain, and **observability** so you can see exactly what happened and what it cost."
