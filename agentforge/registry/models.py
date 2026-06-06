"""Data models for agent registration and capability declaration."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEPRECATED = "deprecated"
    MAINTENANCE = "maintenance"


@dataclass
class AgentCapability:
    """
    Declares a single capability an agent exposes.

    Capabilities are the contract between agents and their consumers:
    they describe *what* the agent can do, the *inputs* it expects, and
    the *outputs* it produces.

    Example::

        cap = AgentCapability(
            name="summarize_document",
            description="Summarize a document to a given length",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
            tags=["nlp", "summarization"],
        )
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    requires_hitl: bool = False  # if True, capability triggers human-in-the-loop
    idempotent: bool = True


@dataclass
class AgentManifest:
    """
    Full declaration of an agent's identity, capabilities, and runtime contract.

    Manifests are versioned (semver string) so the registry can maintain a
    history and consumers can pin to a specific version.

    Example::

        manifest = AgentManifest(
            agent_id="invoice-processor-001",
            name="Invoice Processor",
            version="1.2.0",
            description="Extracts and validates invoice data from PDFs",
            owner="finance-team",
            capabilities=[
                AgentCapability("extract_invoice", "Extract structured data from a PDF invoice"),
                AgentCapability("validate_invoice", "Validate extracted invoice against business rules"),
            ],
            runtime_adapter="anthropic",
            tags=["finance", "document-processing"],
        )
    """

    agent_id: str
    name: str
    version: str  # semver e.g. "1.2.0"
    description: str
    owner: str
    capabilities: list[AgentCapability] = field(default_factory=list)
    runtime_adapter: str = "anthropic"  # anthropic | openai | langchain | vertex
    tags: list[str] = field(default_factory=list)
    status: AgentStatus = AgentStatus.ACTIVE
    registered_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    required_roles: list[str] = field(default_factory=list)  # RBAC roles needed to invoke

    def get_capability(self, name: str) -> AgentCapability | None:
        return next((c for c in self.capabilities if c.name == name), None)

    def capability_names(self) -> list[str]:
        return [c.name for c in self.capabilities]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "owner": self.owner,
            "capabilities": [
                {
                    "name": c.name,
                    "description": c.description,
                    "tags": c.tags,
                    "requires_hitl": c.requires_hitl,
                    "idempotent": c.idempotent,
                }
                for c in self.capabilities
            ],
            "runtime_adapter": self.runtime_adapter,
            "tags": self.tags,
            "status": self.status.value,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "required_roles": self.required_roles,
        }
