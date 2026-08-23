"""
Memory, in two kinds (assignment §2.4).

SHORT-TERM — the Conversation buffer. Ordered turns within ONE ticket, so the
agent remembers what was already said. This is "now".

LONG-TERM  — a lookup into the prior-ticket history store, keyed by customer ID,
so the agent can answer questions that depend on this customer's past, not just
this conversation. This is "before".

Why long-term memory is NOT a tool: prior-ticket history is retrieval, not an
action. Like RAG, it is fetched by our code and injected into the prompt before
the model is asked anything. That keeps the model's tool surface to exactly the
two required read-only tools, and keeps customer scoping in one place.

Scoping: Conversation.history() reads its OWN ticket's customer_id. There is no
way to ask a Conversation for a different customer's history — the same
"bind the scope to the ticket" pattern used by make_mcp_dispatch().

Run the memory demo (offline, no API key, no model):
    uv run python memory.py
"""

from __future__ import annotations

from dataclasses import dataclass, field

import db
from eval.trajectory import record
from ticket import Ticket


# ---------------------------------------------------------------------------
# Long-term: prior tickets, keyed by customer
# ---------------------------------------------------------------------------
def get_history(customer_id: str) -> list[dict]:
    """Return this customer's past, already-closed tickets (oldest first).

    Returns [] for a customer with no history — a first-time customer is a
    normal case, not an error.

    Assignment 2: reads the prior_tickets table instead of a module-level dict.
    Same signature, same return shape, same scoping — only the storage moved.
    """
    return db.fetch_prior_tickets(customer_id)


def format_history_for_prompt(prior_tickets: list[dict]) -> str:
    """Render prior tickets for injection into the system prompt."""
    if not prior_tickets:
        return "PRIOR TICKETS: none. This is the customer's first contact."
    lines = [f"PRIOR TICKETS for this customer ({len(prior_tickets)}):"]
    for t in prior_tickets:
        lines.append(
            f"  - [{t['ticket_id']} | {t['type']}] {t['summary']} "
            f"-> {t['resolution']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Short-term: the in-conversation buffer
# ---------------------------------------------------------------------------
@dataclass
class Conversation:
    """The live buffer for one ticket.

    `messages` is the provider-format message list. The harness loop appends to
    it directly as it runs (assistant turns, tool results, the final answer), so
    the buffer accumulates the FULL exchange — not just the visible text. That
    is what lets a later turn refer back to a tool result fetched earlier.
    """

    ticket: Ticket
    messages: list = field(default_factory=list)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def turn_count(self) -> int:
        """How many customer messages have been sent in this conversation."""
        return sum(
            1 for m in self.messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )

    def history(self) -> list[dict]:
        """This ticket customer's prior tickets. Scoped by construction: the
        customer_id comes from our own ticket, never from a caller.

        Recorded as a trajectory step so a fixture can assert the agent
        actually consulted history before referring to a past resolution —
        otherwise an answer that cites a prior ticket is indistinguishable from
        one that invented a plausible-sounding precedent.
        """
        prior = get_history(self.ticket.customer_id)
        record(
            "memory",
            "prior_tickets",
            args={"customer_id": self.ticket.customer_id},
            outcome="ok" if prior else "empty",
            detail={
                "n_prior_tickets": len(prior),
                "ticket_ids": [t["ticket_id"] for t in prior],
            },
        )
        return prior

    def context_block(self) -> str:
        """The memory portion of the system prompt: who this ticket is for,
        plus their history."""
        return (
            f"ACTIVE TICKET: {self.ticket.ticket_id} | "
            f"customer: {self.ticket.customer_id} | "
            f"type: {self.ticket.ticket_type}\n"
            f"{format_history_for_prompt(self.history())}"
        )


# ---------------------------------------------------------------------------
# Stage 5 demo — offline, deterministic. No model, no API key.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import mock_data as data

    # Data invariants: history must line up with the other mock stores.
    for cust_id, tickets in data.PRIOR_TICKETS.items():
        assert cust_id in data.ACCOUNTS, f"{cust_id} has history but no account"
        for t in tickets:
            assert t["type"] in ("order_status", "delivery", "refund", "account")
    print("Data invariants OK: every customer with history has an account.\n")

    print("=== LONG-TERM: prior tickets, keyed by customer ===")
    for cust_id in ("cust_1001", "cust_3003", "cust_5005"):
        tickets = get_history(cust_id)
        kinds = sorted({t["type"] for t in tickets})
        label = f"{len(tickets)} prior ticket(s)" + (f", types: {kinds}" if kinds else "")
        print(f"  {cust_id}: {label}")
    print("\n  cust_1001 spans three different scenario types — one customer,")
    print("  many past tickets, reached by lookup and never worked concurrently.\n")

    print("=== SHORT-TERM: the buffer grows across turns ===")
    ticket = Ticket(ticket_id="tkt_9001", customer_id="cust_1001", ticket_type="delivery")
    conv = Conversation(ticket=ticket)

    conv.add_user("My order ord_5001 is 10 days late, do I get anything?")
    print(f"  after turn 1: {conv.turn_count()} customer turn(s), {len(conv.messages)} message(s)")

    # The harness loop appends these mid-turn; simulated here so the demo stays
    # offline and deterministic.
    conv.messages.append({"role": "assistant", "content": "[proposes lookup_order]"})
    conv.messages.append({"role": "user", "content": [{"tool_result": "ord_5001 ... delivery_date 2026-07-31"}]})
    conv.messages.append({"role": "assistant", "content": "You're owed a shipping refund plus 20% credit."})

    conv.add_user("And what about the other one?")
    print(f"  after turn 2: {conv.turn_count()} customer turn(s), {len(conv.messages)} message(s)")
    print("  the turn-1 tool result is still in the buffer, so 'the other one'")
    print("  can be resolved against what was already fetched.\n")

    print("=== the memory block injected into the system prompt ===")
    print(conv.context_block())

    print("\n=== scoping: a Conversation can only read its OWN customer ===")
    other = Conversation(ticket=Ticket("tkt_9002", "cust_3003", "account"))
    print(f"  conv (cust_1001)  -> {len(conv.history())} prior tickets")
    print(f"  other (cust_3003) -> {len(other.history())} prior tickets")
    print("  history() takes no argument: the customer comes from the ticket.")
