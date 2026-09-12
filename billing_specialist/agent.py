"""
Billing specialist â€” deterministic dispute resolution logic.

This is the specialist's core reasoning, exposed as a plain function
(resolve_dispute) for standalone testing, and called by the A2A server
for the real handoff. Same function, two entry points.

The logic is deliberately NOT an LLM call. The specialist *decides* by rules
(check order, check amount vs threshold, check account standing, check for
prior refunds on the same order). This makes it:
  - Testable without API keys
  - Deterministic and auditable
  - Immune to prompt injection at the decision layer

The main agent's LLM phrases the typed result into a customer-facing response.

HITL_THRESHOLD_INR = 5000 is the shared constant from spec Â§6.2.
"""

from __future__ import annotations

import sys
import os

# Allow imports from the project root when run as a standalone module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from billing_specialist.schemas import DisputeRequest, DisputeResult
from mock_data import ORDERS, REFUND_REQUESTS, ACCOUNTS

# The shared constant â€” same value used by the policy engine (Phase 5)
# and the HITL gate (Phase 5b). Defined once, imported everywhere.
HITL_THRESHOLD_INR = 5000


def _check_prior_refunds_on_order(order_id: str, exclude_request_id: str | None = None) -> list[dict]:
    """Find any existing refund requests targeting the same order.

    This is the double-refund guard: req_r004 and req_r005 both target
    ord_6002, and only one should be approved. Without this check, both
    go through â€” that is the Â§2.5 CWP (Phase 5b, not this phase).
    """
    prior = []
    for req_id, req in REFUND_REQUESTS.items():
        if req["order_id"] == order_id and req_id != exclude_request_id:
            prior.append(req)
    return prior


def resolve_dispute(request: DisputeRequest) -> DisputeResult:
    """The specialist's decision function. Pure logic, no LLM.

    Decision tree:
    1. Order must exist and belong to the customer.
    2. If account_standing is 'suspended' -> reject outright.
    3. If account_standing is 'flagged' -> always escalate to HITL,
       regardless of amount (flagged customers need human review).
    4. If refund_amount > HITL_THRESHOLD_INR -> escalate to HITL.
    5. If refund_amount == HITL_THRESHOLD_INR -> escalate to HITL
       (>= boundary, documented decision: exactly-at-threshold goes to HITL).
    6. If prior approved/executed refund exists on same order -> escalate
       (double-refund guard).
    7. Otherwise -> approve.
    """
    # 1. Validate order exists and belongs to customer
    order = ORDERS.get(request.order_id)
    if order is None:
        return DisputeResult(
            order_id=request.order_id,
            customer_id=request.customer_id,
            approved=False,
            requires_human_review=False,
            refund_amount=request.refund_amount,
            reasoning=f"Order {request.order_id} not found.",
            action="reject",
        )
    if order["customer_id"] != request.customer_id:
        return DisputeResult(
            order_id=request.order_id,
            customer_id=request.customer_id,
            approved=False,
            requires_human_review=False,
            refund_amount=request.refund_amount,
            reasoning=f"Order {request.order_id} does not belong to customer {request.customer_id}.",
            action="reject",
        )

    # 2. Suspended account -> flat reject
    if request.account_standing == "suspended":
        return DisputeResult(
            order_id=request.order_id,
            customer_id=request.customer_id,
            approved=False,
            requires_human_review=False,
            refund_amount=request.refund_amount,
            reasoning="Account is suspended. Refund requests from suspended accounts are rejected.",
            action="reject",
        )

    # 3. Flagged account -> always HITL, regardless of amount
    #    THIS IS THE LOGIC THE DROPPED-HANDOFF BUG BREAKS:
    #    If account_standing is missing (defaults to 'active'), this check
    #    is skipped, and a flagged customer's refund sails through.
    if request.account_standing == "flagged":
        return DisputeResult(
            order_id=request.order_id,
            customer_id=request.customer_id,
            approved=False,
            requires_human_review=True,
            refund_amount=request.refund_amount,
            reasoning=(
                f"Customer {request.customer_id} has a flagged account. "
                f"Refund of INR {request.refund_amount:,} on order {request.order_id} "
                f"requires human review regardless of amount."
            ),
            action="escalate_hitl",
        )

    # 4-5. Amount vs threshold (>= means at-threshold goes to HITL)
    if request.refund_amount >= HITL_THRESHOLD_INR:
        return DisputeResult(
            order_id=request.order_id,
            customer_id=request.customer_id,
            approved=False,
            requires_human_review=True,
            refund_amount=request.refund_amount,
            reasoning=(
                f"Refund amount INR {request.refund_amount:,} meets or exceeds the "
                f"INR {HITL_THRESHOLD_INR:,} threshold. Human review required."
            ),
            action="escalate_hitl",
        )

    # 6. Double-refund guard
    prior = _check_prior_refunds_on_order(request.order_id)
    approved_prior = [r for r in prior if r["status"] in ("approved", "executed")]
    if approved_prior:
        return DisputeResult(
            order_id=request.order_id,
            customer_id=request.customer_id,
            approved=False,
            requires_human_review=True,
            refund_amount=request.refund_amount,
            reasoning=(
                f"A prior refund on order {request.order_id} has already been "
                f"approved/executed. Possible double-refund â€” escalating to human review."
            ),
            action="escalate_hitl",
        )

    # 7. All checks passed -> approve
    return DisputeResult(
        order_id=request.order_id,
        customer_id=request.customer_id,
        approved=True,
        requires_human_review=False,
        refund_amount=request.refund_amount,
        reasoning=(
            f"Refund of INR {request.refund_amount:,} on order {request.order_id} approved. "
            f"Amount is under threshold, account is in good standing, "
            f"no prior refunds on this order."
        ),
        action="approve",
    )


# ---------------------------------------------------------------------------
# Standalone demo â€” proves the specialist's logic without any API key or
# network call. Run: uv run python -m billing_specialist.agent
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== Billing Specialist â€” standalone demo ===")
    print(f"HITL threshold: INR {HITL_THRESHOLD_INR:,}\n")

    cases = [
        # Under threshold, active account -> approve
        DisputeRequest(
            order_id="ord_5001", customer_id="cust_1001",
            refund_amount=2499, reason="Earbuds late",
            account_standing="active",
        ),
        # Over threshold, active account -> HITL
        DisputeRequest(
            order_id="ord_9006", customer_id="cust_5005",
            refund_amount=15499, reason="Monitor cracked",
            account_standing="active",
        ),
        # Over threshold + flagged account -> HITL (flagged)
        DisputeRequest(
            order_id="ord_6002", customer_id="cust_2002",
            refund_amount=8990, reason="Wrong model shipped",
            account_standing="flagged",
        ),
        # THE DROPPED-HANDOFF BUG: same dispute, but account_standing omitted
        # (defaults to 'active') -> WRONGLY approved if amount were under threshold,
        # or escalates only on amount (misses the flagged-account check)
        DisputeRequest(
            order_id="ord_6002", customer_id="cust_2002",
            refund_amount=8990, reason="Wrong model shipped",
            # account_standing defaults to 'active' â€” THE BUG
        ),
        # Suspended account -> reject
        DisputeRequest(
            order_id="ord_7004", customer_id="cust_3003",
            refund_amount=4999, reason="Cancelled keyboard",
            account_standing="suspended",
        ),
    ]

    for i, req in enumerate(cases, 1):
        result = resolve_dispute(req)
        print(f"Case {i}: {req.order_id} / {req.customer_id} / "
              f"INR {req.refund_amount:,} / standing={req.account_standing}")
        print(f"  -> action={result.action}, approved={result.approved}, "
              f"hitl={result.requires_human_review}")
        print(f"  -> {result.reasoning}")
        print()

