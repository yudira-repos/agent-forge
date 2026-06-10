# Proving HITL is Real — Engineer Demo Guide

> Engineers will ask: "Is the workflow actually paused or is this just a UI trick?"
> Here's exactly what to show to prove it's real at every level.

---

## The two HITL patterns

| | Workflow HITL | Agent HITL |
|---|---|---|
| **Who decides to pause** | Workflow designer (puts a HITL node in the graph) | The AI agent itself (LLM returns `escalate: true`) |
| **Why it pauses** | Business rule — "all payments > $10k need approval" | AI uncertainty — "confidence 42%, I shouldn't guess" |
| **Demo workflow** | `wf-hitl-approval-001` | `wf-escalation-demo-001` |
| **Paused status** | `waiting_hitl` | `waiting_agent_escalation` |
| **Resume via** | `/orchestration` → Approve/Reject buttons | `/escalation` → pick category → Resolve |

---

## Demo A — Workflow HITL: the approval gate

### Setup
Use workflow **"Invoice Payment Approval (Workflow HITL)"** — `wf-hitl-approval-001`

Trigger injects a $24,500 invoice. Policy says anything over $10k needs a human sign-off before the payment instruction is generated.

### Steps

**1. Execute the workflow**
Click Execute. Watch it run in Orchestration — the first node (Claude extraction) completes, then the run **freezes** at the HITL node.

**2. The "is it real?" proof — open a new incognito window**
```
GET /api/workflows/runs/{run_id}
```
Show engineers this JSON:
```json
{
  "run_id": "run-xxxxxxxx",
  "status": "waiting_hitl",
  "current_node_id": "hitl-approve",
  ...
}
```
> "The HTTP API says `waiting_hitl`. This is not a UI state. The LangGraph graph
> is serialized to Postgres. If I kill this server right now and restart it,
> this run will still be here, still paused, still waiting."

**3. Show it persists across browser tabs**
Open the Orchestration page on your phone (or have someone else open it on their laptop). The run is there. The approve/reject buttons work from any device.

**4. Approve it**
Click Approve in the HITL panel. Add your name as reviewer. Watch the run continue — the payment generator node fires, completes.

**5. Show the audit trail**
```
GET /api/workflows/runs/{run_id}/audit
```
The audit log shows:
- `AGENT_STARTED` → extraction node
- `HITL_REQUESTED` → pause
- `HITL_APPROVED` → your name, timestamp
- `TOOL_COMPLETED` → payment node completed

> "Every decision is immutable. You can't edit the audit log — it's append-only.
> For SOC2 this is table stakes."

---

## Demo B — Agent HITL: the AI escalates itself

### Setup
Use workflow **"Agent Escalation: Human-in-the-Loop Classification"** — `wf-escalation-demo-001`

The ticket is designed to be genuinely ambiguous: billing dispute + permissions error + app crash.

### Steps

**1. Execute the workflow**
The OpenAI classifier runs, hits 42% confidence, returns this raw JSON:
```json
{
  "escalate": true,
  "reason": "Ticket spans billing, technical, and account categories — cannot classify with required 70% confidence",
  "confidence": 0.42,
  "candidates": ["billing", "technical", "account"]
}
```

**2. Show engineers the raw LLM output**
Go to:
```
GET /api/traces/{run_id}
```
Find the span for `openai-ambiguous-classifier`. Show the error/output. 

> "The LLM itself decided to escalate. We didn't hardcode this — the system prompt
> tells the model: if confidence < 70%, return escalate:true. The agent framework
> detects that field and calls LangGraph's `interrupt()`."

**3. Show the escalation in the DB**
```
GET /api/escalations
```
```json
[{
  "escalation_id": "a3f8b2c1d4e5f6a7",
  "agent_name": "Ambiguous Ticket Classifier (OpenAI)",
  "reason": "Ticket spans billing, technical, and account categories...",
  "confidence": 0.42,
  "candidates": ["billing", "technical", "account"],
  "status": "pending"
}]
```
> "This is in SQLite/Postgres. The workflow run is checkpointed in LangGraph's
> checkpointer table. Open your phone — go to `/escalation`. It's there."

**4. Show the session replay**
Open `/escalation` and click the pending item. Show:
- The **system prompt** the agent was running with
- The **ticket text** it received
- The **LLM's response** before it escalated

> "We preserved the agent's session at the moment it escalated. When the human
> resolves this, the agent re-runs with the human's decision injected — but it
> gets the same system prompt, the same context, plus `_human_resolution`.
> This is session reconstruction."

**5. Resolve it**
Pick "billing", add a note, click Resolve. Switch to Orchestration — the run resumes. Claude Responder fires, drafts the response knowing a human assigned the billing category.

**6. The smoking gun — two LLM calls for one node**
```
GET /api/traces/{run_id}
```
Look at the spans. The `openai-ambiguous-classifier` node has **one span** (the escalation call). The `claude-escalation-responder` has **one span**. 

> "Two agents, two LLM calls, one human decision in the middle. The human's
> decision is part of the execution graph — not a workaround around it."

---

## The three questions engineers will ask

**Q: "What happens if the server restarts while a workflow is paused?"**

> "Workflow HITL uses Postgres via LangGraph's AsyncPostgresSaver. Agent HITL
> persists the escalation record to SQLite/Postgres before calling `interrupt()`.
> Restart the Railway service right now — both run states survive."
> 
> *(On Railway: trigger a manual deploy while a run is paused. It comes back paused.)*

**Q: "What if the human never resolves it?"**

> "The run sits at `waiting_hitl` / `waiting_agent_escalation` indefinitely.
> You can build a timeout policy — the HITL orchestrator supports timeout_seconds.
> For this demo it's set to 1 hour. In prod you'd page the on-call engineer via
> the Slack integration we have wired."

**Q: "Is this LangGraph-specific? What if we don't want LangGraph?"**

> "The custom DAG walker (the fallback engine) also supports workflow HITL via
> a polling loop against SQLite — same API, no LangGraph dependency.
> Agent HITL is engine-agnostic — it's just the agent runner returning a special
> NodeResult and the engine detecting it. You could plug in Temporal or Prefect
> and the agent layer wouldn't change."

---

## Cheat sheet — URLs to have open

```
/orchestration                          Live run status + OTEL spans
/escalation                             Agent escalation review queue
/api/workflows/runs/{run_id}            Run state JSON (the real proof)
/api/escalations                        Escalation queue JSON
/api/escalations/{id}                   Full session replay JSON
/api/traces/{run_id}                    OTEL spans per run
```
