"""
Phase 5 — policy boundary tests (spec §2.6, SPEC-POLICY).

Local, offline, no AWS: the runtime gate is the pure Cedar-shaped evaluator in
policy.py, so these assert the real fixtures land on the right side of the
threshold and that the boundary agrees with the specialist's existing `>=`
escalation semantics (the consistency risk called out in the Phase 5 plan).

    uv run python test_policy_boundary.py

Requires the same mock_data the specialist reads (no DB, no API key).
"""

from __future__ import annotations

import sys

from billing_specialist.agent import HITL_THRESHOLD_INR
from mock_data import REFUND_REQUESTS
from policy import authorize_refund


def _amount(request_id: str) -> tuple[str, str, int]:
    """Pull the real (customer_id, order_id, amount) for a seeded refund request,
    so the test is coupled to the actual fixtures, not hand-typed numbers."""
    r = REFUND_REQUESTS[request_id]
    return r["customer_id"], r["order_id"], r["amount"]


# (label, request_id, expected outcome) — the exit-gate fixtures from the spec.
CASES: list[tuple[str, str, str]] = [
    ("req_r001  INR 2,499  under threshold        -> allow",          "req_r001", "allow"),
    ("req_r002  INR 15,499 over threshold         -> needs_approval", "req_r002", "needs_approval"),
    ("req_r004  INR 8,990  over threshold         -> needs_approval", "req_r004", "needs_approval"),
    ("req_r003  INR 4,999  just under (boundary)  -> allow",          "req_r003", "allow"),
    ("req_r007  INR 5,000  exactly at (>= -> HITL)-> needs_approval", "req_r007", "needs_approval"),
    ("req_r008  INR 599    under threshold        -> allow",          "req_r008", "allow"),
]


def test_threshold_decisions() -> bool:
    print("=== policy boundary decisions on the real fixtures ===")
    ok = True
    for label, request_id, expected in CASES:
        customer_id, order_id, amount = _amount(request_id)
        got = authorize_refund(customer_id, order_id, amount)
        passed = got == expected
        ok &= passed
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label}")
        if not passed:
            print(f"         expected {expected!r}, got {got!r}")
    return ok


def test_exactly_at_threshold_routes_to_hitl() -> bool:
    """The documented boundary decision: exactly ₹5,000 is NOT auto-approved, it
    routes to HITL — matching the specialist's `>= 5000 -> escalate`."""
    print("\n=== boundary: exactly-at-threshold (>= vs >) ===")
    at = authorize_refund("cust_x", "ord_x", HITL_THRESHOLD_INR)
    just_under = authorize_refund("cust_x", "ord_x", HITL_THRESHOLD_INR - 1)
    ok = at == "needs_approval" and just_under == "allow"
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] INR {HITL_THRESHOLD_INR:,} -> {at}; "
          f"INR {HITL_THRESHOLD_INR - 1:,} -> {just_under}")
    return ok


def test_agrees_with_specialist_semantics() -> bool:
    """Cross-check: for every amount around the threshold, the policy boundary's
    'needs_approval' must line up with the specialist's escalate rule
    (amount >= threshold). This is the consistency the Phase 5 plan flagged: if
    these two ever disagreed on a value, the exactly-5,000 case would flip
    depending on which layer looked at it."""
    print("\n=== policy boundary agrees with specialist's `>=` escalation ===")
    ok = True
    for amt in range(HITL_THRESHOLD_INR - 3, HITL_THRESHOLD_INR + 4):
        policy_says_hitl = authorize_refund("c", "o", amt) == "needs_approval"
        specialist_escalates = amt >= HITL_THRESHOLD_INR
        agree = policy_says_hitl == specialist_escalates
        ok &= agree
        if not agree:
            print(f"  [FAIL] INR {amt:,}: policy_hitl={policy_says_hitl} "
                  f"specialist_escalate={specialist_escalates}")
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] both layers escalate iff amount >= INR {HITL_THRESHOLD_INR:,}")
    return ok


def main() -> int:
    ok = test_threshold_decisions()
    ok &= test_exactly_at_threshold_routes_to_hitl()
    ok &= test_agrees_with_specialist_semantics()
    print("\n" + ("All policy boundary tests passed." if ok
                  else "Policy boundary TESTS FAILED — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
