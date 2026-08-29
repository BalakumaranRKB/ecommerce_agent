"""
LIVE HITL demo against AgentCore Memory (Phase 4 of the video / spec §2.5).

Pauses an over-threshold refund to the REAL AgentCore Memory resource, waits so
you can show the pending record in the AWS console, then resumes on your keypress
and shows it execute / reject / abort-on-drift. One process, one gate, so the
pause is still live when you come back from the console.

Prereqs: AWS creds + the memory id (defaults to the provisioned one).
    export HITL_MEMORY_ID=ordercare_hitl_approvals-fh6dj022G7    # optional; this is the default

Modes:
    uv run python demo_hitl_live.py                 # req_r004 (₹8,990) -> pause -> APPROVE -> executes
    uv run python demo_hitl_live.py --action reject # -> human REJECT (the "rejected live" beat)
    uv run python demo_hitl_live.py --drift         # -> live order status drifts -> re-validation ABORTS
    uv run python demo_hitl_live.py --request req_r002   # the ₹15,499 monitor instead

NOTE ON THE "APPROVAL": the console shows the durable PENDING record persisted to
AgentCore Memory. The approve/reject decision is issued here via the resume path
(that is the reviewer action) — resume re-reads the record FROM Memory, re-validates
live order state, and only then executes.
"""

from __future__ import annotations

import argparse
import os

from billing_specialist.schemas import DisputeRequest
from billing_specialist.settlement import resume_settlement, settle_dispute
from hitl import AgentCoreMemoryApprovalStore, HITLGate, LiveRefundState
from mock_data import ACCOUNTS, ORDERS, REFUND_REQUESTS

MEM_ID = os.getenv("HITL_MEMORY_ID") or "ordercare_hitl_approvals-fh6dj022G7"
REGION = os.getenv("AWS_REGION", "ap-south-1")


def _dispute(rid: str) -> DisputeRequest:
    r = REFUND_REQUESTS[rid]
    return DisputeRequest(
        order_id=r["order_id"], customer_id=r["customer_id"],
        refund_amount=r["amount"], reason=r["reason"],
        account_standing=ACCOUNTS[r["customer_id"]]["standing"],
    )


def _live_gate() -> HITLGate:
    store = AgentCoreMemoryApprovalStore(memory_id=MEM_ID, region_name=REGION)
    return HITLGate(store=store, correlate_by="order")


def main() -> int:
    ap = argparse.ArgumentParser(description="Live HITL pause/resume on AgentCore Memory.")
    ap.add_argument("--request", default="req_r004", help="refund request id (default req_r004)")
    ap.add_argument("--action", choices=["approve", "reject"], default="approve")
    ap.add_argument("--drift", action="store_true",
                    help="simulate live order-status drift before resume (re-validation aborts)")
    args = ap.parse_args()

    req = _dispute(args.request)
    order_id = req.order_id
    print(f"Live HITL demo  |  memory={MEM_ID}  region={REGION}")
    print(f"Request {args.request}: order={order_id}, amount=₹{req.refund_amount}, "
          f"standing={req.account_standing}\n")

    gate = _live_gate()

    # 1) PAUSE — writes a durable PendingApproval to AgentCore Memory.
    print("[1] Handing the over-threshold refund to the gate ...")
    result = settle_dispute(req, gate=gate)
    print(f"    -> settlement_outcome = {result.settlement_outcome} "
          f"(policy={result.policy_outcome})")
    if result.settlement_outcome != "pending_approval":
        print("    (not a pending case — pick an over-threshold or flagged request)")
        return 1

    # Prove it's actually in Memory by reading it back from the live store.
    rec = gate.store.get(order_id)
    if rec is not None:
        print(f"    -> persisted to Memory: key={rec.key!r}, status={rec.status.value}, "
              f"amount=₹{rec.snapshot.amount}, snapshot_status={rec.snapshot.status_snapshot!r}")

    print("\n[2] >>> Open the AgentCore Memory console now:")
    print(f"        Bedrock AgentCore -> Memory -> {MEM_ID}")
    print("        You should see the pending approval event for this order.")
    input("\n    Press Enter to issue the reviewer decision and resume ...")

    # 2) RESUME — re-reads from Memory, re-validates live state, then acts.
    print(f"\n[3] Reviewer action: {args.action.upper()} ...")
    if args.drift:
        # Show re-validation: pretend the live order was cancelled while paused.
        print("    (simulating live order-status drift: 'delivered' -> 'cancelled')")
        outcome = gate.resume(
            order_id, "", "approve",
            refetch=lambda oid, rid: LiveRefundState(
                refund_amount=rec.snapshot.requested_refund, order_status="cancelled"),
        )
        code = outcome.escalation.code if outcome.escalation else None
        print(f"    -> executed={outcome.executed}, abort_code={code}")
        print("    -> re-validation refused a stale approval." if not outcome.executed
              else "    -> (unexpected) executed despite drift")
        return 0

    final = resume_settlement(order_id, args.action, gate=gate)
    print(f"    -> settlement_outcome = {final.settlement_outcome} "
          f"(executed={final.approved})")

    # Read the record back once more: its status is now durable in Memory too.
    rec2 = gate.store.get(order_id)
    if rec2 is not None:
        print(f"    -> Memory record now: status={rec2.status.value}")
    print("\nDone. Point the camera back at the console to show the updated state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
