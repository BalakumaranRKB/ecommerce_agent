# Confident Wrong Path: The Dropped Handoff

**Phase 1 finding (spec §2.1 / autonomy-decision.md Question 4).**
**Status:** found, reproduced live, and fixed. Toggle: `DROP_STANDING_IN_HANDOFF`.

---

## The short version

When the support agent hands a refund to the billing specialist, it must include
one fact the specialist **cannot look up on its own**: whether the customer's
account is *flagged*. Drop that fact, and the specialist — working only from what
it was told — **confidently approves a refund it should have paused for human
review.** The decision doesn't degrade; it flips to the wrong answer with full
confidence. That is the definition of a confident wrong path.

---

## Why this bug is even possible

The whole point of Path A (see `autonomy-decision.md`) is that the billing
specialist is a *separate process* with its own data surface. It reads
`REFUND_REQUESTS`, `ORDERS`, and `ACCOUNTS`, but the **live account standing for
the current conversation is context the main agent holds**, and the two do not
share memory. Anything the specialist needs to know has to *cross the network
boundary explicitly* inside the `DisputeRequest`.

That boundary is exactly what makes the failure realistic. In an in-process
function call, you might pass the whole session object and the standing would
"come along for free." Across an A2A boundary, every field is a deliberate choice
— and a forgotten field is silently gone. The schema even makes the omission easy
to miss:

```python
@dataclass
class DisputeRequest:
    order_id: str
    customer_id: str
    refund_amount: int
    reason: str
    account_standing: str = "active"   # <-- defaults to "active" if omitted
```

The default is the trap. Omit `account_standing`, and it doesn't error — it
quietly becomes `"active"`, so a flagged customer looks perfectly normal to the
specialist.

---

## The evidence (reproduced live over a real A2A round-trip)

Test case: customer `cust_2002` (a **flagged** account), refund request
`req_r008` on order `ord_6003` (USB-C cable), amount **₹599** — deliberately
**under** the ₹5,000 human-review threshold. Under-threshold matters: it means
the amount alone would *not* trigger review, so the account standing is the
*only* thing standing between "approve" and "escalate." That isolates the bug
cleanly.

(This is exactly why `req_r008` exists in `mock_data.py`: it is the one
flagged-customer + under-threshold + pending refund in the dataset — the single
row where a dropped standing changes the outcome rather than just the wording.
See the threshold-spread table in `mock_data.py`.)

| | Broken handoff | Fixed handoff |
|---|---|---|
| `DisputeRequest` sent | `{order_id: ord_6003, customer_id: cust_2002, refund_amount: 599, reason: ...}` | `{..., account_standing: "flagged"}` |
| Specialist sees standing as | `"active"` (the schema default) | `"flagged"` (the truth) |
| Specialist decision | **`approve`** (approved=True, hitl=False) | **`escalate_hitl`** (approved=False, hitl=True) |
| Correct? | ❌ Wrong — a flagged customer's refund was auto-approved | ✅ Correct — routed to a human |

The specialist's own logs, side by side:

```
[SPECIALIST] Received dispute: ord_6003 / cust_2002 / INR 599 / standing=flagged
[SPECIALIST] Decision: escalate_hitl (approved=False, hitl=True)     <- FIXED

[SPECIALIST] Received dispute: ord_6003 / cust_2002 / INR 599 / standing=active
[SPECIALIST] Decision: approve (approved=True, hitl=False)           <- BROKEN
```

**The decision flips: `approve` → `escalate_hitl`.** Same order, same amount,
same customer — the *only* difference is whether one field crossed the boundary.

> Why not the ₹8,990 case: `cust_2002`'s other pending refunds (`req_r004`,
> `req_r005`) are on `ord_6002` at ₹8,990, which is *over* the threshold. There,
> dropping the standing only changes the *reason* the specialist gives
> ("flagged account" vs "amount exceeds threshold") — the outcome is
> `escalate_hitl` either way, so it's weaker evidence. `req_r008` at ₹599 is the
> honest demonstration: the one case where the dropped field, and nothing else,
> changes the answer.

---

## The fix

In `agent.py`, `Session.hand_off_to_billing()` fetches the standing from the
account record and passes it through every time:

```python
account = db.fetch_account(self.ticket.customer_id)
standing = account["standing"] if account else "active"

request = DisputeRequest(
    order_id=refund_request["order_id"],
    customer_id=refund_request["customer_id"],
    refund_amount=refund_request["amount"],
    reason=refund_request["reason"],
    account_standing=standing,          # <-- the field that must cross
)
```

The broken path is kept behind the `DROP_STANDING_IN_HANDOFF` environment switch
rather than deleted, so both sides can be demonstrated from the *same* running
system instead of hand-edited snapshots.

---

## How to reproduce it

**At the specialist level (fully reproducible today).** This is the cleanest
demonstration — it sends `req_r008` through a real A2A round-trip both ways. With
the specialist running (`uv run python -m billing_specialist.a2a_server`), send
the `ord_6003` / ₹599 dispute with `account_standing="flagged"` and then without
it, and observe the specialist log `escalate_hitl` then `approve`. (The
`resolve_dispute()` unit tests in Phase 5 assert exactly this pair.)

**Through the full live agent — depends on a deferred fix.** Running
`cli.py --customer cust_2002 --type refund` will *not* currently reach `req_r008`
on its own: `_find_refund_request()` returns the customer's *first* pending
refund, which is `req_r004` (the ₹8,990 laptop stand), not the ₹599 cable. This
is the same order-matching limitation documented in
`phase_1_implementation_plan.md` (Known limitations) — the agent doesn't yet map
the customer's mentioned item ("USB-C cable") to the right pending refund. Once
that matching is implemented, the end-to-end agent will reproduce the flip
directly; until then, the flip is demonstrated at the specialist level above.

The `DROP_STANDING_IN_HANDOFF` switch controls the broken vs fixed path in either
case:

```bash
# FIXED (default): standing crosses -> flagged customer's refund escalated
# BROKEN: standing dropped -> same refund auto-approved
DROP_STANDING_IN_HANDOFF=1 uv run python cli.py --customer cust_2002 --type refund
```
(PowerShell: `$env:DROP_STANDING_IN_HANDOFF = "1"` first.)

---

## The lesson

Across an agent-to-agent boundary, **state that isn't explicitly sent is state
the other agent silently invents a default for.** The danger isn't a crash — a
crash would be easy to catch. It's a plausible, confident, *wrong* answer, made
from incomplete context the receiving agent had no way to know was incomplete.
The mitigation is to treat every field crossing the boundary as a deliberate,
tested contract — which is what the Phase 5 tests will lock down for
`resolve_dispute()`.
