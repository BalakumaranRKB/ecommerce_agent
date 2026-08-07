"""
CLI entry point — hold a multi-turn conversation with the agent.

Usage:
    uv run python cli.py                              # defaults to cust_1001, refund
    uv run python cli.py --customer cust_2002 --type account
    uv run python cli.py --ticket-id tkt_9001 --customer cust_1001 --type delivery

The customer_id is asked for at startup rather than derived from anything the
user later says, because the customer is the SCOPING AUTHORITY (see docs/PLAN.md
§4). Every tool call the agent proposes will be checked against this customer
before dispatch; changing it mid-conversation would violate that boundary.

Type `exit`, `quit`, or Ctrl+C to end.
"""

from __future__ import annotations

import argparse
import os
import sys

from agent import Session
from llm import get_provider
from ticket import TICKET_TYPES, Ticket
from tracing import ENABLED as TRACING_ENABLED
from tracing import flush as flush_traces


def main() -> int:
    parser = argparse.ArgumentParser(description="E-commerce order-support agent CLI.")
    parser.add_argument("--ticket-id", default="tkt_9001", help="Active ticket ID for this conversation.")
    parser.add_argument("--customer",  default="cust_1001", help="Customer ID (scoping authority).")
    parser.add_argument("--type", choices=TICKET_TYPES, default="refund",
                        help="Initial ticket type; may be reclassified per turn in logs.")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "anthropic"),
                        choices=["anthropic", "groq"])
    args = parser.parse_args()

    ticket = Ticket(ticket_id=args.ticket_id, customer_id=args.customer, ticket_type=args.type)
    provider = get_provider(args.provider)

    print(f"[cli] provider={args.provider}  ticket={ticket}")
    print(f"[cli] tracing={'on' if TRACING_ENABLED else 'off'}")
    print("[cli] loading policy index (first run downloads the embedding model)...")
    session = Session(ticket=ticket, provider=provider, provider_name=args.provider)
    print("[cli] ready. type 'exit' to quit.\n")

    while True:
        try:
            user_message = input("customer> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_message:
            continue
        if user_message.lower() in {"exit", "quit"}:
            return 0

        try:
            answer = session.answer_turn(user_message)
        except Exception as e:  # keep the conversation alive on transient errors
            print(f"[cli] error: {type(e).__name__}: {e}\n")
            continue
        print(f"\nagent> {answer}\n")
        if session.last_trace_id:
            print(f"[cli] trace: {session.last_trace_id}\n")


if __name__ == "__main__":
    # Flush wraps every exit path, not just the clean one. The exporter batches
    # in a background thread, so a CLI that exits promptly can die before the
    # spans are sent — the "my code ran but the dashboard is empty" trap. Three
    # `return 0` paths in main() makes a single wrapper safer than three calls.
    try:
        _code = main()
    finally:
        flush_traces()
    sys.exit(_code)
