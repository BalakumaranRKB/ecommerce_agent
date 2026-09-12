"""
Typed decision objects for the main agent's refund path (Phase 4 / spec §2.7).

This is the "decision is data, not a sentence" boundary: from here on, a
refund decision is a validated Pydantic object with a real `amount: int`
field -- not a number implied somewhere inside a sentence the model wrote.
Pydantic (not a plain dataclass, unlike billing_specialist/schemas.py's
deliberate choice for the A2A wire format) because construction itself must
BE the validation point: a non-int amount or an out-of-enum action raises
HERE, at object-construction time, not three call frames downstream when
something tries to authorize a refund against it.

RefundDecision is the main-agent-side counterpart to the specialist's typed
DisputeResult (billing_specialist/schemas.py). They are kept as two distinct
types on purpose:
  - DisputeResult is the specialist's wire format crossing the A2A boundary.
  - RefundDecision is what the MAIN agent treats as source of truth for a
    turn, however that decision was arrived at.

Today that's always by mapping an already-decided DisputeResult
(`from_dispute_result`) -- the specialist owns the refund verdict end to end,
and Phase 4 must not double-build that job with a second LLM call (see
docs/phase_4_implementation_plan.md, "Risks / things to watch"). A future
non-handoff path, if one is ever added, would populate this SAME
RefundDecision shape via the structured-output tool-use seam in llm.py
(`Provider.create_structured`) instead -- same typed object, different origin,
single downstream contract either way.

PendingApproval is the snapshot Phase 5's HITL gate will pause execution on --
built now, ahead of Phase 5, per the build plan's dependency note, so Phase 5
consumes an existing shape rather than defining one under time pressure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # Only needed for the type hint on from_dispute_result(); kept out of the
    # runtime import path so decisions.py has no hard dependency on
    # billing_specialist for callers (e.g. the structured-output tests) that
    # never touch a DisputeResult.
    from billing_specialist.schemas import DisputeResult


class RefundDecision(BaseModel):
    """The typed refund decision the main agent treats as source of truth.

    `amount` is an int (rupees, paise-free -- see the Phase 4 plan's "Risks:
    keep the amount integer-typed end to end"). A float or string sneaking in
    here would re-introduce exactly the ambiguity Phase 4 exists to remove,
    so it's enforced by the field's type, not by a convention someone has to
    remember to follow.
    """

    order_id: str
    customer_id: str
    amount: int = Field(ge=0, description="Refund amount in INR, whole rupees.")
    action: Literal["approve", "reject", "escalate_hitl"]
    requires_human: bool
    reasoning: str
    # Phase 5 (spec §7.1): the externalized policy boundary's verdict on this
    # amount, when one has been taken. Optional/additive so Phase 4's tests and
    # any pre-policy caller still construct a valid RefundDecision without it.
    policy_outcome: Optional[Literal["allow", "needs_approval"]] = None

    @classmethod
    def from_dispute_result(cls, result: "DisputeResult") -> "RefundDecision":
        """Map the billing specialist's typed DisputeResult into the main
        agent's typed RefundDecision.

        This is a MAPPING, not a re-decision: the specialist already decided
        (its `resolve_dispute` is pure rule-based logic, no LLM). Phase 4 must
        not spend a second LLM call re-deriving the same verdict -- there is
        exactly one source of truth for the refund verdict, and this method
        is how it crosses from the specialist's typed result into the main
        agent's typed decision.

        `policy_outcome` is carried through when the specialist's result already
        has it (Phase 5 settlement populates it); absent on a plain verdict.
        """
        return cls(
            order_id=result.order_id,
            customer_id=result.customer_id,
            amount=result.refund_amount,
            action=result.action,
            requires_human=result.requires_human_review,
            reasoning=result.reasoning,
            policy_outcome=getattr(result, "policy_outcome", None),
        )


class EscalationReason(BaseModel):
    """Why a decision needs a human. Phase 5's policy gate (and/or the
    specialist) attaches one of these to a PendingApproval when it escalates.
    """

    code: Literal["over_threshold", "flagged_account", "state_changed", "double_refund"]
    detail: str


class PendingApproval(BaseModel):
    """The snapshot Phase 5's HITL gate pauses execution on.

    Keyed on order_id (the RESOURCE being mutated), matching the §2.5
    double-refund correlation requirement: correlate by what is being
    changed, not by which ticket/request asked for it -- two refund requests
    against the same order must be recognisable as the same pending action.
    """

    order_id: str
    customer_id: str
    amount: int = Field(ge=0)
    status_snapshot: str  # order status at pause time, for resume re-validation
    requested_refund: int
    reasoning: str
