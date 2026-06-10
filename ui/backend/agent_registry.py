"""
Global agent registry singleton for the AgentForge UI backend.

This module holds the single shared AgentRegistry instance that the API
server, agent runner, and metrics system all reference.

Pre-seeded agents
-----------------
Two runtime adapters are demonstrated out of the box:

  claude-invoice-extractor   → runtime_adapter="anthropic" (Claude Haiku)
  openai-risk-scorer         → runtime_adapter="openai"    (GPT-4o-mini)
  claude-payment-processor   → runtime_adapter="anthropic" (Claude Haiku)

Each manifest's ``metadata`` dict carries adapter-specific config that the
agent runner reads directly — model name, system prompt, output fields, etc.
This means the *workflow designer* only has to pick an Agent ID; the runtime
resolves all model/prompt details from the registry.

Registering a new agent at runtime
-----------------------------------
    from ui.backend.agent_registry import registry
    from agentforge.registry.models import AgentManifest, AgentCapability

    manifest = AgentManifest(
        agent_id="my-agent-001",
        name="My Custom Agent",
        version="1.0.0",
        description="Does something useful",
        owner="my-team",
        runtime_adapter="anthropic",      # or "openai"
        capabilities=[
            AgentCapability("my_capability", "What it does"),
        ],
        metadata={
            "model": "claude-haiku-4-5-20251001",
            "system_prompt": "You are ...",
            "output_fields": "field_a, field_b",
        },
    )
    registry.register(manifest)
"""

from __future__ import annotations

from agentforge.registry.models import AgentCapability, AgentManifest, AgentStatus
from agentforge.registry.registry import AgentRegistry

# ── Singleton ──────────────────────────────────────────────────────────────────

registry: AgentRegistry = AgentRegistry()


# ── Seed: Claude-managed agents ───────────────────────────────────────────────

registry.register(AgentManifest(
    agent_id="claude-invoice-extractor",
    name="Invoice Extractor (Claude)",
    version="2.1.0",
    description=(
        "Extracts structured fields from a raw invoice document. "
        "Returns vendor_id, amount_usd, currency, invoice_number, due_date, and line items."
    ),
    owner="finance-team",
    runtime_adapter="anthropic",
    tags=["finance", "document-processing", "extraction"],
    status=AgentStatus.ACTIVE,
    capabilities=[
        AgentCapability(
            name="extract_invoice",
            description="Parse a raw invoice payload and return structured fields",
            input_schema={
                "type": "object",
                "properties": {
                    "raw_invoice": {"type": "string"},
                    "source_system": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "vendor_id":       {"type": "string"},
                    "amount_usd":      {"type": "number"},
                    "currency":        {"type": "string"},
                    "invoice_number":  {"type": "string"},
                    "due_date":        {"type": "string"},
                    "items":           {"type": "array"},
                },
            },
            tags=["extraction", "finance"],
            idempotent=True,
        ),
    ],
    metadata={
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system_prompt": (
            "You are a precise invoice extraction agent. "
            "Extract vendor_id, amount_usd, currency, invoice_number, due_date, and items "
            "from the invoice data in the execution context. "
            "Return ONLY a valid JSON object with those fields — no explanation, no markdown."
        ),
        "output_fields": "vendor_id, amount_usd, currency, invoice_number, due_date, items",
        # Cost reference (USD per 1k tokens) — used by the metrics dashboard
        "cost_per_1k_input":  0.00025,
        "cost_per_1k_output": 0.00125,
    },
))


registry.register(AgentManifest(
    agent_id="claude-payment-processor",
    name="Payment Processor (Claude)",
    version="1.3.0",
    description=(
        "Generates structured payment instructions from an approved invoice. "
        "Returns payment_id, transaction_id, routing details, and confirmation."
    ),
    owner="payments-team",
    runtime_adapter="anthropic",
    tags=["finance", "payments", "outbound"],
    status=AgentStatus.ACTIVE,
    capabilities=[
        AgentCapability(
            name="process_payment",
            description="Generate payment instructions for an approved invoice",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id":   {"type": "string"},
                    "amount_usd":  {"type": "number"},
                    "invoice_number": {"type": "string"},
                },
                "required": ["vendor_id", "amount_usd"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "payment_id":     {"type": "string"},
                    "transaction_id": {"type": "string"},
                    "status":         {"type": "string"},
                    "processed_at":   {"type": "string"},
                },
            },
            tags=["payments", "write"],
            requires_hitl=True,
            idempotent=False,
        ),
    ],
    metadata={
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system_prompt": (
            "You are a payment processing agent. "
            "Generate a payment_id (format PAY-XXXXXX), transaction_id (format TXN-XXXXXXX), "
            "status='success', and processed_at (ISO-8601 UTC timestamp). "
            "Return ONLY a valid JSON object — no markdown."
        ),
        "output_fields": "payment_id, transaction_id, status, processed_at",
        "cost_per_1k_input":  0.00025,
        "cost_per_1k_output": 0.00125,
    },
))


# ── Seed: Escalation demo agents ──────────────────────────────────────────────

registry.register(AgentManifest(
    agent_id="openai-ambiguous-classifier",
    name="Ambiguous Ticket Classifier (OpenAI)",
    version="1.0.0",
    description=(
        "Classifies support tickets using GPT-4o-mini with strict confidence thresholds. "
        "When the ticket spans multiple categories or confidence < 0.70, the agent signals "
        "escalation with 'escalate: true' so a human can assign the primary category. "
        "This is the canonical pattern for agent-level HITL in AgentForge."
    ),
    owner="support-team",
    runtime_adapter="openai",
    tags=["support", "classification", "escalation", "hitl"],
    status=AgentStatus.ACTIVE,
    capabilities=[
        AgentCapability(
            name="classify_with_escalation",
            description=(
                "Classify a support ticket; escalate to human if confidence < 0.70 "
                "or the ticket spans multiple categories"
            ),
            input_schema={"type": "object", "properties": {"ticket_text": {"type": "string"}}},
            output_schema={
                "type": "object",
                "oneOf": [
                    {
                        "description": "Confident classification",
                        "properties": {
                            "issue_type": {"type": "string"},
                            "severity":   {"type": "string"},
                            "confidence": {"type": "number"},
                            "affected_area": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                    },
                    {
                        "description": "Escalation request",
                        "properties": {
                            "escalate":          {"type": "boolean", "const": True},
                            "reason":            {"type": "string"},
                            "confidence":        {"type": "number"},
                            "candidates":        {"type": "array"},
                            "human_input_needed":{"type": "string"},
                        },
                    },
                ],
            },
            tags=["classification", "escalation"],
            idempotent=True,
        ),
    ],
    metadata={
        "model": "gpt-4o-mini",
        "max_tokens": 512,
        "system_prompt": (
            "You are a support ticket classification agent with strict confidence requirements.\n\n"
            "Analyze the ticket and classify into ONE of: billing, technical, account, feature_request, other.\n\n"
            "If confidence >= 0.70, return this JSON:\n"
            '{"issue_type":"<category>","severity":"<low|medium|high|critical>",'
            '"confidence":<0.70-1.0>,"affected_area":"<area>",'
            '"summary":"<one sentence>","key_details":"<specific codes or IDs>"}\n\n'
            "If confidence < 0.70 (ticket is ambiguous or spans multiple categories), return:\n"
            '{"escalate":true,"reason":"<why you cannot classify confidently>",'
            '"confidence":<0.0-0.69>,"candidates":["<cat1>","<cat2>"],'
            '"human_input_needed":"Please review and assign the primary category"}\n\n'
            "Return ONLY valid JSON, no markdown."
        ),
        "output_fields": "issue_type, severity, confidence, affected_area, summary, key_details",
        "cost_per_1k_input":  0.00015,
        "cost_per_1k_output": 0.00060,
    },
))

registry.register(AgentManifest(
    agent_id="claude-escalation-responder",
    name="Escalation Response Drafter (Claude)",
    version="1.0.0",
    description=(
        "Drafts a customer response after a human-resolved ticket classification. "
        "Aware of the escalation context — references the human reviewer's notes "
        "and assigns appropriate SLA and routing."
    ),
    owner="support-team",
    runtime_adapter="anthropic",
    tags=["support", "response", "post-escalation"],
    status=AgentStatus.ACTIVE,
    capabilities=[
        AgentCapability(
            name="draft_post_escalation_response",
            description="Draft customer response using human-resolved classification",
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "properties": {
                    "response_draft": {"type": "string"},
                    "priority_level": {"type": "string"},
                    "assign_to":      {"type": "string"},
                    "sla_hours":      {"type": "number"},
                    "escalation_note":{"type": "string"},
                },
            },
            tags=["response", "post-escalation"],
            idempotent=True,
        ),
    ],
    metadata={
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 768,
        "system_prompt": (
            "You are a support response drafting agent. The ticket has been classified "
            "by a human reviewer after the AI escalated due to ambiguity.\n\n"
            "Use issue_type, severity, summary, _human_category, and _human_notes from "
            "the execution context to draft a professional customer response.\n\n"
            "Return JSON with: response_draft (2-3 sentences to the customer), "
            "priority_level (P1/P2/P3/P4), assign_to (team name), "
            "sla_hours (number), escalation_note (one sentence about why this was escalated). "
            "Return ONLY valid JSON, no markdown."
        ),
        "output_fields": "response_draft, priority_level, assign_to, sla_hours, escalation_note",
        "cost_per_1k_input":  0.00025,
        "cost_per_1k_output": 0.00125,
    },
))


# ── Seed: CTO Demo agents ─────────────────────────────────────────────────────

registry.register(AgentManifest(
    agent_id="claude-ticket-classifier",
    name="Ticket Classifier (Claude)",
    version="1.0.0",
    description=(
        "Classifies a support ticket and extracts structured fields. "
        "Returns issue_type, severity, affected_area, summary, and key_details."
    ),
    owner="support-team",
    runtime_adapter="anthropic",
    tags=["support", "classification", "triage"],
    status=AgentStatus.ACTIVE,
    capabilities=[
        AgentCapability(
            name="classify_ticket",
            description="Classify and extract fields from a raw support ticket",
            input_schema={"type": "object", "properties": {"ticket_text": {"type": "string"}}},
            output_schema={
                "type": "object",
                "properties": {
                    "issue_type":    {"type": "string"},
                    "severity":      {"type": "string"},
                    "affected_area": {"type": "string"},
                    "summary":       {"type": "string"},
                    "key_details":   {"type": "string"},
                },
            },
            tags=["classification"],
            idempotent=True,
        ),
    ],
    metadata={
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system_prompt": (
            "You are a support ticket classification agent. "
            "Analyze the ticket_text in the execution context and return a JSON object with: "
            "issue_type (e.g. 'billing', 'technical', 'account', 'feature_request'), "
            "severity (one of: low, medium, high, critical), "
            "affected_area (product area or service name), "
            "summary (one sentence summary of the issue), "
            "key_details (any specific error codes, account IDs, or timestamps mentioned). "
            "Return ONLY a valid JSON object — no markdown, no explanation."
        ),
        "output_fields": "issue_type, severity, affected_area, summary, key_details",
        "cost_per_1k_input":  0.00025,
        "cost_per_1k_output": 0.00125,
    },
))


registry.register(AgentManifest(
    agent_id="openai-response-generator",
    name="Response Generator (OpenAI)",
    version="1.0.0",
    description=(
        "Drafts a customer-facing response for a classified support ticket. "
        "Returns response_draft, priority_level, assign_to, and eta_hours."
    ),
    owner="support-team",
    runtime_adapter="openai",
    tags=["support", "response", "drafting"],
    status=AgentStatus.ACTIVE,
    capabilities=[
        AgentCapability(
            name="generate_response",
            description="Draft a support response based on ticket classification",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_type":    {"type": "string"},
                    "severity":      {"type": "string"},
                    "summary":       {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "response_draft": {"type": "string"},
                    "priority_level": {"type": "string"},
                    "assign_to":      {"type": "string"},
                    "eta_hours":      {"type": "number"},
                },
            },
            tags=["drafting", "response"],
            idempotent=True,
        ),
    ],
    metadata={
        "model": "gpt-4o-mini",
        "max_tokens": 768,
        "system_prompt": (
            "You are a customer support response drafting agent. "
            "Using the ticket classification data (issue_type, severity, summary, key_details) "
            "from the execution context, return a JSON object with: "
            "response_draft (a professional, empathetic 2-3 sentence reply to the customer), "
            "priority_level (one of: P1, P2, P3, P4 based on severity), "
            "assign_to (team name: 'billing-team', 'infra-team', 'product-team', or 'tier1-support'), "
            "eta_hours (estimated resolution hours as a number: 1, 4, 8, 24, or 48). "
            "Return ONLY a valid JSON object — no markdown, no explanation."
        ),
        "output_fields": "response_draft, priority_level, assign_to, eta_hours",
        "cost_per_1k_input":  0.00015,
        "cost_per_1k_output": 0.00060,
    },
))


# ── Seed: OpenAI-managed agent ─────────────────────────────────────────────────

registry.register(AgentManifest(
    agent_id="openai-risk-scorer",
    name="Risk Scorer (OpenAI)",
    version="1.0.0",
    description=(
        "Evaluates the financial risk of a payment request using GPT-4o-mini. "
        "Returns a risk_score (0–100), risk_level (low|medium|high), and a rationale string."
    ),
    owner="risk-team",
    runtime_adapter="openai",
    tags=["finance", "risk", "scoring"],
    status=AgentStatus.ACTIVE,
    capabilities=[
        AgentCapability(
            name="score_risk",
            description="Assess payment risk and return a score and level",
            input_schema={
                "type": "object",
                "properties": {
                    "vendor_id":  {"type": "string"},
                    "amount_usd": {"type": "number"},
                    "items":      {"type": "array"},
                },
                "required": ["vendor_id", "amount_usd"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "risk_score": {"type": "number"},
                    "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
                    "rationale":  {"type": "string"},
                },
            },
            tags=["risk", "scoring"],
            idempotent=True,
        ),
    ],
    metadata={
        "model": "gpt-4o-mini",
        "max_tokens": 512,
        "system_prompt": (
            "You are a financial risk assessment agent. "
            "Given vendor and invoice data, return a JSON object with: "
            "risk_score (integer 0-100, where 100 = highest risk), "
            "risk_level (exactly one of: low, medium, high), "
            "rationale (one sentence). "
            "Consider amount_usd > 15000 as elevated risk. "
            "Return ONLY a valid JSON object — no markdown, no explanation."
        ),
        "output_fields": "risk_score, risk_level, rationale",
        # GPT-4o-mini pricing (USD per 1k tokens)
        "cost_per_1k_input":  0.00015,
        "cost_per_1k_output": 0.00060,
    },
))
