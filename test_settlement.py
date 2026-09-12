"""
Phase 5 — settlement seam tests (spec §2.5 + §2.6, Step 3 wiring).

Exercises billing_specialist/settlement.py directly (no A2A server, no API key):
verdict -> policy authorization -> execute / pause / reject, and the human
resume path. Each test uses a FRESH gate so state doesn't leak between cases.

    uv run python test_settlement.py
"""

from __future__ import annotations

import sys

from billing_specialist.schemas import DisputeRequest
from billing_specialist.settlement import settle_dispute, resume_settlement
from hitl import HITLGate


def _gate() -> HITLGate:
    return HITLGate(correlate_by="order")


def test_under_threshold_executes() -> bool:
    """Approve verdict + policy allow -> the refund actually executes."""
    print("=== under-threshold approve -> executed ===")
    g = _gate()
    req = DisputeRequest(order_id="ord_5001", customer_id="cust_1001",
                         refund_amount=2499, reason="Earbuds late", account_standing="active")
    r = settle_dispute(req, gate=g)
    ok = (r.action == "approve" and r.approved is True
          and r.settlement_outcome == "executed" and r.policy_outcome == "allow")
    print(f"  [{'PASS' if ok else 'FAIL'}] action={r.action}, settlement={r.settlement_outcome}, "
          f"policy={r.policy_outcome}")
    return ok


def test_over_threshold_pauses() -> bool:
    """Over-threshold verdict -> pause for a human, nothing executed."""
    print("\n=== over-threshold -> pending_approval (paused) ===")
    g = _gate()
    req = DisputeRequest(order_id="ord_9006", customer_id="cust_5005",
                         refund_amount=15499, reason="Monitor cracked", account_standing="active")
    r = settle_dispute(req, gate=g)
    ok = (r.action == "escalate_hitl" and r.requires_human_review is True
          and r.settlement_outcome == "pending_approval")
    print(f"  [{'PASS' if ok else 'FAIL'}] action={r.action}, settlement={r.settlement_outcome}")
    return ok


def test_suspended_rejects() -> bool:
    """Reject verdict -> rejected, no execution."""
    print("\n=== suspended account -> rejected ===")
    g = _gate()
    req = DisputeRequest(order_id="ord_7004", customer_id="cust_3003",
                         refund_amount=4999, reason="Cancelled", account_standing="suspended")
    r = settle_dispute(req, gate=g)
    ok = r.action == "reject" and r.settlement_outcome == "rejected"
    print(f"  [{'PASS' if ok else 'FAIL'}] action={r.action}, settlement={r.settlement_outcome}")
    return ok


def test_pause_then_human_approve_executes() -> bool:
    """The full HITL round-trip: pause an over-threshold refund, then a human
    approves -> it executes."""
    print("\n=== pause -> human approve -> executed ===")
    g = _gate()
    req = DisputeRequest(order_id="ord_6002", customer_id="cust_2002",
                         refund_amount=8990, reason="Wrong model shipped", account_standing="active")
    paused = settle_dispute(req, gate=g)
    resumed = resume_settlement("ord_6002", "approve", gate=g)
    ok = (paused.settlement_outcome == "pending_approval"
          and resumed.settlement_outcome == "executed" and resumed.approved is True)
    print(f"  [{'PASS' if ok else 'FAIL'}] paused={paused.settlement_outcome}, "
          f"resumed={resumed.settlement_outcome}")
    return ok


def test_pause_then_human_reject() -> bool:
    """A human rejects a paused refund -> rejected, not executed."""
    print("\n=== pause -> human reject -> rejected ===")
    g = _gate()
    req = DisputeRequest(order_id="ord_6002", customer_id="cust_2002",
                         refund_amount=8990, reason="Wrong model shipped", account_standing="active")
    settle_dispute(req, gate=g)
    resumed = resume_settlement("ord_6002", "reject", gate=g)
    ok = resumed.action == "reject" and resumed.settlement_outcome == "rejected"
    print(f"  [{'PASS' if ok else 'FAIL'}] resumed={resumed.settlement_outcome}")
    return ok


def test_double_refund_second_settle_then_resume_once() -> bool:
    """req_r004 then req_r005 both target ord_6002. Settling both pauses ONE
    approval (resource-keyed); approving it once executes once; a second resume
    on the same order is refused with duplicate_blocked."""
    print("\n=== double-refund across two settle calls -> one execution ===")
    g = _gate()
    for reason in ("Wrong model shipped", "Duplicate follow-up on same order"):
        req = DisputeRequest(order_id="ord_6002", customer_id="cust_2002",
                             refund_amount=8990, reason=reason, account_standing="active")
        settle_dispute(req, gate=g)
    first = resume_settlement("ord_6002", "approve", gate=g)
    second = resume_settlement("ord_6002", "approve", gate=g)
    ok = (first.settlement_outcome == "executed"
          and second.settlement_outcome == "duplicate_blocked")
    print(f"  [{'PASS' if ok else 'FAIL'}] first={first.settlement_outcome}, "
          f"second={second.settlement_outcome}")
    return ok


def test_resume_unknown_order() -> bool:
    """Resuming an order with no parked approval is refused cleanly."""
    print("\n=== resume with no pending approval -> rejected ===")
    g = _gate()
    r = resume_settlement("ord_does_not_exist", "approve", gate=g)
    ok = r.settlement_outcome == "rejected"
    print(f"  [{'PASS' if ok else 'FAIL'}] settlement={r.settlement_outcome}")
    return ok


def main() -> int:
    results = [
        test_under_threshold_executes(),
        test_over_threshold_pauses(),
        test_suspended_rejects(),
        test_pause_then_human_approve_executes(),
        test_pause_then_human_reject(),
        test_double_refund_second_settle_then_resume_once(),
        test_resume_unknown_order(),
    ]
    ok = all(results)
    print("\n" + ("All settlement tests passed." if ok
                  else "Settlement TESTS FAILED — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
