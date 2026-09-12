"""
OrderCare — end-to-end demo runner (Phase 9).

Runs the refund lifecycle + PII egress on the REAL seeded data, deterministically
and offline (no Bedrock / no API key / no AWS). Built for the video: repeatable,
no live typing, and it prints the ACTUAL outcome of each scenario rather than a
hardcoded guess, so what you narrate is what the code really did.

    uv run python demo_scenarios.py

Covers: policy threshold routing, the ₹5,000 boundary (< vs >=), the flagged-
account flip, HITL approve/reject, the double-refund guard, re-validation drift,
and PII masking at the egresses. Cross-customer harness-blocking and retrieval-gap
are agent-level (they need the LLM path) and are demoed via the CLI + covered by
their own suites; this runner is the deterministic core.
"""

from __future__ import annotations

import time

from billing_specialist.schemas import DisputeRequest
from billing_specialist.settlement import resume_settlement, settle_dispute
from decisions import RefundDecision
from hitl import HITLGate, InMemoryApprovalStore, LiveRefundState
from mock_data import ACCOUNTS, CUSTOMER_PII, REFUND_REQUESTS
from pii import mask


def _hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def _req(rid: str) -> DisputeRequest:
    r = REFUND_REQUESTS[rid]
    return DisputeRequest(
        order_id=r["order_id"], customer_id=r["customer_id"],
        refund_amount=r["amount"], reason=r["reason"],
        account_standing=ACCOUNTS[r["customer_id"]]["standing"],
    )


def _fresh_gate() -> HITLGate:
    # A fresh, in-memory, order-keyed gate so every scenario is independent and
    # nothing touches AWS regardless of environment.
    return HITLGate(store=InMemoryApprovalStore(), correlate_by="order")


def _line(rid: str, res) -> None:
    r = REFUND_REQUESTS[rid]
    print(f"  {rid}  order={r['order_id']}  ₹{r['amount']:<6}  "
          f"standing={ACCOUNTS[r['customer_id']]['standing']:<9}"
          f"->  action={res.action:<13} settlement={res.settlement_outcome} "
          f"policy={res.policy_outcome}")


def scenario_policy_routing() -> None:
    _hr("A. Policy threshold routing (settle_dispute, fresh gate each)")
    print("  Threshold = ₹5,000. Watch the boundary and the flagged-account flip.\n")
    for rid in ("req_r001", "req_r003", "req_r007", "req_r002", "req_r008"):
        res = settle_dispute(_req(rid), gate=_fresh_gate())
        _line(rid, res)
    print("\n  Reading: r003 (₹4,999) allows but r007 (₹5,000) escalates -> the gate")
    print("  is '>= 5000', not '> 5000'. r008 (₹599) would auto-approve on amount")
    print("  alone, but the FLAGGED standing routes it to a human (dropped-handoff CWP).")


def scenario_hitl_lifecycle() -> None:
    _hr("B. HITL lifecycle — approve vs reject (over-threshold req_r004, ₹8,990)")
    # Approve path
    g = _fresh_gate()
    paused = settle_dispute(_req("req_r004"), gate=g)
    print(f"  1) settle req_r004        -> {paused.settlement_outcome} (paused, awaiting human)")
    approved = resume_settlement("ord_6002", "approve", gate=g)
    print(f"  2) human APPROVE          -> {approved.settlement_outcome} (executed={approved.approved})")
    # Reject path (fresh gate)
    g2 = _fresh_gate()
    settle_dispute(_req("req_r004"), gate=g2)
    rejected = resume_settlement("ord_6002", "reject", gate=g2)
    print(f"  3) (fresh) human REJECT   -> {rejected.settlement_outcome} (executed={rejected.approved})")


def scenario_double_refund() -> None:
    _hr("C. Double-refund guard — req_r004 + req_r005 BOTH target ord_6002")
    g = _fresh_gate()  # ONE gate: the resource ledger is shared, as in production
    executed = 0
    settle_dispute(_req("req_r004"), gate=g)
    r1 = resume_settlement("ord_6002", "approve", gate=g)
    executed += 1 if r1.approved else 0
    print(f"  req_r004 approve -> {r1.settlement_outcome} (executed={r1.approved})")
    settle_dispute(_req("req_r005"), gate=g)   # second ticket, same order
    r2 = resume_settlement("ord_6002", "approve", gate=g)
    executed += 1 if r2.approved else 0
    print(f"  req_r005 approve -> {r2.settlement_outcome} (executed={r2.approved})")
    print(f"\n  Refunds executed on ord_6002: {executed}")
    assert executed == 1, f"DOUBLE REFUND: expected 1 execution, got {executed}"
    print("  PASS: resource-keyed gate paid out exactly once on the order.")


def scenario_revalidation_drift() -> None:
    _hr("D. Re-validation drift — order state changes while a refund is paused")
    g = _fresh_gate()
    verdict = settle_dispute(_req("req_r004"), gate=g)  # pauses; snapshot status 'delivered'
    print(f"  paused req_r004 (snapshot order_status='{ _order_status_at_pause() }')")
    # Human approves LATER, but live order status has drifted to 'cancelled':
    outcome = g.resume(
        "ord_6002", "", "approve",
        refetch=lambda oid, rid: LiveRefundState(refund_amount=8990, order_status="cancelled"),
    )
    code = outcome.escalation.code if outcome.escalation else None
    print(f"  human APPROVE, but live status drifted -> executed={outcome.executed}, abort={code}")
    assert not outcome.executed and code == "state_changed", "drift was not caught!"
    print("  PASS: stale approval refused; not executed.")


def _order_status_at_pause() -> str:
    from mock_data import ORDERS
    return ORDERS["ord_6002"]["status"]


def scenario_pii_egress() -> None:
    _hr("E. PII masking at the egresses (Layer 1; add PII_GUARDRAIL_ID for Layer 2)")
    p = CUSTOMER_PII["cust_1001"]
    reply = (f"Thanks {p['name']}. We'll call {p['phone']} about the refund. "
             f"On file: Aadhaar {p['aadhaar']}, PAN {p['pan']}.")
    masked_reply = mask(reply)
    print("  Customer reply:")
    print("    IN :", reply)
    print("    OUT:", masked_reply)
    assert p["aadhaar"] not in masked_reply and p["pan"] not in masked_reply

    err = REFUND_REQUESTS["req_r003"]["reason"]   # embeds +91 90000 12345
    masked_err = mask(err)
    print("\n  Tool-error string (req_r003 reason):")
    print("    IN :", err)
    print("    OUT:", masked_err)
    assert "90000 12345" not in masked_err
    print("\n  PASS: Aadhaar, PAN, and the error-path phone are all redacted.")


def main() -> int:
    print("OrderCare demo — deterministic refund lifecycle + PII egress")
    print(f"(offline, no AWS; run at {time.strftime('%Y-%m-%d %H:%M:%S')})")
    scenario_policy_routing()
    scenario_hitl_lifecycle()
    scenario_double_refund()
    scenario_revalidation_drift()
    scenario_pii_egress()
    _hr("All demo scenarios ran.")
    print("  Safety-critical asserts (double-refund, drift, PII) all held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
