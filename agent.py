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

import os
from dataclasses import dataclass, field

import db
from a2a_client import send_dispute_sync
from billing_specialist.schemas import DisputeRequest, DisputeResult
from cost_ledger import ledger_context
from decisions import RefundDecision
from eval.trajectory import Trajectory, record, recording
from harness import db_owner_resolver, make_mcp_dispatch, run_harnessed
from memory import Conversation
from pii import mask
from retriever import PolicyRetriever
from semantic_cache import SemanticCache
from ticket import TICKET_TYPES, Ticket
from tools_schema import TOOLS_BY_PROVIDER
from tracing import current_trace_id, observe, trace_context

# CWP TOGGLE (docs/dropped-handoff-found.md). When set truthy, the handoff
# OMITS account_standing from the DisputeRequest -- reproducing the seeded
# dropped-handoff bug where the specialist never learns the customer is
# flagged. Left unset (the default), the handoff passes standing through
# correctly. Kept as an env flag rather than deleted code so the broken and
# fixed paths can be demonstrated against the SAME running agent, which is
# what the CWP writeup compares side by side.
DROP_STANDING_IN_HANDOFF = os.getenv("DROP_STANDING_IN_HANDOFF", "").lower() in ("1", "true", "yes")

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


@observe(name="classify")
def classify_ticket(provider, message: str) -> str:
    """Return one of TICKET_TYPES. Falls back to 'order_status' if the model
    replies with anything unexpected — better a default than a crash."""
    with ledger_context(ticket_id="classify", ticket_type="unclassified", step="classify"):
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
    provider_name: str = "anthropic"
    retriever: PolicyRetriever | SemanticCache | None = None
    conversation: Conversation = field(init=False)
    # Set after each turn; POST /chat returns it (§6.7) so an answer can be tied
    # back to the trace that produced it. None when tracing is off.
    last_trace_id: str | None = field(default=None, init=False)
    # Set after each turn; what the eval scores. Distinct from the LangFuse
    # trace on purpose (PLAN §5.4) — same events, two audiences, no coupling.
    last_trajectory: Trajectory | None = field(default=None, init=False)
    # Set whenever a refund turn completes a handoff (Phase 4 / spec §2.7).
    # This -- not the prose the customer reads -- is the source of truth for
    # the turn's refund decision: a typed RefundDecision mapped straight from
    # the specialist's typed DisputeResult, never parsed out of a sentence.
    # None whenever no refund decision was produced this turn (no pending
    # refund found, or the handoff failed and was degraded to an escalation).
    last_refund_decision: RefundDecision | None = field(default=None, init=False)

    def __post_init__(self):
        self.conversation = Conversation(ticket=self.ticket)
        # Wrap retriever in SemanticCache (Phase 7 / spec §2.9) if not already cached
        if self.retriever is None:
            self.retriever = SemanticCache(retriever=PolicyRetriever())
        elif not isinstance(self.retriever, SemanticCache):
            self.retriever = SemanticCache(retriever=self.retriever)
        self._resolve_owner = db_owner_resolver()
        self._dispatch = make_mcp_dispatch(self.ticket)

    def _build_system_prompt(self, retrieved_block: str) -> str:
        return (
            f"{BASE_SYSTEM_PROMPT}\n\n"
            f"{self.conversation.context_block()}\n\n"
            f"RELEVANT POLICY:\n{retrieved_block}"
        )

    def _find_refund_request(self, user_message: str = "") -> dict | None:
        """Find the pending refund request THIS customer most likely means.

        Order resolution is layered, most-specific first (Option 1 fix, see
        phase_1_implementation_plan.md "Known limitations"):

          1. EXPLICIT order id in the message  ("refund ord_6003") -- if the
             customer names an order, that is unambiguous; use it.
          2. ITEM-NAME match against this customer's orders ("USB-C cable" ->
             ord_6003) -- match the customer's words to an order's item.
          3. FALL BACK to the first pending request ("I want a refund" with no
             specifics) -- better to act on *a* real pending refund than none.

        Whichever order we resolve, we still only ever return a request whose
        customer_id matches the active ticket -- another customer's dispute is
        not expressible here, the same invariant the harness enforces.

        Returns the raw request dict, or None when this customer has no pending
        refund on file at all. We read the same mock REFUND_REQUESTS store the
        specialist reads (db.py has no refund table -- A3-new, intentionally
        not plumbed through Postgres for Phase 1).
        """
        from mock_data import ORDERS, REFUND_REQUESTS

        cust = self.ticket.customer_id
        pending = [
            r for r in REFUND_REQUESTS.values()
            if r["customer_id"] == cust and r["status"] == "pending"
        ]
        if not pending:
            return None
        if len(pending) == 1:
            return pending[0]

        msg = (user_message or "").lower()

        # 1. Explicit order id in the message wins.
        for req in pending:
            if req["order_id"].lower() in msg:
                print(f"  [REFUND-MATCH] explicit order id -> {req['order_id']}")
                return req

        # 2. Item-name match: does the message mention this order's item?
        #    Match on the distinctive words of the item name so "USB-C cable"
        #    in the message lines up with the "USB-C Cable" order item. We
        #    require the WHOLE item name's significant tokens to appear, to
        #    avoid a stray shared word ("case") matching the wrong order.
        for req in pending:
            order = ORDERS.get(req["order_id"])
            if not order:
                continue
            item_tokens = [t for t in order["item"].lower().split() if len(t) > 2]
            if item_tokens and all(tok in msg for tok in item_tokens):
                print(f"  [REFUND-MATCH] item name '{order['item']}' -> {req['order_id']}")
                return req

        # 3. Nothing matched the words -> fall back to the first pending, but
        #    say so, because this is exactly where a wrong-order handoff can
        #    still happen (the customer meant one we couldn't disambiguate).
        print(f"  [REFUND-MATCH] no order named/recognised in message; "
              f"falling back to first pending -> {pending[0]['order_id']}")
        return pending[0]

    def hand_off_to_billing(self, refund_request: dict) -> DisputeResult:
        """Delegate a refund dispute to the billing specialist over A2A.

        This is the typed handoff (spec §1.3): the main agent constructs a
        DisputeRequest, sends it across the process boundary to the specialist
        on port 5001, and gets back a typed DisputeResult. The main agent
        delegates the DECISION but keeps ownership of the customer-facing
        PHRASING (the LLM turns the typed result into prose downstream).

        The account_standing carried here is the crux of the dropped-handoff
        CWP: it is state the specialist cannot recover on its own (it reads a
        different data surface), so if the main agent fails to send it, the
        specialist decides from incomplete context. We fetch it from the
        account record and pass it through -- UNLESS DROP_STANDING_IN_HANDOFF
        is set, which reproduces the bug on purpose for the writeup.
        """
        account = db.fetch_account(self.ticket.customer_id)
        standing = account["standing"] if account else "active"

        if DROP_STANDING_IN_HANDOFF:
            # BROKEN PATH: omit standing -> schema default ("active") applies
            # on the specialist side, so a flagged customer looks normal.
            request = DisputeRequest(
                order_id=refund_request["order_id"],
                customer_id=refund_request["customer_id"],
                refund_amount=refund_request["amount"],
                reason=refund_request["reason"],
            )
            print("  [HANDOFF] account_standing OMITTED (DROP_STANDING_IN_HANDOFF set) "
                  "-- reproducing the dropped-handoff bug")
        else:
            # FIXED PATH: standing crosses the boundary explicitly.
            request = DisputeRequest(
                order_id=refund_request["order_id"],
                customer_id=refund_request["customer_id"],
                refund_amount=refund_request["amount"],
                reason=refund_request["reason"],
                account_standing=standing,
            )

        print(f"  [HANDOFF] -> billing specialist: order={request.order_id} "
              f"amount=INR {request.refund_amount:,} standing={request.account_standing}")
        record(
            "tool_call",
            "hand_off_to_billing",
            args={"order_id": request.order_id},
            detail={
                "agent": "billing_specialist",
                "refund_amount": request.refund_amount,
                "account_standing": request.account_standing,
            },
        )

        result = send_dispute_sync(request)

        print(f"  [HANDOFF] <- specialist decision: {result.action} "
              f"(approved={result.approved}, hitl={result.requires_human_review})")
        record(
            "tool_call",
            "hand_off_to_billing_result",
            detail={
                "action": result.action,
                "approved": result.approved,
                "requires_human_review": result.requires_human_review,
            },
        )

        # Phase 4 / spec §2.7: map the specialist's typed DisputeResult into
        # the main agent's typed RefundDecision. This is a MAPPING of an
        # already-made verdict, not a second decision -- the specialist is
        # the single source of truth for the refund verdict. From here on,
        # `decision.amount` (an int, Pydantic-validated) is what the rest of
        # this turn should trust, not any number that ends up in the
        # customer-facing prose built below.
        decision = RefundDecision.from_dispute_result(result)
        self.last_refund_decision = decision
        record(
            "tool_call",
            "refund_decision",
            detail={
                "order_id": decision.order_id,
                "amount": decision.amount,
                "action": decision.action,
                "requires_human": decision.requires_human,
                "policy_outcome": getattr(result, "policy_outcome", None),
                "settlement_outcome": getattr(result, "settlement_outcome", None),
            },
        )
        return result

    def answer_turn(self, user_message: str) -> str:
        """Run one full turn. Returns the agent's answer.

        trace_context opens the ROOT span and binds ticket identity to every
        span beneath it, so the whole turn — classify, retrieve, each harness
        decision, each tool dispatch — arrives in LangFuse as one nested tree
        rather than a scatter of unrelated spans.

        recording() collects the same events into a plain list for the eval.
        Both wrap the same call because they observe the same turn; neither
        depends on the other, and the agent runs fine with both switched off.
        """
        with recording() as trajectory:
            with trace_context(self.ticket):
                t_type = getattr(self.ticket, "ticket_type", "unclassified") or "unclassified"
                with ledger_context(ticket_id=self.ticket.ticket_id, ticket_type=t_type, step="turn_execution"):
                    answer = self._run_turn(user_message)
                    self.last_trace_id = current_trace_id()
        self.last_trajectory = trajectory
        # §4.2 egress: mask the customer-facing reply before it leaves the system.
        # (Full entity-tag replacement per §4.4 — the documented redaction choice.)
        return mask(answer)

    def _run_turn(self, user_message: str) -> str:
        # Reset per turn -- otherwise a later non-refund turn would still
        # show a stale RefundDecision left over from an earlier turn.
        self.last_refund_decision = None

        # 1. Append the customer message to the buffer.
        self.conversation.add_user(user_message)

        # 2. Classify (logged; used to set ticket_type on first turn if unknown).
        ticket_type = classify_ticket(self.provider, user_message)
        print(f"  [CLASSIFY] {ticket_type}")

        # 2b. REFUND HANDOFF. A refund dispute is not the main agent's to
        # decide -- authority to approve/reject a charge lives with the billing
        # specialist (see docs/autonomy-decision.md, Question 1). When this turn
        # is classified as a refund AND this customer has a pending refund on
        # file, we hand the DECISION off over A2A and let its typed result
        # steer the answer. The main agent still owns the customer-facing
        # phrasing: the specialist's verdict is injected into the prompt as
        # authoritative context, not spoken verbatim.
        specialist_block = ""
        if ticket_type == "refund":
            refund_request = self._find_refund_request(user_message)
            if refund_request is not None:
                try:
                    result = self.hand_off_to_billing(refund_request)
                    # Phase 5: the specialist now SETTLES, not just decides --
                    # result.settlement_outcome says what actually happened at
                    # the execution seam. Phrase THAT for the customer, not just
                    # the verdict. (Falls back gracefully if a pre-Phase-5
                    # specialist without settlement fields ever answers.)
                    settlement = getattr(result, "settlement_outcome", None)
                    specialist_block = (
                        "\n\nBILLING SPECIALIST OUTCOME (authoritative — do not "
                        "override; phrase this for the customer):\n"
                        f"  action: {result.action}\n"
                        f"  approved: {result.approved}\n"
                        f"  requires_human_review: {result.requires_human_review}\n"
                        f"  refund_amount: INR {result.refund_amount:,}\n"
                        f"  settlement_outcome: {settlement}\n"
                        f"  reasoning: {result.reasoning}\n"
                        "Phrase strictly by settlement_outcome:\n"
                        "  - 'executed': the refund has been approved AND processed; "
                        "confirm it to the customer.\n"
                        "  - 'pending_approval': it has been escalated for human "
                        "review; say so and do NOT promise an outcome.\n"
                        "  - 'rejected': it could not be approved; explain briefly "
                        "without inventing a reason.\n"
                        "  - 'duplicate_blocked': a refund on this order is already "
                        "in progress or paid; tell the customer no second refund "
                        "was issued.\n"
                        "  - 'state_changed': the order changed while under review, "
                        "so it needs another look; say it's been sent back for review.\n"
                        "  - 'expired': the review window lapsed; say it will be "
                        "re-submitted for review.\n"
                        "If settlement_outcome is empty, fall back to "
                        "requires_human_review/approved as before."
                    )
                except Exception as e:  # noqa: BLE001
                    # The handoff CAN fail (the specialist is a separate
                    # process -- the honest cost named in autonomy-decision.md).
                    # Degrade to an explicit escalation rather than guessing at
                    # a refund decision the main agent has no authority to make.
                    print(f"  [HANDOFF] FAILED: {mask(str(e))}")
                    record(
                        "tool_call",
                        "hand_off_to_billing",
                        outcome="error",
                        detail={"error": mask(str(e))},  # §4.2: error-path egress
                    )
                    specialist_block = (
                        "\n\nBILLING SPECIALIST UNREACHABLE: the refund decision "
                        "service could not be reached. Tell the customer their "
                        "refund request has been logged and will be reviewed by "
                        "the billing team — do NOT approve or reject it yourself."
                    )

        # 3-4. Long-term memory is already reachable via context_block(); RAG:
        hits = self.retriever.retrieve(user_message)
        if hits:
            print("  [RAG] hits: " + ", ".join(
                f"{h.doc_id} ({h.similarity:.2f})" for h in hits
            ))
        else:
            print("  [RAG] no coverage above threshold -> honest gap")
        retrieved_block = self.retriever.format_for_prompt(hits)

        # 5. Build the system prompt (with the specialist's verdict, if any).
        system_prompt = self._build_system_prompt(retrieved_block) + specialist_block

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
