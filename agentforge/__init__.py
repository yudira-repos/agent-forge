"""
AgentForge — Open Enterprise Agent Framework
=============================================
Production-grade infrastructure for building, governing, and auditing
AI agents in enterprise environments.

Components:
  - aiam        : Agent Identity & Authority Management
  - registry    : Agent Registry with discovery and versioning
  - governance  : Policy engine with SOC 2, HIPAA, GDPR profiles
  - audit       : Structured auditability SDK
  - hitl        : Human-in-the-loop orchestration
  - runtime     : Multi-framework agent runtime (Anthropic, OpenAI, LangChain, Vertex)
"""

__version__ = "0.1.0"
__author__ = "AgentForge Contributors"
__license__ = "Apache-2.0"

from agentforge import aiam, audit, governance, hitl, registry, runtime

__all__ = ["aiam", "audit", "governance", "hitl", "registry", "runtime", "__version__"]
