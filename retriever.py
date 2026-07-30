"""
RAG over the policy docs: Chroma (vector store) + sentence-transformers
(embeddings), both local and free.

Three properties this retriever must have (assignment §2.3):

1. It genuinely retrieves — real embeddings, real vector search, not string
   matching.
2. It is TRACEABLE — every result carries the doc_id and similarity score that
   produced it, so an answer can always be traced back to the exact document
   that justifies it.
3. It knows when it does NOT know — results below MIN_SIMILARITY are discarded,
   and an empty result list is a first-class outcome meaning "nothing in the
   knowledge base covers this." An honest gap beats a confident guess.

Chunking: each policy doc is a single short paragraph, so one document = one
chunk. That keeps traceability exact (a hit points at a whole doc, not a
fragment of one) and avoids chunk-boundary tuning that would buy nothing here.

Storage: an in-memory Chroma client, re-indexed at startup. With 7 tiny docs,
embedding them takes milliseconds, and it removes a whole class of stale-index
bugs (edit a doc, forget to re-index, silently retrieve the old text). Swapping
to chromadb.PersistentClient(path=...) is a one-line change if the corpus ever
grows enough to make startup cost matter.

Run the dataset sanity check (embeds docs, no LLM involved):
    uv run python retriever.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import chromadb

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
    def __init__(
        self,
        embedder=None,
        docs_dir: str = DOCS_DIR,
        min_similarity: float = MIN_SIMILARITY,
    ):
        self.embedder = embedder or SentenceTransformerEmbedder()
        self.min_similarity = min_similarity
        self.docs = load_policy_docs(docs_dir)

        # Cosine space, so distance = 1 - cosine_similarity.
        client = chromadb.EphemeralClient()
        self.collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        self._index()

    def _index(self) -> None:
        embeddings = self.embedder([d["text"] for d in self.docs])
        self.collection.upsert(
            ids=[d["doc_id"] for d in self.docs],
            embeddings=embeddings,
            documents=[d["text"] for d in self.docs],
            metadatas=[{"title": d["title"]} for d in self.docs],
        )

    def retrieve(self, query: str, k: int = TOP_K) -> list[Retrieved]:
        """Return up to k chunks above the similarity threshold, best first.

        An EMPTY list is meaningful: nothing in the knowledge base covers this
        question. Callers must treat that as an honest gap, not as a reason to
        answer from the model's own knowledge.
        """
        query_embedding = self.embedder([query])[0]
        result = self.collection.query(query_embeddings=[query_embedding], n_results=k)

        hits = []
        for doc_id, distance, text, metadata in zip(
            result["ids"][0],
            result["distances"][0],
            result["documents"][0],
            result["metadatas"][0],
        ):
            similarity = 1.0 - distance  # cosine space
            if similarity >= self.min_similarity:
                hits.append(
                    Retrieved(
                        doc_id=doc_id,
                        title=metadata.get("title", doc_id),
                        text=text,
                        similarity=similarity,
                    )
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
    retriever = PolicyRetriever()
    print(f"Indexed {len(retriever.docs)} policy docs from {DOCS_DIR}")
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
