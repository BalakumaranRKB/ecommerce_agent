"""
Typed payloads for the billing specialist A2A boundary.

These dataclasses define the EXACT state that crosses the handoff boundary.
The §2.1 CWP (dropped-handoff bug) is seeded by the `account_standing` field:
omitting it from DisputeRequest means the specialist cannot apply flagged-account
scrutiny, and confidently approves refunds it should have escalated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Optional

import json


@dataclass
class DisputeRequest:
    """The state that crosses the boundary from the main agent to the specialist.

    Every field here is a deliberate design decision:
    - order_id + customer_id: identity and ownership (harness already enforces)
    - refund_amount: the typed amount the policy engine keys off
    - reason: free text from the customer
    - account_standing: THE FIELD THAT MUST CROSS. If omitted, the specialist
      cannot know the customer is 'flagged' and will approve against incomplete
      context. This is the §2.1 CWP seed.
    """
    order_id: str
    customer_id: str
    refund_amount: int
    reason: str
    account_standing: str = "active"  # active / flagged / suspended

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> DisputeRequest:
        return cls(**json.loads(raw))


@dataclass
class DisputeResult:
    """The specialist's typed response — crosses back to the main agent.

    The main agent's LLM phrases this into a customer-facing response.
    The specialist does not generate prose; it returns structured data.
    """
    order_id: str
    customer_id: str
    approved: bool
    requires_human_review: bool
    refund_amount: int
    reasoning: str
    action: Literal["approve", "reject", "escalate_hitl"]
    # Phase 5 (spec §2.5/§2.6): what the specialist's execution seam actually DID
    # after the verdict -- authorize+execute, pause for a human, or block. These
    # are additive and optional: a bare verdict from resolve_dispute() (and every
    # pre-Phase-5 caller/test) leaves them None, and to_json/from_json round-trip
    # unchanged. See billing_specialist/settlement.py.
    settlement_outcome: Optional[Literal[
        "executed", "pending_approval", "rejected", "duplicate_blocked",
        "state_changed", "expired",
    ]] = None
    policy_outcome: Optional[Literal["allow", "needs_approval"]] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> DisputeResult:
        return cls(**json.loads(raw))
