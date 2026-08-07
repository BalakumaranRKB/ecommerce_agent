"""
RAG over the policy docs: PostgreSQL + pgvector for storage,
sentence-transformers for embeddings.

Three properties this retriever must have (assignment §2.3):

1. It genuinely retrieves — real embeddings, real vector search, not string
   matching.
2. It is TRACEABLE — every result carries the doc_id and similarity score that
   produced it, so an answer can always be traced back to the exact document
   that justifies it.
3. It knows when it does NOT know — results below MIN_SIMILARITY are discarded,
   and an empty result list is a first-class outcome meaning "nothing in the
   knowledge base covers this." An honest gap beats a confident guess.

CHANGED IN ASSIGNMENT 2 — storage only. Chroma held the index inside this
process; pgvector holds it in a table every process shares. The Retrieved shape,
the MIN_SIMILARITY floor, and the empty-list-means-gap contract are all
unchanged, which is what let this migration happen without re-tuning anything.

Why the change was necessary (docs/PLAN-assignment-2.md §5.2): an in-process
index belongs to one process. Autoscaling to a second Fargate task would give
each task its own private copy — duplicated startup cost, and two indexes that
can silently disagree. Shared state is the requirement autoscaling creates.

Cosine equivalence, which is why the threshold transferred untouched:
    Chroma cosine space : similarity = 1 - distance
    pgvector `<=>`      : cosine DISTANCE, so similarity = 1 - distance
Same metric, same scale, same 0.32 floor.

Chunking: each policy doc is a single short paragraph, so one document = one
chunk. That keeps traceability exact (a hit points at a whole doc, not a
fragment of one) and avoids chunk-boundary tuning that would buy nothing here.

Indexing: none, on purpose. See the note in db.py — at 7 rows a sequential scan
beats any ANN structure and IVFFlat has nothing to train on.

Run the dataset sanity check (reads the database; run ingest.py first):
    uv run python retriever.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import db
from eval.trajectory import record
from tracing import observe

# Where the policy docs live, resolved relative to this file so CWD never matters.
_HERE = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(_HERE, "policy_docs")

COLLECTION_NAME = "policy_docs"
EMBED_MODEL = "all-MiniLM-L6-v2"  # small, local, free; 384-dim

# Retrieval defaults.
TOP_K = 2
# Cosine similarity below which a hit is treated as "not really about this".
# Tuned empirically against the sanity check (uv run python retriever.py):
#   weakest passing hit:  subscription-cancellation at 0.37
#   strongest leaked hit: order-modification-window  at 0.30
# Floor set at 0.32 — clears the leak with margin on both sides.
MIN_SIMILARITY = 0.32


@dataclass
class Retrieved:
    """One retrieved chunk, with everything needed to trace the answer to it."""
    doc_id: str
    title: str
    text: str
    similarity: float


def load_policy_docs(docs_dir: str = DOCS_DIR) -> list[dict]:
    """Load every .md file in docs_dir. doc_id is the filename without .md;
    title is the leading '# ' heading."""
    docs = []
    for filename in sorted(os.listdir(docs_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(docs_dir, filename)
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        first_line = text.splitlines()[0] if text else ""
        title = first_line.lstrip("# ").strip() or filename
        docs.append({"doc_id": filename[:-3], "title": title, "text": text})
    if not docs:
        raise RuntimeError(f"No .md policy docs found in {docs_dir}")
    return docs


class SentenceTransformerEmbedder:
    """Local embedding model. Loaded lazily so importing this module stays cheap
    and so the ~90MB model download only happens when embeddings are needed."""

    def __init__(self, model_name: str = EMBED_MODEL):
        self.model_name = model_name
        self._model = None

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model.encode(texts, normalize_embeddings=True).tolist()


class PolicyRetriever:
    """Queries the shared policy_chunks table. Holds no index of its own —
    constructing one is cheap because there is nothing to build."""

    def __init__(
        self,
        embedder=None,
        min_similarity: float = MIN_SIMILARITY,
    ):
        self.embedder = embedder or SentenceTransformerEmbedder()
        self.min_similarity = min_similarity

    @observe(name="retrieve")
    def retrieve(self, query: str, k: int = TOP_K) -> list[Retrieved]:
        """Return up to k chunks above the similarity threshold, best first.

        An EMPTY list is meaningful: nothing in the knowledge base covers this
        question. Callers must treat that as an honest gap, not as a reason to
        answer from the model's own knowledge.

        @observe captures the query as span input and the returned Retrieved
        list as span output, so every doc_id and similarity that produced an
        answer is visible in the trace without any explicit logging. That is
        what makes a confident wrong path findable (Stage 4): an answer citing
        a plausible-but-wrong doc looks fine in the text and obvious here.

        The threshold is applied in SQL, so a row below it never leaves the
        database — "retrieved" means the same thing on both sides of the wire.
        """
        query_embedding = self.embedder([query])[0]
        rows = db.search_policy_chunks(query_embedding, k, self.min_similarity)
        hits = [
            Retrieved(
                doc_id=r["doc_id"],
                title=r["title"],
                text=r["body"],
                similarity=float(r["similarity"]),
            )
            for r in rows
        ]
        # Recorded for the eval, separately from the @observe span above. The
        # doc_ids and scores are what let a fixture assert the RIGHT doc was
        # used, not merely that retrieval happened — which is the difference
        # between catching a confident wrong path and missing it entirely.
        record(
            "retrieval",
            "retrieve",
            args={"query": query, "k": k},
            outcome="ok" if hits else "empty",
            detail={
                "doc_ids": [h.doc_id for h in hits],
                "similarities": [round(h.similarity, 4) for h in hits],
                "min_similarity": self.min_similarity,
            },
        )
        return hits

    def format_for_prompt(self, hits: list[Retrieved]) -> str:
        """Render retrieved chunks for injection into the system prompt. Each
        chunk is labelled with its doc_id so the model can cite it and a reader
        can check the citation."""
        if not hits:
            return (
                "NO RELEVANT POLICY FOUND. The knowledge base does not cover this "
                "question. Say so honestly and offer to escalate to a human agent. "
                "Do not invent a policy."
            )
        return "\n\n".join(
            f"[doc_id: {h.doc_id} | similarity: {h.similarity:.2f}]\n{h.text}"
            for h in hits
        )


# ---------------------------------------------------------------------------
# Dataset sanity check (assignment §7): for each question, name the doc that
# SHOULD answer it, and confirm the retriever actually returns that doc. Run
# this before wiring the retriever to an LLM — debugging retrieval directly is
# far easier than debugging it through a model.
# ---------------------------------------------------------------------------
COVERED_QUESTIONS = [
    ("Can I still return this? It arrived three weeks ago.",        "return-eligibility"),
    ("How much do I lose if I refund opened electronics?",          "refund-eligibility"),
    ("My parcel is 10 days late, do I get anything for that?",      "shipping-delay-compensation"),
    ("Why is my account flagged and how do I appeal?",              "account-suspension-appeals"),
    ("I want to stop my monthly plan and get my money back.",       "subscription-cancellation"),
    ("The item turned up smashed, what do I do?",                   "damaged-or-missing-items"),
    ("Can I change the address after ordering?",                    "order-modification-window"),
]

# Deliberately uncovered: no policy doc mentions customs, duties, import taxes,
# or price matching. These MUST return nothing above the threshold.
GAP_QUESTIONS = [
    "Will you cover the international customs fees on my delivery?",
    "Do you price match if I find it cheaper on another site?",
]


if __name__ == "__main__":
    n_indexed = db.count_policy_chunks()
    if n_indexed == 0:
        raise SystemExit(
            "No policy chunks in the database. Run:  uv run python ingest.py"
        )

    retriever = PolicyRetriever()
    print(f"Querying {n_indexed} policy chunks in postgres (pgvector)")
    print(f"Threshold: similarity >= {retriever.min_similarity}\n")

    print("=== covered questions: does the expected doc come back first? ===")
    passed = 0
    for question, expected_doc in COVERED_QUESTIONS:
        hits = retriever.retrieve(question)
        top = hits[0].doc_id if hits else None
        ok = top == expected_doc
        passed += ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {question}")
        print(f"         expected: {expected_doc}")
        if hits:
            print("         got     : " + ", ".join(f"{h.doc_id} ({h.similarity:.2f})" for h in hits))
        else:
            print("         got     : NOTHING above threshold")
    print(f"\n  {passed}/{len(COVERED_QUESTIONS)} covered questions retrieved the expected doc.")

    print("\n=== uncovered questions: the honest gap ===")
    for question in GAP_QUESTIONS:
        hits = retriever.retrieve(question)
        ok = not hits
        print(f"  [{'PASS' if ok else 'FAIL'}] {question}")
        if hits:
            print("         leaked: " + ", ".join(f"{h.doc_id} ({h.similarity:.2f})" for h in hits))
        else:
            print("         correctly returned nothing -> agent must answer honestly")

    print(
        "\nIf a covered question FAILS, fix the doc wording or the threshold.\n"
        "If an uncovered question leaks a doc, raise MIN_SIMILARITY just above\n"
        "the leaked score but below the weakest passing score above."
    )
