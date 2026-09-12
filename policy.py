"""
The refund-amount authorization boundary (Phase 5 / spec §2.6, SPEC-POLICY).

WHAT THIS IS
------------
An authorization check that sits between a DECIDED refund and its execution —
the same idea as `harness_check()`, one layer further out. `harness_check()`
asks "is this tool call permitted for this customer"; this asks "is this
*amount*, for this *customer*, on this *order*, within policy." It does NOT
re-decide the refund (that is the billing specialist's job, in
`billing_specialist/agent.py:resolve_dispute`). It authorizes the amount of an
already-decided one. Defense in depth: the enforcement point does not trust the
decider's judgment, it re-checks — exactly the reason harness_check re-checks
the model rather than trusting it.

WHY A SEPARATE, EXTERNALIZED CHECK
----------------------------------
The rule lives as a Cedar policy (below), authoritatively provisioned in an
AgentCore Policy Engine. Expressing "refunds at or over ₹5,000 need a human" as
an auditable Cedar statement — rather than a bare `if` buried in reasoning code —
is the spec's §2.6 requirement. An over-threshold refund is NOT denied outright;
it is routed to the HITL gate (`hitl.py`), because a large refund isn't
forbidden, it needs a person.

THE `>=` vs `<` CONSISTENCY (read before touching the threshold)
----------------------------------------------------------------
`billing_specialist/agent.py` already escalates on `refund_amount >= 5000`, so
`req_r007` (exactly ₹5,000) goes to HITL. This module MUST agree, or the
specialist and the policy boundary would disagree on the exactly-5,000 case. So
the Cedar condition is `context.amount < 5000` (strictly less than): amounts
UNDER 5,000 are permitted (auto-approve); 5,000 and above are not permitted and
fall through to HITL. This is the reverse of the build-plan's *illustrative*
`<= 5000` — that example would auto-approve exactly-5,000 and contradict the
shipped specialist. We import the specialist's constant so there is exactly ONE
source of truth for the number itself.

RUNTIME EVALUATION — HONEST STATUS
----------------------------------
The installed `bedrock_agentcore.policy.PolicyEngineClient` exposes only policy
CRUD (create/get/list/update/delete) — there is no `isAuthorized`/evaluate call
on it (verified against the installed source). So the managed engine is where
the Cedar policy *lives*, but the runtime allow/deny decision is made by the
local Cedar-shaped evaluator below, which mirrors the exact same statement. The
spec explicitly permits a hand-rolled function of the same shape as a documented
fallback (§6.1); this is that, used deliberately. `provision_cedar_policy()`
below writes the authoritative policy into the managed engine (control-plane,
run from an AWS-authenticated shell); `authorize_refund()` is the runtime gate.

    uv run python -m policy            # prints the Cedar policy + a few decisions
"""

from __future__ import annotations

from typing import Literal

# SINGLE SOURCE OF TRUTH for the threshold number. Defined in the specialist
# (spec §6.2), imported here so the policy boundary and the specialist's own
# escalation logic can never drift apart on the value.
from billing_specialist.agent import HITL_THRESHOLD_INR

PolicyOutcome = Literal["allow", "needs_approval"]

# The authoritative rule, as a Cedar statement. This exact text is what
# provision_cedar_policy() writes into the managed AgentCore Policy Engine, and
# what authorize_refund() mirrors in Python. `context.amount < 5000` (strictly
# less than) so exactly-5,000 is NOT permitted and routes to HITL — matching the
# specialist's `>= 5000 -> escalate`.
CEDAR_POLICY = f"""\
permit(
    principal,
    action == Action::"IssueRefund",
    resource
) when {{
    context.amount < {HITL_THRESHOLD_INR}
}};"""


def authorize_refund(customer_id: str, order_id: str, amount: int) -> PolicyOutcome:
    """Authorize the AMOUNT of an already-decided refund, at the execution seam.

    Returns:
      "allow"          — amount is under the threshold; the refund may execute.
      "needs_approval" — amount is at or over the threshold; route to the HITL
                         gate (NOT a flat deny — a human decides).

    This mirrors CEDAR_POLICY exactly. It is deliberately a pure function of
    (amount vs threshold): it does not re-check account standing, double-refunds,
    or order ownership — those are the specialist's verdict, not the amount
    boundary. Keeping this narrow is what stops it from double-building the
    specialist's job.
    """
    # `< threshold` -> allow  (so exactly-threshold and above -> needs_approval),
    # consistent with the specialist's `>= threshold -> escalate`.
    return "allow" if amount < HITL_THRESHOLD_INR else "needs_approval"


# ---------------------------------------------------------------------------
# Managed-engine provisioning (control-plane; run from an AWS-authenticated
# shell, NOT on the request path). Lazy-imports boto3/bedrock_agentcore so that
# importing this module for the local gate/tests needs zero AWS credentials.
# ---------------------------------------------------------------------------
def provision_cedar_policy(policy_engine_id: str, region_name: str = "ap-south-1",
                           policy_name: str = "refund-amount-threshold") -> dict:
    """Write CEDAR_POLICY into the managed AgentCore Policy Engine as the
    authoritative source of truth. Idempotent (create-or-get by name).

    This does NOT make the engine the runtime evaluator (the SDK can't evaluate
    from Python in this version) — it records the auditable policy artifact the
    §2.6 write-up points at. Returns the created/existing policy details.
    """
    from bedrock_agentcore.policy import PolicyEngineClient  # lazy: no AWS at import

    client = PolicyEngineClient(region_name=region_name)
    return client.create_or_get_policy(
        policy_engine_id=policy_engine_id,
        name=policy_name,
        definition={"cedar": {"statement": CEDAR_POLICY}},
        description="Refunds under INR 5,000 auto-approve; at/over route to HITL.",
    )


if __name__ == "__main__":
    print("=== Refund-amount policy boundary (Phase 5 / §2.6) ===")
    print(f"threshold: INR {HITL_THRESHOLD_INR:,}  (amount < threshold -> allow)\n")
    print("Cedar policy (authoritative, provisioned in the managed engine):")
    print(CEDAR_POLICY)
    print()
    for amt in (2499, 4999, 5000, 8990, 15499):
        print(f"  INR {amt:>6,} -> {authorize_refund('cust_x', 'ord_x', amt)}")
