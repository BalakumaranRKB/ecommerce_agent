"""
Throwaway probe: call the Bedrock KB Retrieve API once and dump the raw shape.
Purpose: see exactly how a result carries its score and its source-document
identity, so retriever.py can map KB chunks back to our doc_ids.

    uv run python kb_probe.py

Delete after Phase 3 is wired.
"""

from __future__ import annotations

import json
import time

import boto3
from botocore.exceptions import ClientError

KB_ID = "URBHHV8INY"
REGION = "ap-south-1"

client = boto3.client("bedrock-agent-runtime", region_name=REGION)


def retrieve_with_resume_retry(query: str, max_wait: int = 90):
    """Call Retrieve, patiently waiting out Aurora Serverless v2 cold-resume.

    Aurora scale-to-zero means the first query after idle returns a
    'resuming after being auto-paused' ValidationException. We back off and
    retry until it lands active, up to max_wait seconds total.
    """
    waited = 0
    delay = 10
    while True:
        try:
            return client.retrieve(
                knowledgeBaseId=KB_ID,
                retrievalQuery={"text": query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {
                        "numberOfResults": 3,
                        "overrideSearchType": "HYBRID",
                    }
                },
            )
        except ClientError as e:
            msg = str(e)
            if "resuming after being auto-paused" in msg and waited < max_wait:
                print(f"  Aurora still resuming... waited {waited}s, sleeping {delay}s")
                time.sleep(delay)
                waited += delay
                continue
            raise


print("Calling KB (will wait out Aurora cold-resume if needed)...")
resp = retrieve_with_resume_retry(
    "Can I still return this? It arrived three weeks ago."
)

results = resp["retrievalResults"]
print(f"\ngot {len(results)} results\n")

for i, r in enumerate(results, 1):
    print(f"=== result {i} ===")
    print(f"  score: {r.get('score')}")
    # location: where the chunk came from (S3 URI etc.)
    print(f"  location: {json.dumps(r.get('location'), indent=2)}")
    # metadata: any attached metadata (may hold the source URI / filename)
    print(f"  metadata keys: {list(r.get('metadata', {}).keys())}")
    print(f"  metadata: {json.dumps(r.get('metadata'), indent=2, default=str)}")
    # content: the actual chunk text (truncated)
    text = r.get("content", {}).get("text", "")
    print(f"  content (first 100 chars): {text[:100]!r}")
    print()
