"""
The specialist's refund SETTLEMENT seam (Phase 5 / spec §2.5 + §2.6).

`resolve_dispute()` (agent.py) DECIDES — it returns a verdict
(approve / reject / escalate_hitl). It does not execute anything, and it does
not pause for a human. This module is the station *after* the verdict:

    resolve_dispute (verdict)  ->  authorize amount (policy.py)  ->
        execute now  |  pause for a human (hitl.py)  |  reject

It lives on the specialist side on purpose: refund EXECUTION authority belongs
to the billing specialist, never the read-only main agent (docs/autonomy-decision.md).
The main agent only ever phrases the outcome for the customer.

WHY THIS ISN'T DOUBLE-BUILDING THE SPECIALIST'S JOB
---------------------------------------------------
`resolve_dispute` already escalates over-threshold and flagged accounts — that
is the VERDICT. The policy boundary here is not a second verdict; it is an
independent authorization of the *amount* at the execution seam, exactly the way
`harness_check()` re-checks the model rather than trusting it. For a normal
`approve` verdict the two agree (amount is under threshold, policy allows,
execute). The policy layer earns its place as defense in depth: if a verdict ever
reached execution with an over-threshold amount — a logic bug, a future code
path that skipped `resolve_dispute` — the boundary catches it and routes to a
human instead of paying out.

STATE
-----
The gate holds pause/resume state in a module-level singleton so a `pause` from
one A2A round-trip is still there when a human's `resume` arrives on a later one
(within the running specialist process). Swap the store for
`AgentCoreMemoryApprovalStore` to survive restarts (Phase 5 Step 0, needs a
memory_id). Correlation is by ORDER (the resource) — the §2.5 double-refund fix.
"""

from __future__ import annotations

import os
import sys

# Reach the project-root modules (hitl, policy, decisions, mock_data), same as
# billing_specialist/agent.py does when run as a standalone module.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from billing_specialist.agent import resolve_dispute
from billing_specialist.schemas import DisputeRequest, DisputeResult
from decisions import RefundDecision
from hitl import AgentCoreMemoryApprovalStore, HITLGate, LiveRefundState
from mock_data import ORDERS
from policy import authorize_refund

# The specialist process's single approval gate. Order-keyed (the fix); in-memory
# by default, or AgentCoreMemoryApprovalStore when HITL_MEMORY_ID is set.
def _make_gate() -> HITLGate:
    mem_id = os.getenv("HITL_MEMORY_ID") or os.getenv("MEMORY_ID")
    if mem_id:
        region = os.getenv("AWS_REGION", "ap-south-1")
        store = AgentCoreMemoryApprovalStore(memory_id=mem_id, region_name=region)
        return HITLGate(store=store, correlate_by="order")
    return HITLGate(correlate_by="order")

_GATE = _make_gate()


def _order_status(order_id: str) -> str:
    order = ORDERS.get(order_id)
    return order["status"] if order else "unknown"


def _result(
    *, order_id: str, customer_id: str, amount: int,
    action: str, approved: bool, requires_human: bool, reasoning: str,
    settlement_outcome: str, policy_outcome: str | None,
) -> DisputeResult:
    return DisputeResult(
        order_id=order_id, customer_id=customer_id, approved=approved,
        requires_human_review=requires_human, refund_amount=amount,
        reasoning=reasoning, action=action,  # type: ignore[arg-type]
        settlement_outcome=settlement_outcome,  # type: ignore[arg-type]
        policy_outcome=policy_outcome,  # type: ignore[arg-type]
    )


def settle_dispute(request: DisputeRequest, gate: HITLGate | None = None) -> DisputeResult:
    """Decide, then authorize, then execute-or-pause. Returns a DisputeResult
    whose verdict fields (action/approved/requires_human_review) are preserved
    from `resolve_dispute`, plus the additive settlement_outcome/policy_outcome.

    `gate` is injectable for tests; production uses the module singleton.
    """
    gate = gate or _GATE
    verdict = resolve_dispute(request)
    decision = RefundDecision.from_dispute_result(verdict)
    status = _order_status(request.order_id)

    # Reject verdict: nothing to authorize or execute.
    if verdict.action == "reject":
        return _result(
            order_id=verdict.order_id, customer_id=verdict.customer_id,
            amount=verdict.refund_amount, action="reject", approved=False,
            requires_human=False, reasoning=verdict.reasoning,
            settlement_outcome="rejected", policy_outcome=None,
        )

    # Escalate verdict: the specialist already routed this to a human. Pause and
    # persist the snapshot; do not execute.
    if verdict.action == "escalate_hitl":
        gate.request_approval(decision, request_id="", order_status=status)
        return _result(
            order_id=verdict.order_id, customer_id=verdict.customer_id,
            amount=verdict.refund_amount, action="escalate_hitl", approved=False,
            requires_human=True, reasoning=verdict.reasoning,
            settlement_outcome="pending_approval", policy_outcome="needs_approval",
        )

    # Approve verdict: independently authorize the AMOUNT before executing.
    pol = authorize_refund(request.customer_id, request.order_id, request.refund_amount)
    if pol == "needs_approval":
        # Defense in depth: the verdict said approve, but the amount is at/over
        # threshold. Trust the boundary, not the verdict — route to a human.
        gate.request_approval(decision, request_id="", order_status=status)
        return _result(
            order_id=verdict.order_id, customer_id=verdict.customer_id,
            amount=verdict.refund_amount, action="escalate_hitl", approved=False,
            requires_human=True,
            reasoning=(verdict.reasoning + " [Policy override: amount requires "
                       "human review despite an approve verdict.]"),
            settlement_outcome="pending_approval", policy_outcome="needs_approval",
        )

    # Authorized: execute now, THROUGH the gate, so the order-keyed double-refund
    # guard still applies (a second auto-approved refund on an already-paid order
    # is blocked). The refetch mirrors what we just decided — no drift at t0.
    gate.request_approval(decision, request_id="", order_status=status)
    outcome = gate.resume(
        request.order_id, "", "approve",
        refetch=lambda oid, rid: LiveRefundState(
            refund_amount=request.refund_amount, order_status=status),
    )
    if outcome.executed:
        return _result(
            order_id=verdict.order_id, customer_id=verdict.customer_id,
            amount=verdict.refund_amount, action="approve", approved=True,
            requires_human=False, reasoning=verdict.reasoning,
            settlement_outcome="executed", policy_outcome="allow",
        )
    # A guard blocked execution (e.g. this order already has a refund out).
    detail = outcome.escalation.detail if outcome.escalation else "refund not executed"
    return _result(
        order_id=verdict.order_id, customer_id=verdict.customer_id,
        amount=verdict.refund_amount, action="reject", approved=False,
        requires_human=False, reasoning=detail,
        settlement_outcome="duplicate_blocked", policy_outcome="allow",
    )


def resume_settlement(
    order_id: str, human_action: str, gate: HITLGate | None = None
) -> DisputeResult:
    """A human's approve/reject on a paused refund. Re-validates live order state
    first (aborts on drift), guards against a double refund, then executes or
    rejects. Returns a DisputeResult carrying the settlement_outcome.
    """
    gate = gate or _GATE
    rec = gate.store.get(order_id)  # order-keyed store
    if rec is None:
        return _result(
            order_id=order_id, customer_id="", amount=0, action="reject",
            approved=False, requires_human=False,
            reasoning=f"No pending approval found for order {order_id}.",
            settlement_outcome="rejected", policy_outcome=None,
        )

    customer_id = rec.snapshot.customer_id
    amount = rec.snapshot.amount

    # Re-validation reads LIVE order status; amount can't drift in the mock, so we
    # carry the parked amount and let a real status change trip state_changed.
    outcome = gate.resume(
        order_id, "", human_action,
        refetch=lambda oid, rid: LiveRefundState(
            refund_amount=rec.snapshot.requested_refund,
            order_status=_order_status(oid)),
    )

    if outcome.executed:
        return _result(
            order_id=order_id, customer_id=customer_id, amount=amount,
            action="approve", approved=True, requires_human=False,
            reasoning="Refund approved by a human reviewer and executed.",
            settlement_outcome="executed", policy_outcome="allow",
        )

    code = outcome.escalation.code if outcome.escalation else None
    if code == "state_changed":
        return _result(
            order_id=order_id, customer_id=customer_id, amount=amount,
            action="escalate_hitl", approved=False, requires_human=True,
            reasoning=outcome.escalation.detail,  # type: ignore[union-attr]
            settlement_outcome="state_changed", policy_outcome=None,
        )
    if code == "double_refund":
        return _result(
            order_id=order_id, customer_id=customer_id, amount=amount,
            action="reject", approved=False, requires_human=False,
            reasoning=outcome.escalation.detail,  # type: ignore[union-attr]
            settlement_outcome="duplicate_blocked", policy_outcome=None,
        )
    if outcome.status.value == "expired":
        return _result(
            order_id=order_id, customer_id=customer_id, amount=amount,
            action="escalate_hitl", approved=False, requires_human=True,
            reasoning="This approval expired before it was actioned; re-submit for review.",
            settlement_outcome="expired", policy_outcome=None,
        )
    # Human rejected (or any other non-executing terminal).
    return _result(
        order_id=order_id, customer_id=customer_id, amount=amount,
        action="reject", approved=False, requires_human=False,
        reasoning="Refund rejected by a human reviewer.",
        settlement_outcome="rejected", policy_outcome=None,
    )
