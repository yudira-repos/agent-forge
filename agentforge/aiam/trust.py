"""Trust chains — agent-to-agent authority delegation."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class DelegationToken:
    """
    A signed delegation token that allows an agent (the *delegate*) to
    act with a subset of the *delegator*'s authority.

    The token encodes the delegation path so auditors can reconstruct
    the full chain:  root-agent → mid-agent → leaf-agent.

    Example::

        secret = b"org-signing-key"
        token = DelegationToken.create(
            delegator_id="orchestrator-001",
            delegate_id="worker-042",
            scopes=["documents:read", "summaries:write"],
            ttl=600,
            secret=secret,
        )
        assert token.verify(secret)
    """

    token_id: str
    delegator_id: str
    delegate_id: str
    scopes: list[str]
    issued_at: float
    expires_at: float
    parent_token_id: str | None  # for chained delegations
    signature: str

    @classmethod
    def create(
        cls,
        delegator_id: str,
        delegate_id: str,
        scopes: list[str],
        ttl: int,
        secret: bytes,
        parent_token_id: str | None = None,
    ) -> "DelegationToken":
        now = time.time()
        token_id = str(uuid.uuid4())
        payload = {
            "token_id": token_id,
            "delegator_id": delegator_id,
            "delegate_id": delegate_id,
            "scopes": sorted(scopes),
            "issued_at": now,
            "expires_at": now + ttl,
            "parent_token_id": parent_token_id,
        }
        sig = hmac.new(
            secret,
            json.dumps(payload, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        return cls(**payload, signature=sig)

    def verify(self, secret: bytes) -> bool:
        """Verify signature and expiry."""
        if time.time() > self.expires_at:
            return False
        payload = {
            "token_id": self.token_id,
            "delegator_id": self.delegator_id,
            "delegate_id": self.delegate_id,
            "scopes": sorted(self.scopes),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "parent_token_id": self.parent_token_id,
        }
        expected = hmac.new(
            secret,
            json.dumps(payload, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.signature, expected)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class TrustChain:
    """
    A validated chain of delegation tokens from a root agent to a leaf.

    Call ``TrustChain.build()`` to assemble and verify a delegation path,
    then ``effective_scopes()`` to get the intersection of all delegated
    scopes along the chain (scopes can only narrow, never widen).

    Example::

        chain = TrustChain.build(
            tokens=[orchestrator_token, worker_token],
            secret=b"org-signing-key",
        )
        scopes = chain.effective_scopes()  # narrowest intersection
    """

    def __init__(self, tokens: list[DelegationToken]) -> None:
        self._tokens = tokens

    @classmethod
    def build(cls, tokens: list[DelegationToken], secret: bytes) -> "TrustChain":
        """
        Validate and assemble a delegation chain.

        Raises ``ValueError`` if any token is invalid, expired, or if the
        chain linkage is broken (delegate[n] must equal delegator[n+1]).
        """
        if not tokens:
            raise ValueError("Trust chain must contain at least one token.")

        for i, token in enumerate(tokens):
            if not token.verify(secret):
                raise ValueError(
                    f"Token {token.token_id} at position {i} has an invalid "
                    f"signature or is expired."
                )

        for i in range(len(tokens) - 1):
            if tokens[i].delegate_id != tokens[i + 1].delegator_id:
                raise ValueError(
                    f"Chain broken at position {i}: "
                    f"delegate={tokens[i].delegate_id} != "
                    f"delegator={tokens[i + 1].delegator_id}"
                )

        return cls(tokens)

    def effective_scopes(self) -> set[str]:
        """
        Return the intersection of scopes across all tokens in the chain.
        Each delegation can only reduce, never expand, authority.
        """
        if not self._tokens:
            return set()
        scope_sets = [set(t.scopes) for t in self._tokens]
        result = scope_sets[0]
        for s in scope_sets[1:]:
            result &= s
        return result

    @property
    def root_agent(self) -> str:
        return self._tokens[0].delegator_id

    @property
    def leaf_agent(self) -> str:
        return self._tokens[-1].delegate_id

    @property
    def depth(self) -> int:
        return len(self._tokens)

    def summary(self) -> dict[str, Any]:
        return {
            "root": self.root_agent,
            "leaf": self.leaf_agent,
            "depth": self.depth,
            "effective_scopes": sorted(self.effective_scopes()),
            "chain": [
                {
                    "token_id": t.token_id,
                    "delegator": t.delegator_id,
                    "delegate": t.delegate_id,
                }
                for t in self._tokens
            ],
        }
