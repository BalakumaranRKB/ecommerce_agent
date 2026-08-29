"""
RAG over the policy docs: Amazon Bedrock Knowledge Base (Aurora PostgreSQL +
pgvector, Titan Text Embeddings v2) + Bedrock Cross-Encoder Reranker (Cohere
Rerank v3.5).

Three properties this retriever must have (assignment §2.3):

1. It genuinely retrieves — real embeddings, real vector search, via the
   managed Knowledge Base Retrieve API + cross-encoder rerank. Not string matching.
2. It is TRACEABLE — every result carries the doc_id and score that produced
   it, so an answer can always be traced back to the exact document that
   justifies it.
3. The system knows when it does NOT know — an honest gap beats a confident
   guess. (Enforced via mechanical threshold on cross-encoder relevance scores).

CHANGED IN ASSIGNMENT 3:
- Primary retrieval: Bedrock Knowledge Base (Aurora Serverless v2 + Titan v2, 1024-dim,
  ap-south-1) with HYBRID search.
- Precision & Mechanical Thresholding: Bedrock Cross-Encoder Reranker (Cohere
  Rerank v3.5, us-east-1).
  Bedrock KB hybrid rank scores (~0.500) do not provide absolute relevance margins.
  Passing the top KB candidates through Bedrock's native cross-encoder model
  (cohere.rerank-v3-5:0) produces calibrated relevance scores (0.32 - 0.79 for covered
  queries, < 0.09 for out-of-domain gap queries).
- A hard mechanical floor at MIN_SIMILARITY = 0.20 deterministically drops
  unrelated/gap queries into [] before the LLM ever sees them, restoring the code-level
  safety boundary with ZERO heavy ML libraries locally.

Run the dataset sanity check:
    uv run python retriever.py
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

from eval.trajectory import record
from tracing import observe

# ── Knowledge Base & Reranker configuration ────────────────────────────
KB_ID = os.getenv("BEDROCK_KB_ID", "URBHHV8INY")
KB_REGION = os.getenv("AWS_REGION", "ap-south-1")

RERANK_REGION = os.getenv("BEDROCK_RERANK_REGION", "us-east-1")
RERANK_MODEL = os.getenv(
    "BEDROCK_RERANK_MODEL",
    f"arn:aws:bedrock:{RERANK_REGION}::foundation-model/cohere.rerank-v3-5:0"
)

# Number of initial candidates pulled from the KB for the reranker to score
CANDIDATE_K = 5

# Final number of top chunks passed to the LLM prompt
TOP_K = 2

# Mechanical threshold: scores below 0.20 are dropped into [] (honest gap)
MIN_SIMILARITY = 0.20


def _doc_id_from_uri(uri: str) -> str:
    """s3://ordercare-policy-docs-kb/return-eligibility.md -> return-eligibility."""
    filename = uri.rsplit("/", 1)[-1]
    return filename[:-3] if filename.endswith(".md") else filename


def _title_from_text(text: str) -> str:
    """Leading '# ' heading of a chunk, matching A2's title convention."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line.lstrip("# ").strip() or first_line


@dataclass
class Retrieved:
    """One retrieved chunk, with everything needed to trace the answer to it.

    `similarity` is the calibrated relevance score returned by the cross-encoder reranker.
    """
    doc_id: str
    title: str
    text: str
    similarity: float


class PolicyRetriever:
    """Queries the managed Bedrock Knowledge Base and applies cross-encoder reranking."""

    def __init__(
        self,
        kb_id: str = KB_ID,
        region: str = KB_REGION,
        rerank_region: str = RERANK_REGION,
        rerank_model: str = RERANK_MODEL,
        min_similarity: float = MIN_SIMILARITY,
        kb_client=None,
        rerank_client=None,
    ):
        self.kb_id = kb_id
        self.min_similarity = min_similarity
        self.rerank_model = rerank_model
        self._kb_client = kb_client or boto3.client("bedrock-agent-runtime", region_name=region)
        self._rerank_client = rerank_client or boto3.client("bedrock-agent-runtime", region_name=rerank_region)

    def _retrieve_raw_kb(self, query: str, k: int = CANDIDATE_K, max_wait: int = 90) -> list[dict]:
        """Call the KB Retrieve API with hybrid search, patiently waiting out
        Aurora Serverless v2 cold-resume (scale-to-zero) if waking."""
        waited = 0
        delay = 8
        while True:
            try:
                resp = self._kb_client.retrieve(
                    knowledgeBaseId=self.kb_id,
                    retrievalQuery={"text": query},
                    retrievalConfiguration={
                        "vectorSearchConfiguration": {
                            "numberOfResults": k,
                            "overrideSearchType": "HYBRID",
                        }
                    },
                )
                return resp.get("retrievalResults", [])
            except ClientError as e:
                if "resuming after being auto-paused" in str(e) and waited < max_wait:
                    time.sleep(delay)
                    waited += delay
                    continue
                raise

    def _rerank(self, query: str, candidate_chunks: list[dict]) -> list[tuple[dict, float]]:
        """Score and re-order candidate chunks using the Bedrock cross-encoder reranker."""
        if not candidate_chunks:
            return []

        docs = [c["content"]["text"] for c in candidate_chunks]
        try:
            resp = self._rerank_client.rerank(
                queries=[{"type": "TEXT", "textQuery": {"text": query}}],
                sources=[
                    {
                        "type": "INLINE",
                        "inlineDocumentSource": {
                            "type": "TEXT",
                            "textDocument": {"text": d},
                        },
                    }
                    for d in docs
                ],
                rerankingConfiguration={
                    "type": "BEDROCK_RERANKING_MODEL",
                    "bedrockRerankingConfiguration": {
                        "modelConfiguration": {"modelArn": self.rerank_model},
                        "numberOfResults": len(docs),
                    },
                },
            )
            reranked = []
            for item in resp.get("results", []):
                idx = item["index"]
                score = float(item["relevanceScore"])
                reranked.append((candidate_chunks[idx], score))
            return reranked
        except Exception as e:
            print(f"  [WARN] reranker failed ({type(e).__name__}: {e}), falling back to KB score")
            return [(c, float(c.get("score", 0.0))) for c in candidate_chunks]

    @observe(name="retrieve")
    def retrieve(self, query: str, k: int = TOP_K) -> list[Retrieved]:
        """Return the top k hits above MIN_SIMILARITY, best first.

        If no chunk scores above MIN_SIMILARITY (0.20), returns [] — the mechanical honest gap.
        """
        raw_candidates = self._retrieve_raw_kb(query, k=CANDIDATE_K)
        if not raw_candidates:
            record("retrieval", "retrieve", args={"query": query, "k": k}, outcome="empty", detail={"doc_ids": [], "similarities": [], "gap_decision": "empty_kb"})
            return []

        reranked = self._rerank(query, raw_candidates)

        # Apply hard mechanical threshold
        passed_hits: list[Retrieved] = []
        for r, score in reranked:
            if score >= self.min_similarity:
                uri = (
                    r.get("location", {}).get("s3Location", {}).get("uri")
                    or r.get("metadata", {}).get("x-amz-bedrock-kb-source-uri", "")
                )
                text = r.get("content", {}).get("text", "")
                passed_hits.append(
                    Retrieved(
                        doc_id=_doc_id_from_uri(uri),
                        title=_title_from_text(text),
                        text=text,
                        similarity=round(score, 4),
                    )
                )

        final_hits = passed_hits[:k]

        record(
            "retrieval",
            "retrieve",
            args={"query": query, "k": k},
            outcome="ok" if final_hits else "empty",
            detail={
                "doc_ids": [h.doc_id for h in final_hits],
                "similarities": [h.similarity for h in final_hits],
                "search_type": "HYBRID+RERANK",
                "min_similarity": self.min_similarity,
                "gap_decision": "mechanical" if not final_hits else "covered",
            },
        )
        return final_hits

    def format_for_prompt(self, hits: list[Retrieved]) -> str:
        """Render retrieved chunks for the system prompt."""
        if not hits:
            return (
                "NO RELEVANT POLICY FOUND. The policy index has no document covering "
                "this topic. Tell the customer honestly that you cannot answer from "
                "policy and offer to escalate to a human agent. Do not invent numbers or terms."
            )
        docs = "\n\n".join(
            f"[doc_id: {h.doc_id}]\n{h.text}"
            for h in hits
        )
        return (
            f"RELEVANT POLICY (relevance >= {self.min_similarity}):\n\n{docs}"
        )


COVERED_QUESTIONS = [
    ("Can I still return this? It arrived three weeks ago.",        "return-eligibility"),
    ("How much do I lose if I refund opened electronics?",          "refund-eligibility"),
    ("My parcel is 10 days late, do I get anything for that?",      "shipping-delay-compensation"),
    ("Why is my account flagged and how do I appeal?",              "account-suspension-appeals"),
    ("I want to stop my monthly plan and get my money back.",       "subscription-cancellation"),
    ("The item turned up smashed, what do I do?",                   "damaged-or-missing-items"),
    ("Can I change the address after ordering?",                    "order-modification-window"),
]

GAP_QUESTIONS = [
    "Will you cover the international customs fees on my delivery?",
    "Do you price match if I find it cheaper on another site?",
]


if __name__ == "__main__":
    retriever = PolicyRetriever()
    print(f"Querying Bedrock KB {retriever.kb_id} + Cohere Reranker ({retriever.rerank_model.rsplit('/', 1)[-1]})")
    print(f"Mechanical Threshold: MIN_SIMILARITY = {retriever.min_similarity}\n")

    print("=== COVERED QUESTIONS: expect all PASS with score >= 0.20 ===")
    passed = 0
    for question, expected_doc in COVERED_QUESTIONS:
        hits = retriever.retrieve(question, k=TOP_K)
        top = hits[0].doc_id if hits else None
        score = hits[0].similarity if hits else 0.0
        ok = top == expected_doc
        passed += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {question}")
        print(f"         expected: {expected_doc} | got: {top} (score: {score:.4f})")
    print(f"\n  {passed}/{len(COVERED_QUESTIONS)} covered questions ranked the expected doc first.\n")

    print("=== GAP QUESTIONS: expect [] empty list (score < 0.20) ===")
    gap_passed = 0
    for question in GAP_QUESTIONS:
        hits = retriever.retrieve(question, k=TOP_K)
        ok = len(hits) == 0
        gap_passed += ok
        status = "PASS (empty [])" if ok else "FAIL (leaked)"
        print(f"  [{status}] {question}")
        if hits:
            print(f"         leaked doc: {hits[0].doc_id} (score: {hits[0].similarity})")
    print(f"\n  {gap_passed}/{len(GAP_QUESTIONS)} gap questions correctly dropped into [].")