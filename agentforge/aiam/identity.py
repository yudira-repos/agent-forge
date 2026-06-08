"""Agent Identity — cryptographically-signed agent credentials."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentIdentity:
    """
    Immutable identity record for a registered agent.

    Each agent gets a stable UUID (``agent_id``) that persists across
    restarts and a set of roles that determine what the agent is allowed
    to do in the system.

    Example::

        identity = AgentIdentity.create(
            name="invoice-processor",
            roles=["finance-reader", "document-writer"],
            owner="finance-team",
            metadata={"env": "production"},
        )
        print(identity.agent_id)  # stable UUID
    """

    agent_id: str
    name: str
    roles: list[str]
    owner: str
    created_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        name: str,
        roles: list[str],
        owner: str,
        metadata: dict[str, Any] | None = None,
    ) -> "AgentIdentity":
        """Create a new agent identity with a freshly-generated UUID."""
        return cls(
            agent_id=str(uuid.uuid4()),
            name=name,
            roles=list(roles),
            owner=owner,
            created_at=time.time(),
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "roles": self.roles,
            "owner": self.owner,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentIdentity":
        return cls(**data)


@dataclass
class AgentCredential:
    """
    Short-lived signed credential (similar to a JWT) that an agent presents
    when invoking tools or calling downstream services.

    The ``token`` field is an HMAC-SHA256 over the credential payload using
    the issuer's signing secret.  Use ``verify()`` before trusting any
    credential received over the wire.

    Example::

        secret = b"super-secret-key"
        cred = AgentCredential.issue(identity, ttl=300, secret=secret)
        assert cred.verify(secret)
    """

    agent_id: str
    issued_at: float
    expires_at: float
    scopes: list[str]
    token: str  # hex-encoded HMAC

    @classmethod
    def issue(
        cls,
        identity: AgentIdentity,
        scopes: list[str],
        ttl: int,
        secret: bytes,
    ) -> "AgentCredential":
        """Issue a signed credential for *identity* valid for *ttl* seconds."""
        now = time.time()
        agent_id: str = identity.agent_id
        issued_at: float = now
        expires_at: float = now + ttl
        sorted_scopes: list[str] = sorted(scopes)
        payload = {
            "agent_id": agent_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "scopes": sorted_scopes,
        }
        token = hmac.new(
            secret,
            json.dumps(payload, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        return cls(
            agent_id=agent_id,
            issued_at=issued_at,
            expires_at=expires_at,
            scopes=sorted_scopes,
            token=token,
        )

    def verify(self, secret: bytes) -> bool:
        """Return True if the token is valid and not expired."""
        if time.time() > self.expires_at:
            return False
        payload = {
            "agent_id": self.agent_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "scopes": sorted(self.scopes),
        }
        expected = hmac.new(
            secret,
            json.dumps(payload, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.token, expected)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at
