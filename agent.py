"""
Agent orchestration — the full turn lifecycle assembled.

For each customer message, one turn runs (in this order):

  1. APPEND — the customer message goes into the short-term buffer.
  2. CLASSIFY — a tiny LLM call picks one of the four ticket types. If the
     active ticket has no type yet, we record it; otherwise we just log it.
  3. LONG-TERM MEMORY — pull this customer's prior tickets (by customer_id).
  4. RAG — embed the message, retrieve top-k policy chunks above threshold.
  5. INJECT — build the system prompt from the ticket context, prior history,
     and either the retrieved policy or an explicit "no coverage" instruction.
  6. HARNESS LOOP — run_harnessed() drives the model with the tools available.
     Every tool call the model proposes passes harness_check() first (Stages 1
     and 3). The final answer is appended to the buffer by the loop.

All customer-scoped context (tool dispatch, prior tickets, active ticket) is
bound to a single Session object at construction time — so the wrong customer
is not expressible from here on out.

The classify step is a SECOND LLM call per turn (see docs/PLAN.md §3.2). That is
deliberate: an explicit `classify` phase we can point at and log, rather than
brittle rules or an implicit "the model figures it out." Two calls per turn is a
small price, and Groq/Anthropic classify calls are cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness import db_owner_resolver, make_mcp_dispatch, run_harnessed
from memory import Conversation
from retriever import PolicyRetriever
from ticket import TICKET_TYPES, Ticket
from tools_schema import TOOLS_BY_PROVIDER

# The base persona. Ticket context, prior tickets, and retrieved policy are
# appended per turn.
BASE_SYSTEM_PROMPT = """You are an e-commerce order-support agent. Answer the
customer briefly and directly.

Rules:
- Use ONLY the RELEVANT POLICY below to justify any policy claim. If it says
  "NO RELEVANT POLICY FOUND", tell the customer honestly that you cannot answer
  that from policy and offer to escalate. Do not invent numbers or terms.
- When you cite a policy, mention the doc_id in square brackets, e.g. [doc_id:
  refund-eligibility].
- Use the tools to fetch this customer's own order or account details when you
  need concrete facts (delivery date, status, standing). If a tool call is
  rejected as cross-customer, tell the customer you cannot look up someone
  else's data.
- Prior tickets are for context on this customer's history; do not re-answer
  them, only reference them when relevant.
"""

CLASSIFY_PROMPT = (
    "Classify the customer's message into EXACTLY ONE of these ticket types "
    "and reply with ONLY that word, nothing else:\n"
    "  order_status  - asking where an order is or its current state\n"
    "  delivery      - late, missing, or damaged delivery\n"
    "  refund        - refund or return request\n"
    "  account       - subscription, account standing, suspension, appeal\n"
    "If uncertain, pick the closest fit."
)


def classify_ticket(provider, message: str) -> str:
    """Return one of TICKET_TYPES. Falls back to 'order_status' if the model
    replies with anything unexpected — better a default than a crash."""
    resp = provider.create(
        system=CLASSIFY_PROMPT,
        messages=[{"role": "user", "content": message}],
        tools=[],
    )
    label = (resp.text or "").strip().lower().split()[0] if resp.text else ""
    label = label.strip(".,:;!?")
    return label if label in TICKET_TYPES else "order_status"


@dataclass
class Session:
    """One conversation with one customer, holding the objects that persist
    across turns. Everything customer-scoped is bound here."""

    ticket: Ticket
    provider: object
    provider_name: str = "groq"
    retriever: PolicyRetriever | None = None
    conversation: Conversation = field(init=False)

    def __post_init__(self):
        self.conversation = Conversation(ticket=self.ticket)
        if self.retriever is None:
            self.retriever = PolicyRetriever()
        self._resolve_owner = db_owner_resolver()
        self._dispatch = make_mcp_dispatch(self.ticket)

    def _build_system_prompt(self, retrieved_block: str) -> str:
        return (
            f"{BASE_SYSTEM_PROMPT}\n\n"
            f"{self.conversation.context_block()}\n\n"
            f"RELEVANT POLICY:\n{retrieved_block}"
        )

    def answer_turn(self, user_message: str) -> str:
        """Run one full turn. Returns the agent's answer."""
        # 1. Append the customer message to the buffer.
        self.conversation.add_user(user_message)

        # 2. Classify (logged; used to set ticket_type on first turn if unknown).
        ticket_type = classify_ticket(self.provider, user_message)
        print(f"  [CLASSIFY] {ticket_type}")

        # 3-4. Long-term memory is already reachable via context_block(); RAG:
        hits = self.retriever.retrieve(user_message)
        if hits:
            print("  [RAG] hits: " + ", ".join(
                f"{h.doc_id} ({h.similarity:.2f})" for h in hits
            ))
        else:
            print("  [RAG] no coverage above threshold -> honest gap")
        retrieved_block = self.retriever.format_for_prompt(hits)

        # 5. Build the system prompt.
        system_prompt = self._build_system_prompt(retrieved_block)

        # 6. Harness loop with the LIVE buffer.
        answer = run_harnessed(
            messages=self.conversation.messages,
            ticket=self.ticket,
            provider=self.provider,
            tools=TOOLS_BY_PROVIDER[self.provider_name],
            resolve_owner=self._resolve_owner,
            dispatch=self._dispatch,
            system_prompt=system_prompt,
        )
        return answer
