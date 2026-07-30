"""
Active ticket / session context — the spine of one conversation.

Exactly one Ticket is live per conversation. Its customer_id is the single
authority for two things:
  1. Permission scoping — every proposed tool call is checked against this
     customer before the harness will dispatch it (see harness.py).
  2. Long-term memory — the key used to look up this customer's prior tickets
     (Stage 5).

A conversation never manages multiple concurrent tickets; the customer's other
tickets live in the prior-history store and are reached by lookup.
"""

from __future__ import annotations

from dataclasses import dataclass

# The closed set of ticket types within the fixed e-commerce domain (PLAN §5).
TICKET_TYPES = ("order_status", "delivery", "refund", "account")


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    customer_id: str
    ticket_type: str

    def __post_init__(self) -> None:
        if self.ticket_type not in TICKET_TYPES:
            raise ValueError(
                f"ticket_type must be one of {TICKET_TYPES}, got {self.ticket_type!r}"
            )
