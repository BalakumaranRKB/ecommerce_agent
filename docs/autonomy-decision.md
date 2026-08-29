# Autonomy Decision — Assignment 3, Session 5 Framework

**Date:** 24 Aug 2026. **Branch:** `assignment-3`.
**Decision: Path A — build the billing specialist agent.**

## The four questions

The Session 5 class framework requires four questions to be answered honestly
before deciding whether to split an agent. Each is stated, then answered against
*this* agent's real data and tools.

### Question 1 — Different tools / data / authority?

> Does any sub-task genuinely need meaningfully different tools, data, or
> authority than your main agent already has?

**Yes — and this is the decider.**

The main support agent operates with two read-only tools (`lookup_order`,
`check_account_status`), enforced by `harness_check()` via `PERMITTED_TOOLS`.
It retrieves policy documents and answers customer questions. It has **no
mutation authority** — it cannot approve, reject, or execute a refund.

Billing disputes require:
- **Different data:** the `REFUND_REQUESTS` store (`req_r001`–`req_r007`),
  which contains refund amounts, dispute reasons, and status. The main agent
  never touches this data; it is not exposed through `lookup_order` or
  `check_account_status`.
- **Different authority:** the ability to *act* on a disputed charge —
  approve a refund, reject it, or escalate it. This is mutation authority that
  the main agent's read-only tool set deliberately does not have.
- **Different context:** the specialist needs the customer's `account_standing`
  (e.g. `flagged` for `cust_2002`) to make informed decisions — a piece of
  context that must *cross the boundary* into the specialist's reasoning.

Enforcing this boundary by **which agent you are** is cleaner than a permission
`if` inside the harness. The main agent's `PERMITTED_TOOLS = {"lookup_order",
"check_account_status"}` stays exactly as it is; the specialist's authority is
scoped to its own service.

### Question 2 — One-sentence job?

> Can the candidate specialist agent's job be described in one honest sentence?

**Yes:** "Resolve billing disputes and process refund decisions."

This is honest and bounded. The specialist does not handle order status queries,
delivery complaints, or account management — those stay with the main agent.
The job description does not sprawl, so the boundary is in the right place.

### Question 3 — Real bottleneck?

> Is there a real, current bottleneck that a second agent would relieve — not
> just a tidier architecture diagram?

**Qualified yes.** The real bottleneck is *authority scope*. To handle billing
disputes today, the main agent would need:
1. `PERMITTED_TOOLS` expanded to include refund-mutation tools.
2. Conditional authorization logic inside `harness_check()` ("if this is a
   refund tool call, check the amount against the policy threshold").
3. The policy boundary (Phase 5) and HITL gate (Phase 5b) wired into the
   *same* harness loop that handles ordinary read-only queries.

This is not a theoretical bottleneck — it is a real increase in the complexity
of `harness_check()`, which today is a clean 10-line function that checks two
things: is the tool permitted, and does the entity belong to this customer. Adding
refund-specific authorization logic there mixes read and write concerns in the
same boundary function.

A separate specialist keeps the main agent's harness simple (read-only tools,
ownership check) and gives the billing specialist its own, purpose-built
authorization logic (amount threshold, account standing, double-refund guard).

The honest caveat: if this were a production system with no deadline, the
cleanliness argument alone would not justify a separate service. The bottleneck
is real but manageable within one agent. We accept Path A because Question 1's
data/authority separation is the stronger argument, and Question 4's cost is
acceptable.

### Question 4 — Afford the failure mode?

> Can you afford the failure mode a handoff introduces?

**Yes — and we prove it by finding and fixing it.**

Every handoff can drop state across the boundary. The specific failure mode:
when the main agent hands off a dispute on `ord_6002` (`cust_2002`,
`account_standing = "flagged"`), the refund amount or the customer's account
standing might fail to cross into the specialist. If `account_standing` is
omitted, the specialist doesn't know the customer is flagged and confidently
approves a ₹8,990 refund that should have required extra scrutiny.

This is the §2.1 CWP (confident wrong path): a real dropped-handoff case,
found and fixed. Documented in `docs/dropped-handoff-found.md` with broken
vs. fixed side by side.

We afford this failure mode because:
1. The state that needs to cross is small and enumerable (order_id,
   customer_id, refund_amount, reason, account_standing).
2. The specialist's typed input schema (`DisputeRequest`) makes missing
   fields visible at the boundary — a missing `account_standing` is a schema
   gap, not a silent omission.
3. The fix is straightforward: include `account_standing` in every handoff.

---

## Path A vs Path B — pros and cons

| | **Path A — build the specialist** | **Path B — stay single-agent** |
|---|---|---|
| **Pros** | True separation of concerns: specialist owns its own data (REFUND_REQUESTS) and authority (approve/reject) without bloating the main agent's PERMITTED_TOOLS. Authority boundary enforced by *which agent you are*, not an in-agent `if`. Independent failure isolation. Demonstrates the harder engineering (real typed A2A handoff). | Nothing new to operate — one system to harden. The billing logic could be wired into the existing harness with conditional authorization. Less to break before the deadline. |
| **Cons** | A whole extra service: separate process, port, deploy. The dropped-handoff failure mode (state that should cross doesn't → specialist confidently wrong). New PII egress surface at the boundary. More surface for the very risks A3 reduces. | Main agent's PERMITTED_TOOLS must expand to include mutation tools. `harness_check()` mixes read and write authorization. Policy boundary and HITL gate share the harness loop with ordinary queries. Less to *show* live (no handoff demo). |

---

## Decision record

**Path A — build the specialist agent.**

Justified by the Question-1 data/authority separation: billing disputes need
transaction records (`REFUND_REQUESTS`) and mutation authority (approve/reject
refunds) that the main agent's read-only tool set (`lookup_order`,
`check_account_status`) does not and should not have. The cost is a real failure
mode (dropped handoff), which we found and fixed on `ord_6002` where the
customer's `flagged` account standing failed to cross the boundary, causing the
specialist to confidently approve a ₹8,990 refund it should have escalated.

The four-question framework pointed clearly to Path A. Questions 1 and 2 are
unambiguous; Question 3 is qualified but real; Question 4 is accepted and
proven via the §2.1 CWP.
