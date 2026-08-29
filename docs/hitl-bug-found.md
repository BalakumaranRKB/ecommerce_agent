# Confident Wrong Path: The Double Refund

**Phase 5 finding (spec §2.5 / §5.6).**
**Status:** found, reproduced, and fixed. Toggle: `HITLGate(correlate_by=...)`.

---

## The short version

Two refund requests target the **same order**. Approve both, and the gate pays
out **two refunds on one order** — quietly, with no error. Each approval, looked
at on its own, is perfectly valid: a real pending request, a real human clicking
approve. The bug is that the gate correlates approvals by the **ticket** they came
in on instead of the **resource** (the order) they mutate — so it never notices
that the second approval is a second bite at the same apple. That is a confident
wrong path: nothing throws, nothing looks wrong, and money leaves twice.

---

## Why this bug is even possible

The dataset seeds it directly. `mock_data.REFUND_REQUESTS` has two pending
requests on one order:

```
req_r004  order=ord_6002  amount=8990  "Laptop stand wrong model shipped, full refund"
req_r005  order=ord_6002  amount=8990  "Duplicate follow-up on the laptop stand refund (same order as req_r004)"
```

`REQUESTS_BY_ORDER["ord_6002"] == ["req_r004", "req_r005"]`. Both are over the
₹5,000 threshold, so both route to the HITL gate for a human. The question is
what the gate does when a human approves the first, and then the second arrives.

The natural-but-wrong instinct is to track approvals by their own id — one record
per `request_id`. It reads cleanly and each record is internally consistent. But
`request_id` is the **ticket**; the thing actually being mutated is the **order**.
Two tickets, one resource. Key by the ticket and the gate treats them as
unrelated; key by the resource and the second is obviously the same action
already in flight.

---

## The evidence (the same gate, one switch flipped)

The correlation strategy is a single lever, `correlate_by`, threaded through
every operation — create, resume, and the executed ledger — so broken and fixed
run against the *same* code (same idiom as Phase 1's `DROP_STANDING_IN_HANDOFF`).

`uv run python -m hitl` drives `req_r004` then `req_r005` through the gate both
ways:

```
=== HITL double-refund CWP (both req_r004 + req_r005 target ord_6002) ===

  correlate_by='request' (ticket-keyed, THE BUG): 2 refunds executed
  correlate_by='order'   (resource-keyed, FIXED): 1 refunds executed

  BUG reproduced: ticket-keying pays out twice on one order.
  FIX confirmed: resource-keying pays out once.
```

| | Ticket-keyed (broken) | Resource-keyed (fixed) |
|---|---|---|
| Correlation key | `request_id` (`req_r004`, `req_r005` — two keys) | `order_id` (`ord_6002` — one key) |
| Second `request_approval` | creates a **second** independent pending | returns the **existing** pending (same resource) |
| Executed ledger keyed by | `request_id` — `req_r005` not found in it | `order_id` — `ord_6002` already there |
| Refunds executed on `ord_6002` | **2** ❌ | **1** ✅ |

The mechanism, precisely: the executed-refund ledger uses the *same* correlation
key as everything else. In ticket-keyed mode it records `{req_r004}` after the
first payout, so when `req_r005` resumes it looks itself up, isn't there, and pays
again. In resource-keyed mode it records `{ord_6002}`, so `req_r005` (which maps to
`ord_6002`) is recognised as already-executed and refused with a structured
`double_refund` outcome.

---

## The fix

Correlate by the resource. In `hitl.py` the whole change is the key function,
which every operation routes through:

```python
def _key(self, order_id: str, request_id: str) -> str:
    return order_id if self.correlate_by == "order" else request_id
```

`correlate_by="order"` is the default; `"request"` is retained only so the bug
stays reproducible against the same code. Because the key drives creation, the
executed ledger, and the resume-time guard uniformly, keying on the order closes
the hole at every stage:

- **at creation** — a second request on `ord_6002` returns the existing pending
  instead of opening a parallel one;
- **at resume** — the resource-correlation guard sees `ord_6002` already has a
  refund out and refuses with `EscalationReason(code="double_refund")`;
- **on replay** — approving the same already-executed order again is refused the
  same way (covers the "replayed duplicate approval call" case in §5.6).

---

## How to reproduce it

```bash
uv run python -m hitl              # prints the broken(2) vs fixed(1) contrast
uv run python test_hitl_gate.py    # asserts fixed==1 and bug==2, plus re-validation/expiry
```

The relevant assertions in `test_hitl_gate.py`:

- `test_double_refund_fixed_executes_once` — resource-keyed gate pays once.
- `test_double_refund_bug_reproduces` — ticket-keyed gate pays twice (the bug,
  asserted so it can't silently rot).
- `test_replay_of_same_approval_refused` — a replayed approval on an executed
  order is refused with `double_refund`.

---

## The lesson

An approval gate keyed on the **request** answers "has *this ticket* been
approved?" — but the thing you must protect is the **resource**, and the question
that actually matters is "does *this order* already have a refund in flight or
paid?" The two coincide right up until two tickets point at one resource, and then
the ticket-keyed gate is confidently, silently wrong. Correlate by what is being
*mutated*, not by what *asked* — the same principle the spec states as §5.4, and
the same shape as Phase 1's lesson that state must be tied to the thing it
describes, not assumed from context.
