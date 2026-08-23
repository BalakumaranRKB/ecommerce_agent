"""
Reproduce the confident-wrong-path finding — deterministically, in seconds.

No LLM, no API key, no cost. Just the retriever against the same seven policy
documents, so the same query always produces the same scores. That determinism
is the point: a finding you can only describe is an anecdote, one you can
re-run is evidence.

WHAT THIS SHOWS
Each block below pairs a CONTROL query (one of retriever.py's own
COVERED_QUESTIONS, which passes) against VARIANT queries that ask the same
question the way a real customer would — with an order id, a product name, some
narrative. The control retrieves the right document. The variants do not.

The retriever is not broken in general. It is brittle to paraphrase, which is
much harder to notice and is exactly what a fixed list of sanity questions
cannot catch: those questions were written by the same person who wrote the
documents, in the same vocabulary.

    uv run python -m eval.reproduce_cwp
    uv run python -m eval.reproduce_cwp --hypothesis   # also test the proposed fix

Requires: docker compose up, and `uv run python ingest.py` already run.
"""

from __future__ import annotations

import argparse
import re
import sys

import db
from retriever import PolicyRetriever

# Each case: (label, query, expected_doc_id, is_control)
CASES: list[tuple[str, str, str, bool]] = [
    # ---- Instance 1 & 2: the shipping-delay policy ----
    (
        "CONTROL   (retriever.py COVERED_QUESTIONS)",
        "My parcel is 10 days late, do I get anything for that?",
        "shipping-delay-compensation",
        True,
    ),
    (
        "INSTANCE 1 (Stage 1, first traced ticket)",
        "My order ord_5001 hasn't arrived and it's well past the date I was promised. "
        "Do I get anything for that?",
        "shipping-delay-compensation",
        False,
    ),
    (
        "INSTANCE 2 (Stage 2, post-migration check)",
        "can you tell me about the delivery status of ord_5001",
        "shipping-delay-compensation",
        False,
    ),
    # ---- Instance 3: the damaged-items policy ----
    (
        "CONTROL   (retriever.py COVERED_QUESTIONS)",
        "The item turned up smashed, what do I do?",
        "damaged-or-missing-items",
        True,
    ),
    (
        "INSTANCE 3 (Stage 3, delivery_damaged fixture)",
        "The desk lamp from ord_8005 turned up smashed, what do I do?",
        "damaged-or-missing-items",
        False,
    ),
]

# The proposed fix: strip order ids and known product names before EMBEDDING.
# The tool call still receives the full untouched message — this only affects
# what gets turned into a vector.
_ORDER_ID = re.compile(r"\bord_\d+\b", re.I)
_PRODUCT_NAMES = [
    "wireless earbuds", "phone case", "notebook", "laptop stand",
    "usb-c cable", "mechanical keyboard", "desk lamp", "27-inch monitor",
]


def strip_specifics(query: str) -> str:
    """Remove order ids and product names, leaving the intent."""
    cleaned = _ORDER_ID.sub(" ", query)
    lowered = cleaned.lower()
    for name in _PRODUCT_NAMES:
        idx = lowered.find(name)
        while idx != -1:
            cleaned = cleaned[:idx] + " " * len(name) + cleaned[idx + len(name):]
            lowered = cleaned.lower()
            idx = lowered.find(name)
    return re.sub(r"\s+", " ", cleaned).strip()


def show(retriever: PolicyRetriever, query: str, expected: str) -> bool:
    """Run one query, print what came back, return whether expected was found."""
    hits = retriever.retrieve(query)
    found = any(h.doc_id == expected for h in hits)
    if hits:
        rendered = ", ".join(f"{h.doc_id} ({h.similarity:.2f})" for h in hits)
    else:
        rendered = "NOTHING above threshold"
    print(f"      got      : {rendered}")
    print(f"      expected : {expected}   ->  {'FOUND' if found else 'MISSING'}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproduce the CWP finding.")
    parser.add_argument("--hypothesis", action="store_true",
                        help="Also re-run failing queries with specifics stripped.")
    args = parser.parse_args()

    if db.count_policy_chunks() == 0:
        print("No policy chunks in the database. Run: uv run python ingest.py")
        return 2

    retriever = PolicyRetriever()
    print(f"threshold: similarity >= {retriever.min_similarity}")
    print("=" * 74)

    failures = []
    for label, query, expected, is_control in CASES:
        print(f"\n  {label}")
        print(f'      query    : "{query}"')
        found = show(retriever, query, expected)
        if is_control and not found:
            print("      !! control failed — the corpus or threshold changed since this was written")
        if not is_control and not found:
            failures.append((label, query, expected))

    print("\n" + "=" * 74)
    print(f"{len(failures)} of {sum(1 for c in CASES if not c[3])} realistic phrasings "
          f"failed to retrieve their policy.")
    print("\nThe controls pass. The realistic phrasings do not. Same questions,")
    print("same documents, same threshold — only the wording differs, and the")
    print("wording that fails is the wording a customer would actually use.")

    if args.hypothesis and failures:
        print("\n" + "=" * 74)
        print("HYPOTHESIS: order ids and product names dilute the query embedding.")
        print("Re-running each failure with those stripped, everything else identical.\n")
        recovered = 0
        for label, query, expected in failures:
            cleaned = strip_specifics(query)
            print(f"  {label}")
            print(f'      before   : "{query}"')
            print(f'      after    : "{cleaned}"')
            if show(retriever, cleaned, expected):
                recovered += 1
            print()
        print(f"{recovered}/{len(failures)} recovered after stripping specifics.")
        if recovered == len(failures):
            print("Hypothesis holds: the extra tokens were the cause, not the phrasing itself.")
        elif recovered:
            print("Hypothesis partly holds — some failures have another cause too.")
        else:
            print("Hypothesis rejected: stripping specifics changed nothing. Look elsewhere.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
