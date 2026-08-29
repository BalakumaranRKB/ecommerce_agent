"""
Probe: does this account+region have Bedrock reranker access?

Reranking is what would solve the Phase 3 bind — hybrid ranks correctly but its
scores are un-thresholdable rank buckets; a reranker returns real relevance
scores we CAN threshold for the honest gap. But reranker model access is
separate from Claude/Titan access, and (per AWS docs) the Cohere/Amazon rerank
models are NOT listed as available in ap-south-1 (Mumbai) — they're in Oregon,
Tokyo, Frankfurt, etc. This probe finds out for THIS account.

It tries the standalone rerank() API (simplest access test) against both
candidate models. If either returns real relevanceScores, reranking is viable
and we wire it into retriever.py. If both fail with access/region errors, we
fall back to the hybrid + LLM-honest-gap approach (Option 3) and document why.

    uv run python kb_rerank_probe.py
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

REGION = "ap-south-1"
client = boto3.client("bedrock-agent-runtime", region_name=REGION)

# Candidate reranker models (ARNs are region-scoped; :: means AWS-owned).
CANDIDATES = [
    f"arn:aws:bedrock:{REGION}::foundation-model/amazon.rerank-v1:0",
    f"arn:aws:bedrock:{REGION}::foundation-model/cohere.rerank-v3-5:0",
]

# A tiny relevance test: query about returns, with one on-topic and one
# off-topic document. A working reranker should score doc 0 >> doc 1.
QUERY = "Can I still return this item I bought three weeks ago?"
DOCS = [
    "Items may be returned within 30 days of delivery in resalable condition.",
    "Subscriptions renew automatically on the same calendar day each month.",
]


def try_model(model_arn: str) -> bool:
    print(f"\n--- trying: {model_arn.rsplit('/', 1)[-1]} ---")
    try:
        resp = client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": QUERY}}],
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": d},
                    },
                }
                for d in DOCS
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": model_arn},
                    "numberOfResults": 2,
                },
            },
        )
        print("  SUCCESS — reranker returned real relevance scores:")
        for r in resp["results"]:
            which = "on-topic " if r["index"] == 0 else "off-topic"
            print(f"    doc[{r['index']}] ({which}) relevanceScore = {r['relevanceScore']:.4f}")
        top = resp["results"][0]
        if top["index"] == 0:
            print("  ✓ ranked the on-topic doc first — reranker works correctly.")
        else:
            print("  ⚠ ranked off-topic first — works but odd; still, access confirmed.")
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        print(f"  FAILED [{code}]: {msg[:160]}")
        if code in ("AccessDeniedException",):
            print("  -> access not granted for this model (request it in the console).")
        elif code in ("ValidationException", "ResourceNotFoundException"):
            print("  -> model likely NOT available in this region.")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED (unexpected): {type(e).__name__}: {str(e)[:160]}")
        return False


if __name__ == "__main__":
    print(f"Probing reranker access in {REGION}...")
    any_ok = False
    for arn in CANDIDATES:
        any_ok |= try_model(arn)

    print("\n" + "=" * 60)
    if any_ok:
        print("RESULT: reranking IS available. -> Option 1 (wire reranker).")
    else:
        print("RESULT: no reranker access in this region/account.")
        print("        -> Option 3 (hybrid + LLM honest-gap), documented as a")
        print("           §3.3 finding: reranker unavailable in ap-south-1.")
    print("=" * 60)
