"""
Unit tests for the billing specialist's decision logic — resolve_dispute().

Deterministic, offline: no network, no A2A server, no API key. Just the rulebook
against constructed DisputeRequests, so every decision is reproducible. This is
the specialist's brain tested in isolation; the A2A transport around it is tested
separately in test_a2a_roundtrip.py.

    uv run python -m billing_specialist.test_resolve_dispute

Requires: docker compose up + `uv run python ingest.py` already run, because
resolve_dispute() reads ORDERS/ACCOUNTS/REFUND_REQUESTS through mock_data (the
specialist's own data surface). No LLM or API key needed.

Each case is (label, DisputeRequest, expected_action, expected_hitl). The three
core branches plus the dropped-standing FLIP are covered — the flip is the CWP
from docs/dropped-handoff-found.md, asserted here as the pair that must differ.
"""

from __future__ import annotations

import sys

from billing_specialist.agent import resolve_dispute
from billing_specialist.schemas import DisputeRequest


# (label, request, expected_action, expected_requires_human_review)
CASES: list[tuple[str, DisputeRequest, str, bool]] = [
    # ---- Branch 1: flagged account -> escalate regardless of amount ----
    (
        "flagged account, under threshold -> escalate",
        DisputeRequest(
            order_id="ord_6003", customer_id="cust_2002",
            refund_amount=599, reason="USB-C cable dead",
            account_standing="flagged",
        ),
        "escalate_hitl", True,
    ),
    # ---- Branch 2: suspended account -> reject ----
    (
        "suspended account -> reject",
        DisputeRequest(
            order_id="ord_7004", customer_id="cust_3003",
            refund_amount=4999, reason="Cancelled before shipping",
            account_standing="suspended",
        ),
        "reject", False,
    ),
    # ---- Branch 3: normal active account, under threshold -> approve ----
    (
        "active account, under threshold -> approve",
        DisputeRequest(
            order_id="ord_5001", customer_id="cust_1001",
            refund_amount=2499, reason="Earbuds arrived late",
            account_standing="active",
        ),
        "approve", False,
    ),
    # ---- Branch 4: over threshold -> escalate even for an active account ----
    (
        "active account, over threshold -> escalate",
        DisputeRequest(
            order_id="ord_9006", customer_id="cust_5005",
            refund_amount=15499, reason="Monitor cracked",
            account_standing="active",
        ),
        "escalate_hitl", True,
    ),
]


# The CWP flip: the SAME under-threshold refund on a flagged customer, sent WITH
# vs WITHOUT the account_standing. The decision must flip. This is the dropped-
# handoff finding asserted as a test (docs/dropped-handoff-found.md).
FLIP_FIXED = DisputeRequest(
    order_id="ord_6003", customer_id="cust_2002",
    refund_amount=599, reason="USB-C cable dead",
    account_standing="flagged",          # standing crosses -> correct
)
FLIP_BROKEN = DisputeRequest(
    order_id="ord_6003", customer_id="cust_2002",
    refund_amount=599, reason="USB-C cable dead",
    # account_standing omitted -> schema default "active" -> the bug
)


def run_cases() -> bool:
    ok = True
    print("=== resolve_dispute() decision branches ===")
    for label, request, want_action, want_hitl in CASES:
        result = resolve_dispute(request)
        passed = result.action == want_action and result.requires_human_review == want_hitl
        ok &= passed
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label}")
        if not passed:
            print(f"         expected action={want_action}, hitl={want_hitl}")
            print(f"         got      action={result.action}, hitl={result.requires_human_review}")
    return ok


def run_flip() -> bool:
    print("\n=== dropped-handoff CWP: the decision must FLIP ===")
    fixed = resolve_dispute(FLIP_FIXED)
    broken = resolve_dispute(FLIP_BROKEN)
    print(f"  WITH  standing='flagged' -> action={fixed.action}, hitl={fixed.requires_human_review}")
    print(f"  WITHOUT standing (bug)   -> action={broken.action}, hitl={broken.requires_human_review}")

    ok = True
    # The fixed path must escalate; the broken path must (wrongly) approve.
    ok &= fixed.action == "escalate_hitl" and fixed.requires_human_review is True
    ok &= broken.action == "approve" and broken.approved is True
    # And they must genuinely differ — that difference IS the finding.
    ok &= fixed.action != broken.action
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] dropping account_standing flips {broken.action} -> {fixed.action}")
    return ok


def main() -> int:
    ok = run_cases()
    ok &= run_flip()
    print("\n" + ("All resolve_dispute() tests passed." if ok
                  else "resolve_dispute() TESTS FAILED — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
