"""
Ingest — seed the reference tables, ONCE.

CHANGED IN ASSIGNMENT 3: policy-doc embedding is GONE from here. In A2 this
script embedded the seven policy docs locally (sentence-transformers) and wrote
their vectors into the pgvector `policy_chunks` table. In A3 retrieval is a
managed Bedrock Knowledge Base — the KB owns embedding and the vector store, and
its corpus is synced from S3 (see Phase 0 / docs/phase-0-findings.md). So there
is nothing for this script to embed anymore.

What remains is the OTHER half of ingest: seeding the reference tables
(accounts, orders, prior tickets) into Postgres. The agent's tools and the
billing handoff still read these through db.fetch_* accessors, so they must be
present. This is deliberately a command you run, not startup work — tasks read,
ingest writes (docs/PLAN-assignment-2.md §5.5).

Idempotent — seeding is upserts, so re-running refreshes rows in place.

    uv run python ingest.py            # seed reference data
    uv run python ingest.py --verify   # re-check what's in the database

To (re)load the policy corpus into the Knowledge Base, sync its S3 data source
in the Bedrock console instead — that path no longer runs through this script.
"""

from __future__ import annotations

import argparse
import sys

import db
import mock_data as data


def verify() -> bool:
    """Prove the database holds the reference data the agent expects. Returns
    True if sane. (Policy retrieval is verified separately by running
    retriever.py against the live Knowledge Base.)"""
    ok = True

    # Spot-check the reference tables through the same accessors the agent uses,
    # so this verifies the read path and not just the row counts.
    order = db.fetch_order("ord_5001")
    print(f"[verify] ord_5001      : {order['item'] if order else 'MISSING'}, "
          f"promised {order['promised_delivery_date'] if order else '?'} -> "
          f"actual {order['delivery_date'] if order else '?'}")
    ok &= order is not None

    owner = db.fetch_order_owner("ord_6002")
    print(f"[verify] ord_6002 owner: {owner} (harness needs this to block cross-customer)")
    ok &= owner == "cust_2002"

    account = db.fetch_account("cust_1001")
    print(f"[verify] cust_1001     : standing={account['standing'] if account else '?'}, "
          f"{len(account['order_history']) if account else 0} orders")
    ok &= account is not None and len(account["order_history"]) == 3

    tickets = db.fetch_prior_tickets("cust_1001")
    print(f"[verify] cust_1001 history: {len(tickets)} prior tickets")
    ok &= len(tickets) == 3

    # cust_5005 is deliberately absent from PRIOR_TICKETS — the first-time
    # customer case. An empty list here is correct, not a failure.
    empty = db.fetch_prior_tickets("cust_5005")
    print(f"[verify] cust_5005 history: {len(empty)} (expected 0 — first-time customer)")
    ok &= empty == []

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed reference data into Postgres.")
    parser.add_argument("--verify", action="store_true", help="Only re-check the database.")
    args = parser.parse_args()

    if args.verify:
        return 0 if verify() else 1

    print("[ingest] ensuring schema...")
    db.init_schema()

    n_acct, n_ord, n_tkt = db.seed_reference_data(
        data.ACCOUNTS, data.ORDERS, data.PRIOR_TICKETS
    )
    print(f"[ingest] seeded {n_acct} accounts, {n_ord} orders, {n_tkt} prior tickets\n")

    print("[ingest] verifying...")
    ok = verify()
    print("\n[ingest] " + ("done." if ok else "FINISHED WITH PROBLEMS — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
