"""
LIVE A2A handoff demo — the dropped-handoff CWP, broken vs fixed (video Part 1).

Starts the billing specialist as a real subprocess (a genuine HTTP A2A boundary
on :5001), then sends the SAME refund ticket across the handoff TWICE:

  BROKEN: account_standing dropped (defaults to "active")  -> specialist APPROVES
  FIXED : account_standing="flagged" crosses the boundary  -> specialist ESCALATES

Same ticket, same code, one field of state — that field is the §2.1 CWP. The
ticket is req_r008 (ord_6003, ₹599, flagged cust_2002): the amount alone is under
the ₹5,000 line, so account_standing is the ONLY thing that should route it to a
human. Drop it and the specialist confidently approves a refund it should have
escalated.

    uv run python demo_handoff.py

No LLM/API key needed. The specialist reads mock_data (falls back to it if no
Postgres, per the Phase 3 fix), so this runs standalone. Port 5001 must be free.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request

from billing_specialist.schemas import DisputeRequest, DisputeResult
from mock_data import REFUND_REQUESTS

PORT = 5001
REASON = REFUND_REQUESTS["req_r008"]["reason"]


def _wait_ready(timeout: float = 20.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # 404/405 still means it's listening
        except Exception:
            time.sleep(0.5)
    return False


def _send(request: DisputeRequest) -> DisputeResult:
    from a2a_client import send_dispute_sync
    return send_dispute_sync(request)


def main() -> int:
    print("=== LIVE A2A handoff: dropped-handoff CWP, broken vs fixed ===")
    print(f"    ticket: ord_6003, ₹599, customer cust_2002 (FLAGGED)")
    print(f"    reason: {REASON!r}\n")
    print("    starting billing specialist on :5001 ...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "billing_specialist.a2a_server"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        if not _wait_ready():
            print("    [FAIL] specialist did not come up (is :5001 free?)")
            return 1
        print("    specialist ready.\n")

        # BROKEN — account_standing dropped across the handoff (defaults to "active").
        broken_req = DisputeRequest(
            order_id="ord_6003", customer_id="cust_2002", refund_amount=599,
            reason=REASON, account_standing="active",  # <- the dropped state
        )
        broken = _send(broken_req)
        print("  BROKEN  (standing dropped -> 'active'):")
        print(f"     action={broken.action}  requires_human={broken.requires_human_review}")
        print(f"     reasoning: {broken.reasoning}\n")

        # FIXED — the real flagged standing crosses the boundary.
        fixed_req = DisputeRequest(
            order_id="ord_6003", customer_id="cust_2002", refund_amount=599,
            reason=REASON, account_standing="flagged",  # <- state preserved
        )
        fixed = _send(fixed_req)
        print("  FIXED   (standing 'flagged' preserved):")
        print(f"     action={fixed.action}  requires_human={fixed.requires_human_review}")
        print(f"     reasoning: {fixed.reasoning}\n")

        flipped = broken.action != fixed.action
        print("  " + ("=" * 66))
        if flipped:
            print(f"  CWP shown: one dropped field flips the verdict "
                  f"{broken.action} -> {fixed.action}.")
            print("  The amount alone (₹599 < ₹5,000) would auto-approve; only the")
            print("  flagged standing routes it to a human. Drop it and the refund")
            print("  is confidently, wrongly approved.")
        else:
            print(f"  (Both returned {broken.action} — expected a flip; check "
                  f"resolve_dispute rules / order status.)")
        return 0 if flipped else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
