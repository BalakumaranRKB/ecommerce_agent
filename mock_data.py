"""
Mock in-memory data for the e-commerce order-support agent.

Three read-only stores, sharing the same customer IDs:
  - ORDERS:        8 orders (order_id, customer_id, item, status,
                   promised_delivery_date, delivery_date)
  - ACCOUNTS:      one per customer (standing active|flagged|suspended, order_history)
  - PRIOR_TICKETS: past closed tickets per customer (long-term memory, Stage 5)

Two date fields, deliberately:
  - promised_delivery_date — what the customer was told at purchase. Fixed.
  - delivery_date          — the actual (or currently expected) date.

Lateness is the gap between them, which is what makes a delivery ticket
answerable with a number instead of a vibe. "My order is late, do I get
anything?" needs the agent to look up both dates, compute the gap, and check it
against the shipping-delay policy — three steps a trajectory eval can verify,
where a single ambiguous date field would only have proven it called the tool.

Everything here is read-only, so there is nothing to mutate and no reset() is
needed.
"""

from __future__ import annotations

# order_id -> OrderRecord.  status in {shipped, delivered, in_transit, cancelled}
#
# The spread is intentional: ord_5001 is 10 days late and still in flight (the
# compensation case), ord_9007 arrived 11 days late and matches cust_1001's prior
# ticket tkt_8801 exactly, and several arrived on time so "is this late?" has a
# real negative case and not just positives.
ORDERS: dict[str, dict] = {
    "ord_5001": {"order_id": "ord_5001", "customer_id": "cust_1001", "item": "Wireless Earbuds",    "status": "shipped",    "promised_delivery_date": "2026-07-21", "delivery_date": "2026-07-31"},
    "ord_5002": {"order_id": "ord_5002", "customer_id": "cust_1001", "item": "Phone Case",           "status": "delivered",  "promised_delivery_date": "2026-07-08", "delivery_date": "2026-07-10"},
    "ord_9007": {"order_id": "ord_9007", "customer_id": "cust_1001", "item": "Notebook",             "status": "delivered",  "promised_delivery_date": "2026-05-19", "delivery_date": "2026-05-30"},
    "ord_6002": {"order_id": "ord_6002", "customer_id": "cust_2002", "item": "Laptop Stand",         "status": "delivered",  "promised_delivery_date": "2026-06-28", "delivery_date": "2026-06-28"},
    "ord_6003": {"order_id": "ord_6003", "customer_id": "cust_2002", "item": "USB-C Cable",          "status": "in_transit", "promised_delivery_date": "2026-08-02", "delivery_date": "2026-08-02"},
    "ord_7004": {"order_id": "ord_7004", "customer_id": "cust_3003", "item": "Mechanical Keyboard",  "status": "cancelled",  "promised_delivery_date": "2026-07-01", "delivery_date": "2026-07-01"},
    "ord_8005": {"order_id": "ord_8005", "customer_id": "cust_4004", "item": "Desk Lamp",            "status": "delivered",  "promised_delivery_date": "2026-07-15", "delivery_date": "2026-07-15"},
    "ord_9006": {"order_id": "ord_9006", "customer_id": "cust_5005", "item": "27-inch Monitor",      "status": "shipped",    "promised_delivery_date": "2026-07-26", "delivery_date": "2026-07-28"},
}

# customer_id -> AccountRecord.  standing in {active, flagged, suspended}
ACCOUNTS: dict[str, dict] = {
    "cust_1001": {"customer_id": "cust_1001", "standing": "active",    "order_history": ["ord_5001", "ord_5002", "ord_9007"]},
    "cust_2002": {"customer_id": "cust_2002", "standing": "flagged",   "order_history": ["ord_6002", "ord_6003"]},
    "cust_3003": {"customer_id": "cust_3003", "standing": "suspended", "order_history": ["ord_7004"]},
    "cust_4004": {"customer_id": "cust_4004", "standing": "active",    "order_history": ["ord_8005"]},
    "cust_5005": {"customer_id": "cust_5005", "standing": "active",    "order_history": ["ord_9006"]},
}

# ---------------------------------------------------------------------------
# Long-term memory store (Stage 5): the customer's PAST, already-closed tickets.
#
# Keyed by customer_id, each mapping to a list of prior tickets that may span
# different scenario types. This is where "one customer has several tickets
# across different scenarios" lives — as history on record, never as concurrent
# live work. A prior ticket carries no customer_id field because the store key
# already is the customer.
#
# cust_5005 is deliberately absent: a first-time customer with no history, so
# the empty case is exercised too.
# ---------------------------------------------------------------------------
PRIOR_TICKETS: dict[str, list[dict]] = {
    "cust_1001": [
        {"ticket_id": "tkt_8801", "type": "delivery",     "summary": "Order ord_9007 arrived 11 days after the promised date",  "resolution": "Shipping fee refunded, 10% store credit issued"},
        {"ticket_id": "tkt_8825", "type": "refund",       "summary": "Returned the phone case from ord_5002, wrong colour",    "resolution": "Refund approved, paid in 5 business days"},
        {"ticket_id": "tkt_8850", "type": "account",      "summary": "Account flagged after a disputed charge",                "resolution": "Appeal upheld, restored to active standing"},
    ],
    "cust_2002": [
        {"ticket_id": "tkt_8702", "type": "order_status", "summary": "Asked where order ord_6002 had reached",                 "resolution": "Tracking link shared, delivered on time"},
        {"ticket_id": "tkt_8744", "type": "account",      "summary": "Third chargeback dispute filed on the account",          "resolution": "Account flagged pending review"},
    ],
    "cust_3003": [
        {"ticket_id": "tkt_8610", "type": "account",      "summary": "Fifth chargeback dispute filed",                         "resolution": "Account suspended, appeal window closed"},
    ],
    "cust_4004": [
        {"ticket_id": "tkt_8590", "type": "refund",       "summary": "Refund requested on ord_8005 forty days after delivery", "resolution": "Outside the 30-day window, escalated for goodwill review"},
    ],
}


# Derived ownership index (order_id -> owning customer_id). This is METADATA the
# harness consults to scope a proposed call BEFORE dispatch; it is NOT order
# contents. Row-level-security style: the policy layer (harness) knows ownership,
# the tool layer returns contents only when the call is allowed.
ORDER_OWNER: dict[str, str] = {oid: rec["customer_id"] for oid, rec in ORDERS.items()}
