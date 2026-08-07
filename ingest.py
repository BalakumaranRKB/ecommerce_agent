"""
Ingest — embed the policy docs and seed the reference tables, ONCE.

Deliberately a command you run, not something the agent does at startup
(docs/PLAN-assignment-2.md §5.5). Assignment 1 rebuilt its index in memory on
every boot, which was free and correct when the index lived inside one process.
Now the index is shared state in a database, and startup-ingest would mean:

  - every Fargate task re-embedding the same seven documents on boot,
  - that work landing at the worst moment, since a task is spawned precisely
    because load spiked and it should be answering, not embedding, and
  - concurrent tasks racing to write identical rows.

So: ingest writes, tasks only read.

Idempotent — every write is an upsert, so re-running after editing a policy doc
refreshes it in place. That matters because the honest-gap behaviour depends on
what is IN the corpus; a stale row silently changes what the agent can answer.

    uv run python ingest.py            # embed docs + seed reference data
    uv run python ingest.py --verify   # re-check what's in the database
"""

from __future__ import annotations

import argparse
import sys

import db
import mock_data as data
from retriever import DOCS_DIR, SentenceTransformerEmbedder, load_policy_docs


def ingest_policy_docs(docs_dir: str = DOCS_DIR) -> int:
    """Embed every policy doc and upsert it into policy_chunks."""
    docs = load_policy_docs(docs_dir)
    print(f"[ingest] loaded {len(docs)} policy docs from {docs_dir}")

    embedder = SentenceTransformerEmbedder()
    print(f"[ingest] embedding with {embedder.model_name} (first run downloads ~90MB)...")
    embeddings = embedder([d["text"] for d in docs])

    dim = len(embeddings[0])
    if dim != db.EMBED_DIM:
        raise SystemExit(
            f"[ingest] FATAL: model produced {dim}-dim vectors but policy_chunks.embedding "
            f"is vector({db.EMBED_DIM}). Changing embedding model means changing the column "
            f"type AND re-tuning MIN_SIMILARITY — the threshold does not transfer."
        )

    for doc, embedding in zip(docs, embeddings):
        db.upsert_policy_chunk(doc["doc_id"], doc["title"], doc["text"], embedding)
        print(f"[ingest]   + {doc['doc_id']}")
    return len(docs)


def verify() -> bool:
    """Prove the database holds what the agent expects. Returns True if sane."""
    ok = True

    n_chunks = db.count_policy_chunks()
    expected_chunks = len(load_policy_docs())
    print(f"[verify] policy_chunks : {n_chunks} (expected {expected_chunks})")
    ok &= n_chunks == expected_chunks

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
    parser = argparse.ArgumentParser(description="Embed policy docs and seed reference data.")
    parser.add_argument("--verify", action="store_true", help="Only re-check the database.")
    args = parser.parse_args()

    if args.verify:
        return 0 if verify() else 1

    print("[ingest] ensuring schema...")
    db.init_schema()

    n_acct, n_ord, n_tkt = db.seed_reference_data(
        data.ACCOUNTS, data.ORDERS, data.PRIOR_TICKETS
    )
    print(f"[ingest] seeded {n_acct} accounts, {n_ord} orders, {n_tkt} prior tickets")

    n_docs = ingest_policy_docs()
    print(f"[ingest] embedded {n_docs} policy docs\n")

    print("[ingest] verifying...")
    ok = verify()
    print("\n[ingest] " + ("done." if ok else "FINISHED WITH PROBLEMS — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
