"""
Phase 5 — HITL gate tests (spec §2.5, SPEC-HITL).

Local, offline, no AWS: the gate logic runs against InMemoryApprovalStore and an
injected clock, so every property is deterministic. Covers the four §5 local
acceptance criteria plus the broken-vs-fixed contrast that is the §2.5 CWP.

    uv run python test_hitl_gate.py
"""

from __future__ import annotations

import sys

from decisions import RefundDecision
from hitl import (
    ApprovalStatus,
    HITLGate,
    LiveRefundState,
)
from mock_data import REFUND_REQUESTS


def _decision(request_id: str) -> tuple[RefundDecision, str]:
    r = REFUND_REQUESTS[request_id]
    return (
        RefundDecision(
            order_id=r["order_id"], customer_id=r["customer_id"], amount=r["amount"],
            action="escalate_hitl", requires_human=True, reasoning=r["reason"],
        ),
        request_id,
    )


def _drive_double_refund(correlate_by: str) -> int:
    """req_r004 then req_r005 (both ord_6002) → how many execute?"""
    gate = HITLGate(correlate_by=correlate_by)
    for rid in ("req_r004", "req_r005"):
        decision, _ = _decision(rid)
        gate.request_approval(decision, request_id=rid, order_status="delivered")
    executed = 0
    for rid in ("req_r004", "req_r005"):
        decision, _ = _decision(rid)
        outcome = gate.resume(decision.order_id, rid, "approve")
        executed += int(outcome.executed)
    return executed


def test_double_refund_fixed_executes_once() -> bool:
    """THE §2.5 exit gate: resource-keyed gate pays out once on ord_6002."""
    print("=== double-refund: resource-keyed (fixed) executes once ===")
    n = _drive_double_refund("order")
    ok = n == 1
    print(f"  [{'PASS' if ok else 'FAIL'}] order-keyed: {n} refund(s) executed (want 1)")
    return ok


def test_double_refund_bug_reproduces() -> bool:
    """The bug itself, asserted so it can't silently 'get fixed' and rot: the
    ticket-keyed gate pays out TWICE on one order."""
    print("\n=== double-refund: ticket-keyed (the CWP) executes twice ===")
    n = _drive_double_refund("request")
    ok = n == 2
    print(f"  [{'PASS' if ok else 'FAIL'}] request-keyed: {n} refund(s) executed (bug pays 2)")
    return ok


def test_revalidation_aborts_on_drift() -> bool:
    """§5.3: approve, then the order changes under the pause; resume must abort
    with a structured state_changed outcome and NOT execute."""
    print("\n=== re-validation aborts when live state drifts ===")
    gate = HITLGate(correlate_by="order")
    decision, rid = _decision("req_r004")   # ord_6002, 8990, snapshot status 'delivered'
    gate.request_approval(decision, request_id=rid, order_status="delivered")

    # Live state now differs from the parked snapshot (order got cancelled).
    def drifted_refetch(order_id, request_id):
        return LiveRefundState(refund_amount=8990, order_status="cancelled")

    outcome = gate.resume(decision.order_id, rid, "approve", refetch=drifted_refetch)
    ok = (not outcome.executed
          and outcome.escalation is not None
          and outcome.escalation.code == "state_changed")
    print(f"  [{'PASS' if ok else 'FAIL'}] executed={outcome.executed}, "
          f"escalation={outcome.escalation.code if outcome.escalation else None}")
    return ok


def test_revalidation_allows_when_unchanged() -> bool:
    """Control for the above: if nothing drifted, the same approve DOES execute —
    so the abort is caused by drift, not by re-validation always blocking."""
    print("\n=== re-validation allows when live state is unchanged ===")
    gate = HITLGate(correlate_by="order")
    decision, rid = _decision("req_r004")
    gate.request_approval(decision, request_id=rid, order_status="delivered")

    def stable_refetch(order_id, request_id):
        return LiveRefundState(refund_amount=8990, order_status="delivered")

    outcome = gate.resume(decision.order_id, rid, "approve", refetch=stable_refetch)
    ok = outcome.executed and outcome.status == ApprovalStatus.executed
    print(f"  [{'PASS' if ok else 'FAIL'}] executed={outcome.executed}")
    return ok


def test_expired_cannot_execute() -> bool:
    """§5.5: a pending that sat past its window becomes expired and cannot
    execute. Driven by a fake clock so it's deterministic."""
    print("\n=== expiry: a stale pending cannot execute ===")
    now = {"t": 1000.0}
    gate = HITLGate(correlate_by="order", ttl_seconds=60, clock=lambda: now["t"])
    decision, rid = _decision("req_r004")
    gate.request_approval(decision, request_id=rid, order_status="delivered")

    now["t"] += 61  # advance past the 60s window

    def stable_refetch(order_id, request_id):
        return LiveRefundState(refund_amount=8990, order_status="delivered")

    outcome = gate.resume(decision.order_id, rid, "approve", refetch=stable_refetch)
    ok = (not outcome.executed) and outcome.status == ApprovalStatus.expired
    print(f"  [{'PASS' if ok else 'FAIL'}] executed={outcome.executed}, status={outcome.status.value}")
    return ok


def test_replay_of_same_approval_refused() -> bool:
    """A replayed duplicate approval on an already-executed order is refused with
    double_refund — covers the 'replayed duplicate approval call' case in §5.6."""
    print("\n=== replay of an executed approval is refused ===")
    gate = HITLGate(correlate_by="order")
    decision, rid = _decision("req_r004")
    gate.request_approval(decision, request_id=rid, order_status="delivered")

    def stable_refetch(order_id, request_id):
        return LiveRefundState(refund_amount=8990, order_status="delivered")

    first = gate.resume(decision.order_id, rid, "approve", refetch=stable_refetch)
    second = gate.resume(decision.order_id, rid, "approve", refetch=stable_refetch)
    ok = (first.executed
          and not second.executed
          and second.escalation is not None
          and second.escalation.code == "double_refund")
    print(f"  [{'PASS' if ok else 'FAIL'}] first executed={first.executed}, "
          f"replay executed={second.executed}, "
          f"replay escalation={second.escalation.code if second.escalation else None}")
    return ok


def main() -> int:
    results = [
        test_double_refund_fixed_executes_once(),
        test_double_refund_bug_reproduces(),
        test_revalidation_aborts_on_drift(),
        test_revalidation_allows_when_unchanged(),
        test_expired_cannot_execute(),
        test_replay_of_same_approval_refused(),
    ]
    ok = all(results)
    print("\n" + ("All HITL gate tests passed." if ok
                  else "HITL gate TESTS FAILED — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
