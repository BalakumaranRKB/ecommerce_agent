"""
Phase 5 — AWS provisioning for the policy engine + HITL memory (Step 0).

Run this ONCE from an AWS-authenticated shell (creds + region set). It is NOT on
the request path — it's a control-plane setup script. It:

  1. Creates (or reuses) an AgentCore Policy Engine and writes the refund-amount
     Cedar policy into it  -> the auditable source-of-truth policy artifact.
  2. Creates (or reuses) an AgentCore Memory resource with NO strategies -> the
     durable store for HITL PendingApproval snapshots.

Then it prints the two IDs you paste into your config (POLICY_ENGINE_ID,
MEMORY_ID).

    uv run python provision_phase5.py                 # create both, print IDs
    uv run python provision_phase5.py --region ap-south-1

HONEST NOTE (policy engine): the installed PolicyEngineClient exposes only policy
CRUD -- there is no runtime isAuthorized/evaluate call. So this engine is where
the Cedar statement LIVES (auditable, real), but the runtime allow/deny decision
is still made by policy.authorize_refund()'s local Cedar-shaped mirror. Creating
the engine is worth it for the auditable-artifact story; it does not change code
behavior. The Memory resource, by contrast, DOES have runtime payoff: it backs
AgentCoreMemoryApprovalStore so a paused approval survives a process restart.
"""

from __future__ import annotations

import argparse
import json

from policy import CEDAR_POLICY  # the exact permit(...) when { amount < 5000 } statement

# AWS name constraint ^[A-Za-z][A-Za-z0-9_]*$ applies to the policy engine and
# policy names too (not just memory) -- underscores only, no dashes.
POLICY_ENGINE_NAME = "ordercare_refund_policy_engine"
POLICY_NAME = "refund_amount_threshold"
MEMORY_NAME = "ordercare_hitl_approvals"   # [A-Za-z0-9_]; no dashes for memory names
DEFAULT_REGION = "ap-south-1"

# 90 days is the SDK default; HITL approvals are short-lived, but a longer floor
# is harmless and avoids a parked approval expiring out from under a slow human.
MEMORY_EVENT_EXPIRY_DAYS = 90


def provision_policy_engine(region: str) -> dict:
    from bedrock_agentcore.policy import PolicyEngineClient

    client = PolicyEngineClient(region_name=region)
    print(f"[policy] create-or-get engine {POLICY_ENGINE_NAME!r} in {region} ...")
    engine = client.create_or_get_policy_engine(
        name=POLICY_ENGINE_NAME,
        description="OrderCare refund authorization (Phase 5 / spec §2.6).",
    )
    engine_id = engine["policyEngineId"]
    print(f"[policy] engine ACTIVE: {engine_id}")

    print(f"[policy] create-or-get policy {POLICY_NAME!r} (Cedar) ...")
    policy = client.create_or_get_policy(
        policy_engine_id=engine_id,
        name=POLICY_NAME,
        definition={"cedar": {"statement": CEDAR_POLICY}},
        description="Refunds under INR 5,000 auto-approve; at/over route to HITL.",
    )
    print(f"[policy] policy ACTIVE: {policy.get('policyId')}")
    return {"policy_engine_id": engine_id, "policy_id": policy.get("policyId")}


def provision_memory(region: str) -> dict:
    from bedrock_agentcore.memory.controlplane import MemoryControlPlaneClient

    cp = MemoryControlPlaneClient(region_name=region)

    # Reuse by name if it already exists (create_memory isn't create-or-get).
    for m in cp.list_memories(max_results=100):
        if m.get("name") == MEMORY_NAME:
            mid = m.get("id") or m.get("memoryId")
            print(f"[memory] reusing existing {MEMORY_NAME!r}: {mid}")
            return {"memory_id": mid}

    print(f"[memory] creating {MEMORY_NAME!r} in {region} (no strategies) ...")
    memory = cp.create_memory(
        name=MEMORY_NAME,
        event_expiry_days=MEMORY_EVENT_EXPIRY_DAYS,
        description="OrderCare HITL PendingApproval snapshots (Phase 5 / spec §2.5).",
        # No strategies: we store raw approval-record blobs, not extracted
        # semantic/summary memories.
        wait_for_active=True,
    )
    mid = memory.get("id") or memory.get("memoryId")
    print(f"[memory] memory ACTIVE: {mid}")
    return {"memory_id": mid}


def main() -> int:
    ap = argparse.ArgumentParser(description="Provision Phase 5 AWS resources.")
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--skip-policy", action="store_true", help="Only provision Memory.")
    ap.add_argument("--skip-memory", action="store_true", help="Only provision the policy engine.")
    args = ap.parse_args()

    out: dict = {"region": args.region}
    if not args.skip_policy:
        out.update(provision_policy_engine(args.region))
    if not args.skip_memory:
        out.update(provision_memory(args.region))

    print("\n=== Phase 5 resource IDs (record these) ===")
    print(json.dumps(out, indent=2))
    print("\nNext:")
    print("  - Put MEMORY_ID into settlement.py's gate (swap InMemoryApprovalStore ->")
    print("    AgentCoreMemoryApprovalStore(memory_id=..., region_name=...)).")
    print("  - POLICY_ENGINE_ID is the auditable Cedar artifact; runtime gating stays")
    print("    on policy.authorize_refund() until a data-plane evaluate API is confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
