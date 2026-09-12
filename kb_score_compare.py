"""
Compare SEMANTIC vs HYBRID scores from the KB, for the same covered + gap
questions. Purpose: hybrid returns fused RANK scores (0.500/0.492/0.484
buckets) that can't be thresholded; semantic returns real similarity scores
but may mis-rank. This prints both so we can decide the retrieval config.

    uv run python kb_score_compare.py
"""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError

KB_ID = "URBHHV8INY"
REGION = "ap-south-1"
client = boto3.client("bedrock-agent-runtime", region_name=REGION)

COVERED = [
    ("return", "Can I still return this? It arrived three weeks ago.", "return-eligibility"),
    ("refund", "How much do I lose if I refund opened electronics?", "refund-eligibility"),
    ("shipping", "My parcel is 10 days late, do I get anything for that?", "shipping-delay-compensation"),
    ("account", "Why is my account flagged and how do I appeal?", "account-suspension-appeals"),
    ("subscription", "I want to stop my monthly plan and get my money back.", "subscription-cancellation"),
    ("damaged", "The item turned up smashed, what do I do?", "damaged-or-missing-items"),
    ("address", "Can I change the address after ordering?", "order-modification-window"),
]
GAP = [
    ("customs", "Will you cover the international customs fees on my delivery?"),
    ("pricematch", "Do you price match if I find it cheaper on another site?"),
]


def retrieve(query, search_type, k=3, max_wait=90):
    waited, delay = 0, 8
    while True:
        try:
            resp = client.retrieve(
                knowledgeBaseId=KB_ID,
                retrievalQuery={"text": query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {
                        "numberOfResults": k,
                        "overrideSearchType": search_type,
                    }
                },
            )
            return resp["retrievalResults"]
        except ClientError as e:
            if "resuming after being auto-paused" in str(e) and waited < max_wait:
                time.sleep(delay); waited += delay; continue
            raise


def docid(r):
    uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
    return uri.rsplit("/", 1)[-1].removesuffix(".md")


for mode in ["SEMANTIC", "HYBRID"]:
    print("\n" + "=" * 70)
    print(f"  {mode}")
    print("=" * 70)
    print("\n  COVERED (want expected doc #1, score high & VARIED):")
    worst = 1.0
    for label, q, expected in COVERED:
        hits = retrieve(q, mode)
        top = docid(hits[0]) if hits else None
        ok = top == expected
        if ok:
            worst = min(worst, hits[0]["score"])
        line = "  ".join(f"{docid(h)}={h['score']:.3f}" for h in hits)
        print(f"    [{'OK  ' if ok else 'FAIL'}] {label:12} -> {line}")
    print(f"    weakest correct top-hit: {worst:.3f}")

    print("\n  GAP (want top score LOW):")
    best = 0.0
    for label, q in GAP:
        hits = retrieve(q, mode)
        best = max(best, hits[0]["score"] if hits else 0.0)
        line = "  ".join(f"{docid(h)}={h['score']:.3f}" for h in hits)
        print(f"    {label:12} -> {line}")
    print(f"    strongest gap top-hit: {best:.3f}")

    print(f"\n  separation: covered_worst={worst:.3f}  gap_best={best:.3f}  "
          f"{'CLEAN (gap between them)' if worst > best else 'OVERLAP'}")
