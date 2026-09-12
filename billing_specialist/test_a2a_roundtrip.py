"""
Integration test for the billing specialist over A2A.

Starts the specialist as a real subprocess, waits for it to listen, does ONE
genuine A2A round-trip through a2a_client.send_dispute_to_specialist(), asserts
the typed DisputeResult, and tears the process down. This is the transport tested
for real — a live HTTP boundary between two processes — as opposed to
test_resolve_dispute.py, which tests the decision logic in isolation.

    uv run python -m billing_specialist.test_a2a_roundtrip

Requires: docker compose up + `uv run python ingest.py` already run (the
specialist reads mock_data), and the a2a-sdk[http-server] deps installed. No LLM
or API key needed. Uses port 5001 — make sure no other specialist is already
running on it, or this will start a second and the test may bind-fail.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import urllib.error
import urllib.request

SPECIALIST_PORT = 5001


def _wait_ready(timeout: float = 20.0) -> bool:
    """Poll the port until the server answers (any HTTP status = listening)."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{SPECIALIST_PORT}/", timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # a 404/405 still means the server is up and listening
        except Exception:
            time.sleep(0.5)
    return False


async def _roundtrip():
    from a2a_client import send_dispute_to_specialist
    from billing_specialist.schemas import DisputeRequest

    request = DisputeRequest(
        order_id="ord_6003",
        customer_id="cust_2002",
        refund_amount=599,
        reason="USB-C cable dead",
        account_standing="flagged",
    )
    return await send_dispute_to_specialist(request)


def main() -> int:
    print("=== A2A integration round-trip ===")
    print("  starting billing specialist subprocess on port 5001...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "billing_specialist.a2a_server"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not _wait_ready():
            print("  [FAIL] specialist did not become ready within timeout")
            return 1
        print("  specialist ready; sending one dispute over A2A...")

        result = asyncio.run(_roundtrip())
        print(f"  round-trip result: order={result.order_id} "
              f"action={result.action} hitl={result.requires_human_review}")

        ok = (
            result.order_id == "ord_6003"
            and result.action == "escalate_hitl"
            and result.requires_human_review is True
        )
        if ok:
            print("  [PASS] A2A round-trip returned a typed, correct DisputeResult")
        else:
            print("  [FAIL] unexpected result from the round-trip")
        print("\n" + ("A2A integration test passed." if ok
                      else "A2A integration TEST FAILED — see above."))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
