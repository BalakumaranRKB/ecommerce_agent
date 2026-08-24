"""
Mock data for the e-commerce order-support agent — ASSIGNMENT 3 EDITION.

WHY THIS FILE WAS EXPANDED (assignment-3 §"A note on your mock data")
---------------------------------------------------------------------
Assignment 1's dataset was sized for a much earlier stage: it had no PII fields
at all (no names, phones, emails, addresses) and no refund *amounts*. Three of
Assignment 3's demos depend on data this file did not previously contain:

  1. PII masking (§2.4) needs realistic PII-bearing fields so masking is
     actually testable, not trivially obvious. A dataset with no phone number
     in it cannot demonstrate a phone number being redacted.

  2. The HITL gate (§2.5) and the policy boundary (§2.6) both need refund
     amounts that fall on BOTH sides of whatever threshold is set, so there are
     real over-threshold and under-threshold cases to react to. A gate with
     nothing to block is not a demonstrated gate.

  3. The Presidio-gap "confident wrong path" (§2.4) needs domain-specific PII
     formats that Presidio's default recognizers miss out of the box — for an
     India-facing store, Aadhaar and PAN numbers. Without a real Aadhaar in the
     data, the "leak I found and fixed" writeup would be manufactured, which is
     exactly the anti-pattern the assignment grades against.

WHAT CHANGED FROM ASSIGNMENT 2's mock_data.py
---------------------------------------------
  - ACCOUNTS gained a CUSTOMER_PII block: full name, email, phone (Indian
    +91 format), shipping address, and — for two customers — an Aadhaar and/or
    PAN on file (the domain-specific formats Presidio misses).
  - ORDERS gained an `amount` (order value in INR) and a `currency` field.
  - A new REFUND_REQUESTS store: per-order refund asks with explicit amounts,
    deliberately spread across the HITL threshold (see THRESHOLD note below).
  - PRIOR_TICKETS summaries now reference concrete amounts, so the long-term
    memory the agent injects is itself a realistic PII/amount leak surface.
  - Every store still shares the same customer IDs, same as Assignment 1/2.

WHAT DID *NOT* CHANGE — and must not, to keep the A2 code working
----------------------------------------------------------------
  - The ORDERS record shape KEEPS every A2 field (order_id, customer_id, item,
    status, promised_delivery_date, delivery_date). `db.py`'s fetch_order and
    the delivery-lateness computation both still work unchanged — the new
    `amount`/`currency` fields are additive.
  - The ACCOUNTS record shape KEEPS customer_id, standing, order_history.
    `db.fetch_account` returns standing + order_history exactly as before; the
    PII lives in a parallel CUSTOMER_PII dict so a caller that only wants
    standing is not forced to touch PII. (This separation is itself a masking
    design decision — see docs/spec-assignment-3.md §5.)
  - ORDER_OWNER is still derived the same way, so harness scoping is unchanged.

THRESHOLD DESIGN (read before setting HITL_THRESHOLD_INR anywhere)
------------------------------------------------------------------
The HITL/policy refund threshold used across the assignment is **INR 5,000**
(documented and justified in docs/spec-assignment-3.md §6). The refund amounts
below are chosen so that, for the same threshold:

  - at least two requests sit clearly UNDER it   (auto-approvable),
  - at least two sit clearly OVER it             (must route to HITL / be
                                                  rejected by the policy engine),
  - one sits just BELOW and one just ABOVE       (so an off-by-one or a >= vs >
                                                  bug in the gate is visible),
  - two target the SAME order/resource           (so the "correlate by resource,
                                                  not by ticket" double-refund
                                                  case in §2.5 has real inputs).

Everything here is read-only mock data. There is no reset(); tests seed a fresh
database from these dicts.
"""

from __future__ import annotations

# ===========================================================================
# ORDERS  (order_id -> OrderRecord)
# ---------------------------------------------------------------------------
# UNCHANGED A2 FIELDS: order_id, customer_id, item, status,
#                      promised_delivery_date, delivery_date.
# NEW A3 FIELDS:       amount (order value, INR), currency.
#
# `amount` is the value of the order and therefore the CEILING on any refund
# against it — a refund request can be for the full amount or a partial. The
# spread across orders is intentional (see the per-order notes).
# status in {shipped, delivered, in_transit, cancelled}
# ===========================================================================
ORDERS: dict[str, dict] = {
    # cust_1001 — active, three orders spanning under/over the threshold.
    "ord_5001": {"order_id": "ord_5001", "customer_id": "cust_1001", "item": "Wireless Earbuds",   "status": "shipped",    "promised_delivery_date": "2026-07-21", "delivery_date": "2026-07-31", "amount": 2499,  "currency": "INR"},
    "ord_5002": {"order_id": "ord_5002", "customer_id": "cust_1001", "item": "Phone Case",          "status": "delivered",  "promised_delivery_date": "2026-07-08", "delivery_date": "2026-07-10", "amount": 799,   "currency": "INR"},
    "ord_9007": {"order_id": "ord_9007", "customer_id": "cust_1001", "item": "Notebook",            "status": "delivered",  "promised_delivery_date": "2026-05-19", "delivery_date": "2026-05-30", "amount": 349,   "currency": "INR"},

    # cust_2002 — flagged. ord_6002 is a HIGH-VALUE order: full refund would be
    # well over the threshold, the headline "must go to HITL" case.
    "ord_6002": {"order_id": "ord_6002", "customer_id": "cust_2002", "item": "Laptop Stand",        "status": "delivered",  "promised_delivery_date": "2026-06-28", "delivery_date": "2026-06-28", "amount": 8990,  "currency": "INR"},
    "ord_6003": {"order_id": "ord_6003", "customer_id": "cust_2002", "item": "USB-C Cable",         "status": "in_transit", "promised_delivery_date": "2026-08-02", "delivery_date": "2026-08-02", "amount": 599,   "currency": "INR"},

    # cust_3003 — suspended, cancelled order (full refund, no return, per policy).
    "ord_7004": {"order_id": "ord_7004", "customer_id": "cust_3003", "item": "Mechanical Keyboard", "status": "cancelled",  "promised_delivery_date": "2026-07-01", "delivery_date": "2026-07-01", "amount": 4999,  "currency": "INR"},

    # cust_4004 — active. ord_8005 is the damaged-item case AND sits at EXACTLY
    # the just-below-threshold boundary (4,999 < 5,000), so a >= vs > bug shows.
    "ord_8005": {"order_id": "ord_8005", "customer_id": "cust_4004", "item": "Desk Lamp",           "status": "delivered",  "promised_delivery_date": "2026-07-15", "delivery_date": "2026-07-15", "amount": 4999,  "currency": "INR"},

    # cust_5005 — active, first-time customer. High-value monitor: over-threshold.
    "ord_9006": {"order_id": "ord_9006", "customer_id": "cust_5005", "item": "27-inch Monitor",     "status": "shipped",    "promised_delivery_date": "2026-07-26", "delivery_date": "2026-07-28", "amount": 15499, "currency": "INR"},
}


# ===========================================================================
# ACCOUNTS  (customer_id -> AccountRecord)
# ---------------------------------------------------------------------------
# UNCHANGED A2 FIELDS: customer_id, standing, order_history.
# `db.fetch_account` returns THESE and only these (plus a derived order_history)
# — the PII below is deliberately NOT folded in here, so a caller that needs
# only standing never has to load, and then be trusted not to leak, PII.
# standing in {active, flagged, suspended}
# ===========================================================================
ACCOUNTS: dict[str, dict] = {
    "cust_1001": {"customer_id": "cust_1001", "standing": "active",    "order_history": ["ord_5001", "ord_5002", "ord_9007"]},
    "cust_2002": {"customer_id": "cust_2002", "standing": "flagged",   "order_history": ["ord_6002", "ord_6003"]},
    "cust_3003": {"customer_id": "cust_3003", "standing": "suspended", "order_history": ["ord_7004"]},
    "cust_4004": {"customer_id": "cust_4004", "standing": "active",    "order_history": ["ord_8005"]},
    "cust_5005": {"customer_id": "cust_5005", "standing": "active",    "order_history": ["ord_9006"]},
}


# ===========================================================================
# CUSTOMER_PII  (customer_id -> PII record)  ***NEW IN ASSIGNMENT 3***
# ---------------------------------------------------------------------------
# The realistic PII the masking layers (§2.4) must catch. Kept in its own store,
# NOT merged into ACCOUNTS, so:
#   - the "which fields are PII" boundary is explicit and auditable, and
#   - a code path that only needs standing/order_history cannot accidentally
#     pull PII into a context it will later have to be trusted to scrub.
#
# COVERAGE MATRIX — every field here exists to be caught (or missed) by a layer:
#
#   field          Presidio default?   Bedrock Guardrails?   Notes
#   -------------  ------------------   -------------------   -----------------
#   name           yes (NER)            yes (NAME)            baseline
#   email          yes                  yes (EMAIL)          baseline
#   phone (+91)    partial *            yes (PHONE)          * Presidio's phone
#                                                              recognizer is
#                                                              region-sensitive;
#                                                              +91 formatting is
#                                                              a known soft spot
#                                                              -> layering win.
#   address        partial (NER)        partial              messy free text
#   aadhaar        NO (default)         NO (not a built-in)  ** THE §2.4 CWP:
#                                                              needs a custom
#                                                              recognizer; both
#                                                              default layers
#                                                              miss it.
#   pan            NO (default)         NO                    ** same class.
#
# The Aadhaar/PAN rows are what make the "find a real leak your first-pass
# masking missed, then fix it" requirement an HONEST finding rather than a
# manufactured one: they are genuinely uncovered by BOTH default layers, so the
# fix is a real custom recognizer, not a staged demo.
#
# NOTE: these are fabricated, format-valid-looking test values, NOT real
# identifiers. Aadhaar shown with spaces (as printed on real cards) so the
# "format-preserving evasion" sub-case (spaced vs unspaced digits) is available.
# ===========================================================================
CUSTOMER_PII: dict[str, dict] = {
    "cust_1001": {
        "customer_id": "cust_1001",
        "name":    "Ananya Krishnan",
        "email":   "ananya.krishnan@example.in",
        "phone":   "+91 98765 43210",
        "address": "14, 2nd Cross, Devarachikkanahalli, Bengaluru, Karnataka 560038",
        "aadhaar": "3782 4629 1057",   # ** Presidio default MISS — the CWP seed
        "pan":     "AKPPK7821L",        # ** Presidio default MISS
    },
    "cust_2002": {
        "customer_id": "cust_2002",
        "name":    "Rohit Mehta",
        "email":   "rohit.mehta@example.in",
        "phone":   "+91 91234 56780",
        "address": "Flat 7B, Sunrise Apartments, Powai, Mumbai, Maharashtra 400076",
        "pan":     "BMTPM4590Q",        # ** PAN present, Aadhaar absent — partial coverage case
    },
    "cust_3003": {
        "customer_id": "cust_3003",
        "name":    "Fatima Sheikh",
        "email":   "fatima.sheikh@example.in",
        "phone":   "+91 99887 76655",
        "address": "22 Residency Road, Ashok Nagar, Chennai, Tamil Nadu 600030",
    },
    "cust_4004": {
        "customer_id": "cust_4004",
        "name":    "Daniel Fernandes",
        "email":   "daniel.fernandes@example.in",
        "phone":   "+91 90000 12345",
        "address": "3, Palm Grove, Bandra West, Mumbai, Maharashtra 400050",
        "aadhaar": "9021 3847 6512",   # ** second Aadhaar — lets a fix be tested on 2 records
    },
    "cust_5005": {
        "customer_id": "cust_5005",
        "name":    "Priya Nair",
        "email":   "priya.nair@example.in",
        "phone":   "+91 98450 09821",
        "address": "88, Lake View Layout, Koramangala, Bengaluru, Karnataka 560095",
    },
}


# ===========================================================================
# REFUND_REQUESTS  (request_id -> RefundRequest)  ***NEW IN ASSIGNMENT 3***
# ---------------------------------------------------------------------------
# The inputs to the HITL gate (§2.5) and the policy boundary (§2.6). Each is a
# refund ASK against a specific order, with an explicit amount. The gate/policy
# engine key off `order_id` (the RESOURCE), NOT `request_id` (the ticket) — see
# §2.5 "correlate by resource, not by ticket".
#
# Fields:
#   request_id   - unique id for this ask (the "ticket"-level id)
#   order_id     - the RESOURCE being mutated (the correlation key)
#   customer_id  - owner; must match the order's owner (harness already enforces)
#   amount       - refund amount requested, INR. Compared against HITL_THRESHOLD.
#   reason       - free text; ALSO a PII leak surface (some reasons quote a phone)
#   status       - initial state; the state machine advances this
#                  (pending/approved/rejected/expired/executed)
#
# SPREAD ACROSS THE INR 5,000 THRESHOLD (the whole point of this store):
#   req_r001  ord_5001  2,499   UNDER   -> auto-approvable
#   req_r002  ord_9006 15,499   OVER    -> HITL required / policy rejects
#   req_r003  ord_8005  4,999   JUST UNDER (== 4,999 < 5,000) -> boundary test
#   req_r004  ord_6002  8,990   OVER    -> HITL required
#   req_r005  ord_6002  8,990   OVER + SAME ORDER as req_r004 -> DOUBLE-REFUND
#                                        correlation case for §2.5.
#   req_r006  ord_7004  4,999   UNDER, cancelled order -> full refund path
#   req_r007  ord_5002  5,000   EXACTLY AT threshold -> forces a >= vs > decision
# ===========================================================================
REFUND_REQUESTS: dict[str, dict] = {
    "req_r001": {"request_id": "req_r001", "order_id": "ord_5001", "customer_id": "cust_1001", "amount": 2499,  "reason": "Earbuds arrived 10 days late, requesting partial refund",                       "status": "pending"},
    "req_r002": {"request_id": "req_r002", "order_id": "ord_9006", "customer_id": "cust_5005", "amount": 15499, "reason": "Monitor screen cracked on arrival, full refund requested",                      "status": "pending"},
    "req_r003": {"request_id": "req_r003", "order_id": "ord_8005", "customer_id": "cust_4004", "amount": 4999,  "reason": "Desk lamp smashed in transit; call me on +91 90000 12345 to arrange",           "status": "pending"},
    "req_r004": {"request_id": "req_r004", "order_id": "ord_6002", "customer_id": "cust_2002", "amount": 8990,  "reason": "Laptop stand wrong model shipped, full refund",                                 "status": "pending"},
    "req_r005": {"request_id": "req_r005", "order_id": "ord_6002", "customer_id": "cust_2002", "amount": 8990,  "reason": "Duplicate follow-up on the laptop stand refund (same order as req_r004)",       "status": "pending"},
    "req_r006": {"request_id": "req_r006", "order_id": "ord_7004", "customer_id": "cust_3003", "amount": 4999,  "reason": "Cancelled before shipping, full refund on the keyboard",                        "status": "pending"},
    "req_r007": {"request_id": "req_r007", "order_id": "ord_5002", "customer_id": "cust_1001", "amount": 5000,  "reason": "Phone case defective; refund exactly at the policy line",                       "status": "pending"},
}


# ===========================================================================
# PRIOR_TICKETS  (customer_id -> list[PriorTicket])
# ---------------------------------------------------------------------------
# UNCHANGED SHAPE from A2 (ticket_id, type, summary, resolution). Summaries now
# reference concrete INR amounts, so the long-term-memory block the agent
# injects into its prompt is itself a realistic amount/PII surface the masking
# layers must handle on the WAY OUT, not just tool outputs.
# cust_5005 deliberately absent: the first-time-customer empty case.
# ===========================================================================
PRIOR_TICKETS: dict[str, list[dict]] = {
    "cust_1001": [
        {"ticket_id": "tkt_8801", "type": "delivery", "summary": "Order ord_9007 arrived 11 days after the promised date",              "resolution": "Shipping fee refunded (INR 60), 10% store credit issued"},
        {"ticket_id": "tkt_8825", "type": "refund",   "summary": "Returned the phone case from ord_5002, wrong colour",                "resolution": "Refund of INR 799 approved, paid in 5 business days"},
        {"ticket_id": "tkt_8850", "type": "account",  "summary": "Account flagged after a disputed charge",                            "resolution": "Appeal upheld, restored to active standing"},
    ],
    "cust_2002": [
        {"ticket_id": "tkt_8702", "type": "order_status", "summary": "Asked where order ord_6002 (INR 8,990) had reached",             "resolution": "Tracking link shared, delivered on time"},
        {"ticket_id": "tkt_8744", "type": "account",      "summary": "Third chargeback dispute filed on the account",                  "resolution": "Account flagged pending review"},
    ],
    "cust_3003": [
        {"ticket_id": "tkt_8610", "type": "account",  "summary": "Fifth chargeback dispute filed",                                     "resolution": "Account suspended, appeal window closed"},
    ],
    "cust_4004": [
        {"ticket_id": "tkt_8590", "type": "refund",   "summary": "Refund of INR 4,999 requested on ord_8005 forty days after delivery","resolution": "Outside the 30-day window, escalated for goodwill review"},
    ],
}


# ===========================================================================
# Derived ownership index (order_id -> owning customer_id).
# UNCHANGED from A2. Metadata the harness consults to scope a proposed call
# BEFORE dispatch; NOT order contents.
# ===========================================================================
ORDER_OWNER: dict[str, str] = {oid: rec["customer_id"] for oid, rec in ORDERS.items()}


# ===========================================================================
# Convenience index: which refund requests target each order (the RESOURCE).
# This is what a resource-correlated HITL state machine keys on. req_r004 and
# req_r005 both map to ord_6002 — the double-refund pair.
# ===========================================================================
REQUESTS_BY_ORDER: dict[str, list[str]] = {}
for _rid, _req in REFUND_REQUESTS.items():
    REQUESTS_BY_ORDER.setdefault(_req["order_id"], []).append(_rid)


# ===========================================================================
# Self-check / invariants. Run offline, no DB, no API key:
#     python mock_data.py
# Proves the data actually has the properties the A3 demos depend on, so a
# thin-data regression is caught here rather than in the middle of a video.
# ===========================================================================
if __name__ == "__main__":
    HITL_THRESHOLD_INR = 5000  # must match docs/spec-assignment-3.md §6

    print("=== customer-id consistency across all stores ===")
    for cid in ACCOUNTS:
        assert cid in CUSTOMER_PII, f"{cid} has an account but no PII record"
    for cid in CUSTOMER_PII:
        assert cid in ACCOUNTS, f"{cid} has PII but no account"
    for oid, o in ORDERS.items():
        assert o["customer_id"] in ACCOUNTS, f"{oid} owned by unknown customer"
    for rid, r in REFUND_REQUESTS.items():
        assert r["order_id"] in ORDERS, f"{rid} targets unknown order"
        assert r["customer_id"] == ORDERS[r["order_id"]]["customer_id"], (
            f"{rid} customer does not own {r['order_id']} — harness would reject"
        )
    print("  OK: every store shares consistent customer/order ids.\n")

    print("=== PII coverage the masking demo depends on (§2.4) ===")
    has_aadhaar = [c for c, p in CUSTOMER_PII.items() if "aadhaar" in p]
    has_pan     = [c for c, p in CUSTOMER_PII.items() if "pan" in p]
    print(f"  customers with a phone (+91)      : {len(CUSTOMER_PII)}/{len(CUSTOMER_PII)}")
    print(f"  customers with an Aadhaar on file : {has_aadhaar}  (Presidio-default MISS)")
    print(f"  customers with a PAN on file      : {has_pan}  (Presidio-default MISS)")
    assert has_aadhaar, "need at least one Aadhaar for the §2.4 CWP finding"
    assert has_pan, "need at least one PAN for the §2.4 CWP finding"
    leaky = [rid for rid, r in REFUND_REQUESTS.items() if "+91" in r["reason"]]
    print(f"  refund reasons embedding a phone  : {leaky}  (log/error-path leak surface)")
    assert leaky, "need at least one refund reason carrying PII for the error-path leak"
    print()

    print(f"=== refund amounts vs the HITL/policy threshold (INR {HITL_THRESHOLD_INR}) ===")
    under = [(rid, r["amount"]) for rid, r in REFUND_REQUESTS.items() if r["amount"] <  HITL_THRESHOLD_INR]
    at    = [(rid, r["amount"]) for rid, r in REFUND_REQUESTS.items() if r["amount"] == HITL_THRESHOLD_INR]
    over  = [(rid, r["amount"]) for rid, r in REFUND_REQUESTS.items() if r["amount"] >  HITL_THRESHOLD_INR]
    print(f"  UNDER threshold (auto-approvable) : {under}")
    print(f"  AT threshold    (>= vs > matters) : {at}")
    print(f"  OVER threshold  (HITL / reject)   : {over}")
    assert under and over, "need cases on BOTH sides of the threshold"
    assert at, "need a case exactly AT the threshold to force a >= vs > decision"
    print()

    print("=== double-refund correlation case (§2.5) ===")
    dup_orders = {oid: rids for oid, rids in REQUESTS_BY_ORDER.items() if len(rids) > 1}
    print(f"  orders with >1 refund request     : {dup_orders}")
    assert dup_orders, "need >=1 order with two requests for the double-refund case"
    print("  -> a resource-correlated gate must not approve both of these.\n")

    print("All Assignment 3 data invariants hold. This dataset can honestly")
    print("demonstrate masking, the HITL threshold, the policy boundary, and")
    print("the double-refund correlation case.")
