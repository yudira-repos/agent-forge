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
