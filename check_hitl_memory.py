"""
Does the live AgentCore Memory HITL store actually round-trip? (Phase 5 tail)

Writes one ApprovalRecord to the real Memory resource via
AgentCoreMemoryApprovalStore.put(), then reads it back with .get(). This is the
5-minute check that decides whether the "approve in HITL, watch the Memory record
change in the AWS console" demo is real, or whether the read path (Finding 4)
needs a fix first.

Needs AWS creds + the memory id:
    export HITL_MEMORY_ID=ordercare_hitl_approvals-fh6dj022G7   # or it defaults to this
    uv run python check_hitl_memory.py

On FAIL it dumps the raw shape of what list_events() returns, so we can see
whether events are dicts (get works) or model objects (get needs adapting).
"""

from __future__ import annotations

import os
import time

from decisions import PendingApproval
from hitl import AgentCoreMemoryApprovalStore, ApprovalRecord, ApprovalStatus

MEM_ID = os.getenv("HITL_MEMORY_ID") or "ordercare_hitl_approvals-fh6dj022G7"
REGION = os.getenv("AWS_REGION", "ap-south-1")


def _make_record(key: str) -> ApprovalRecord:
    snap = PendingApproval(
        order_id="ord_RTCHECK", customer_id="cust_RTCHECK", amount=8990,
        status_snapshot="delivered", requested_refund=8990,
        reasoning="round-trip check record",
    )
    return ApprovalRecord(
        key=key, order_id="ord_RTCHECK", request_id="req_RTCHECK",
        status=ApprovalStatus.pending, created_at=time.time(), snapshot=snap,
    )


def _dump_raw_events(store: AgentCoreMemoryApprovalStore) -> None:
    print("\n--- raw list_events() shape (diagnosing the read path) ---")
    try:
        events = store._mgr.list_events(
            actor_id=store._actor_id, session_id=store._session_id)
        events = list(events)
        print(f"  event count      : {len(events)}")
        if events:
            ev = events[0]
            print(f"  type(events[0])  : {type(ev).__name__}")
            print(f"  has .get()       : {hasattr(ev, 'get')}  "
                  f"(store's get()/all() assume dict-like .get('payload'))")
            print(f"  dir (sample)     : {[a for a in dir(ev) if not a.startswith('_')][:12]}")
    except Exception as e:
        print(f"  list_events() itself raised: {e!r}")


def main() -> int:
    print(f"HITL Memory round-trip check\n  memory_id={MEM_ID}\n  region={REGION}\n")
    key = f"ord_RTCHECK_{int(time.time())}"

    try:
        store = AgentCoreMemoryApprovalStore(memory_id=MEM_ID, region_name=REGION)
    except Exception as e:
        print(f"FAIL: could not construct store: {e!r}")
        print("  -> check AWS creds / region / that the memory_id exists.")
        return 1

    # WRITE
    try:
        store.put(_make_record(key))
        print(f"WRITE ok: put a record with key={key!r}")
    except Exception as e:
        print(f"FAIL on put(): {e!r}")
        _dump_raw_events(store)
        return 1

    # AgentCore Memory can be eventually consistent; give it a moment.
    time.sleep(3)

    # READ
    try:
        got = store.get(key)
    except Exception as e:
        print(f"FAIL on get() (raised): {e!r}")
        _dump_raw_events(store)
        return 1

    if got is None:
        print("FAIL on get(): wrote a record but read back None.")
        print("  This is the Finding-4 read path: get() didn't find what put() wrote.")
        _dump_raw_events(store)
        return 1

    ok = (got.key == key and got.snapshot.amount == 8990
          and got.status == ApprovalStatus.pending)
    if ok:
        print(f"READ ok: round-tripped key={got.key!r}, amount={got.snapshot.amount}, "
              f"status={got.status.value}")
        print("\nPASS: the live AgentCore Memory HITL store round-trips.")
        print("  -> the 'approve in HITL, record updates in the console' demo is REAL.")
        return 0

    print(f"FAIL: read a record but it didn't match what was written: {got!r}")
    _dump_raw_events(store)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
