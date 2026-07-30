"""
The 7 demo scenarios (assignment §4 / PLAN.md §8).

Runs them end-to-end so the demo video can walk through real output. Each
scenario is a (label, customer_id, ticket_type, messages...) tuple. Where the
scenario needs multi-turn to make its point (memory), it uses several messages
on the same Session.

Usage:
    uv run python scenarios.py                    # run all seven
    uv run python scenarios.py 1 6                # run scenarios 1 and 6 only
    uv run python scenarios.py --list             # print the scenario list

The three coverage areas graded (§3):
  Functional  — scenarios 1-5 (five distinct end-to-end)
  Craft       — scenario 6 (harness rejects cross-customer before dispatch)
  Defensible  — see README and docs/PLAN.md §11
"""

from __future__ import annotations

import argparse
import os
import sys

from agent import Session
from llm import get_provider
from ticket import Ticket

SCENARIOS = [
    {
        "n": 1, "label": "Order status",
        "customer": "cust_1001", "type": "order_status",
        "turns": ["Where is my order ord_5001?"],
    },
    {
        "n": 2, "label": "Delivery issue",
        "customer": "cust_1001", "type": "delivery",
        "turns": ["My order ord_5001 seems 10 days late. Do I get anything for that?"],
    },
    {
        "n": 3, "label": "Refund request",
        "customer": "cust_1001", "type": "refund",
        "turns": ["Can I still return ord_5002? It arrived about 3 weeks ago."],
    },
    {
        "n": 4, "label": "Subscription / account",
        "customer": "cust_2002", "type": "account",
        "turns": ["Why is my account flagged, and how do I appeal?"],
    },
    {
        "n": 5, "label": "Honest gap",
        "customer": "cust_5005", "type": "order_status",
        "turns": ["Will you cover the international customs fees on my delivery?"],
    },
    {
        "n": 6, "label": "Craft / cross-customer rejection",
        "customer": "cust_1001", "type": "order_status",
        "turns": ["Can you check the status of ord_6002 for me?"],  # NOT cust_1001's
    },
    {
        "n": 7, "label": "Long-term memory",
        "customer": "cust_1001", "type": "delivery",
        "turns": ["What was the issue with my last late order?"],
    },
]


def run_one(scenario: dict, provider, retriever=None) -> None:
    print("\n" + "=" * 72)
    print(f"SCENARIO {scenario['n']}: {scenario['label']}")
    print(f"  customer: {scenario['customer']}  |  type: {scenario['type']}")
    print("=" * 72)

    ticket = Ticket(
        ticket_id=f"tkt_demo_{scenario['n']:02d}",
        customer_id=scenario["customer"],
        ticket_type=scenario["type"],
    )
    session = Session(
        ticket=ticket,
        provider=provider,
        provider_name=os.getenv("LLM_PROVIDER", "groq"),
        retriever=retriever,  # share the retriever across scenarios to avoid re-embedding
    )
    for turn_text in scenario["turns"]:
        print(f"\ncustomer> {turn_text}")
        try:
            answer = session.answer_turn(turn_text)
        except Exception as e:
            print(f"[scenarios] error: {type(e).__name__}: {e}")
            continue
        print(f"\nagent> {answer}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the 7 demo scenarios.")
    parser.add_argument("ids", nargs="*", type=int, help="Scenario numbers to run (default: all).")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "groq"),
                        choices=["anthropic", "groq"])
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS:
            print(f"  {s['n']}. {s['label']:38s} ({s['customer']}, {s['type']})")
        return 0

    to_run = [s for s in SCENARIOS if not args.ids or s["n"] in args.ids]
    if not to_run:
        print(f"No scenarios matched {args.ids}. Use --list to see IDs.")
        return 1

    provider = get_provider(args.provider)

    # Build the retriever once; each scenario reuses it (embedding the same 7
    # docs seven times would be pointless).
    from retriever import PolicyRetriever
    print(f"[scenarios] provider={args.provider}  loading policy index...")
    retriever = PolicyRetriever()
    print(f"[scenarios] running {len(to_run)} scenario(s)\n")

    for s in to_run:
        run_one(s, provider, retriever=retriever)

    print("\n" + "=" * 72)
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
