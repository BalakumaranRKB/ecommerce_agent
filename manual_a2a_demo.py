"""
Manual, narrated A2A round-trip -- run this AFTER the specialist server is up
on port 5001. This plays the role of "main agent sending a handoff," but
outside agent.py, so you can watch each step of the protocol in isolation.

    uv run python manual_a2a_demo.py
"""

from __future__ import annotations

import asyncio

from billing_specialist.schemas import DisputeRequest
from a2a_client import send_dispute_to_specialist


async def main():
    print("=" * 70)
    print("STEP 1 -- Building the DisputeRequest (the state that must cross)")
    print("=" * 70)
    # The FIXED case: account_standing is explicitly included.
    request = DisputeRequest(
        order_id="ord_6002",
        customer_id="cust_2002",
        refund_amount=8990,
        reason="Wrong model shipped",
        account_standing="flagged",  # <-- present = fixed handoff
    )
    print(request)
    print("\nSerialized to JSON (this is what actually crosses the wire):")
    print(" ", request.to_json())

    print("\n" + "=" * 70)
    print("STEP 2 -- Sending over A2A to http://localhost:5001")
    print("=" * 70)
    print("(This is a real HTTP call to the OTHER process. If that process")
    print(" were down, this would fail here -- the honest cost of a handoff.)")

    result = await send_dispute_to_specialist(request)

    print("\n" + "=" * 70)
    print("STEP 3 -- DisputeResult received back (typed, deserialized)")
    print("=" * 70)
    print(result)
    print(f"\n  action                 = {result.action}")
    print(f"  approved               = {result.approved}")
    print(f"  requires_human_review  = {result.requires_human_review}")
    print(f"  reasoning              = {result.reasoning}")

    print("\n" + "=" * 70)
    print("Now compare: same order, but account_standing OMITTED (the bug)")
    print("=" * 70)
    broken_request = DisputeRequest(
        order_id="ord_6002",
        customer_id="cust_2002",
        refund_amount=8990,
        reason="Wrong model shipped",
        # account_standing NOT set -> defaults to "active" -> THE BUG
    )
    print("Serialized JSON (notice: no 'flagged' anywhere):")
    print(" ", broken_request.to_json())

    broken_result = await send_dispute_to_specialist(broken_request)
    print(f"\n  action                 = {broken_result.action}")
    print(f"  approved               = {broken_result.approved}")
    print(f"  requires_human_review  = {broken_result.requires_human_review}")
    print(f"  reasoning              = {broken_result.reasoning}")

    print("\n" + "=" * 70)
    print("SIDE BY SIDE")
    print("=" * 70)
    print(f"  WITH account_standing='flagged'   -> action={result.action}, hitl={result.requires_human_review}")
    print(f"  WITHOUT account_standing (bug)    -> action={broken_result.action}, hitl={broken_result.requires_human_review}")


if __name__ == "__main__":
    asyncio.run(main())
