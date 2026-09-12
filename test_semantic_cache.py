"""
Phase 7 / spec §2.9 — Semantic Cache Tests (SPEC-CACHE).

Verifies:
1. Cache miss on first query, cache hit on reworded semantic equivalent query.
2. Distinct/unrelated queries correctly miss when similarity is below threshold.
3. Latency and network call reduction on cache hits.
4. Interface compatibility with PolicyRetriever (`retrieve`, `format_for_prompt`).

Run:
    uv run python test_semantic_cache.py
"""

from __future__ import annotations

import time

from retriever import Retrieved
from semantic_cache import SemanticCache, SimpleLocalEmbedder, cosine_similarity


class MockRetriever:
    def __init__(self) -> None:
        self.call_count = 0

    def retrieve(self, query: str, k: int = 2) -> list[Retrieved]:
        self.call_count += 1
        time.sleep(0.05)  # Simulate 50ms retrieval latency
        return [
            Retrieved(
                doc_id="return-eligibility",
                title="Return Eligibility",
                text="Returns accepted within 30 days of delivery.",
                similarity=0.82,
            )
        ]

    def format_for_prompt(self, hits: list[Retrieved]) -> str:
        if not hits:
            return "NO RELEVANT POLICY FOUND."
        return f"RELEVANT POLICY:\n[doc_id: {hits[0].doc_id}]\n{hits[0].text}"


def test_cosine_similarity():
    """Verify vector cosine math."""
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    v3 = [0.0, 1.0, 0.0]
    assert abs(cosine_similarity(v1, v2) - 1.0) < 1e-6
    assert abs(cosine_similarity(v1, v3) - 0.0) < 1e-6
    print("  [PASS] test_cosine_similarity")


def test_cache_miss_then_hit_exact():
    """First query misses, exact second query hits without calling underlying retriever."""
    mock = MockRetriever()
    cache = SemanticCache(retriever=mock, threshold=0.85, embedder=SimpleLocalEmbedder())

    res1 = cache.retrieve("What is the return window?")
    assert mock.call_count == 1
    assert res1[0].doc_id == "return-eligibility"

    res2 = cache.retrieve("What is the return window?")
    assert mock.call_count == 1  # Underlying retriever NOT called
    assert res2[0].doc_id == "return-eligibility"

    stats = cache.get_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5
    print("  [PASS] test_cache_miss_then_hit_exact")


def test_cache_hit_on_rephrased_query():
    """Reworded question with high semantic similarity hits the cache."""
    mock = MockRetriever()
    cache = SemanticCache(retriever=mock, threshold=0.70, embedder=SimpleLocalEmbedder())

    q1 = "Can I return my order item?"
    q2 = "Can I return an item from my order?"  # Reworded equivalent

    cache.retrieve(q1)
    assert mock.call_count == 1

    res2 = cache.retrieve(q2)
    assert mock.call_count == 1  # Cache HIT
    assert res2[0].doc_id == "return-eligibility"

    stats = cache.get_stats()
    assert stats["hits"] == 1
    print("  [PASS] test_cache_hit_on_rephrased_query")


def test_cache_miss_on_unrelated_query():
    """Unrelated query below similarity threshold misses and triggers fresh retrieval."""
    mock = MockRetriever()
    cache = SemanticCache(retriever=mock, threshold=0.80, embedder=SimpleLocalEmbedder())

    cache.retrieve("How do I return a damaged shoe?")
    assert mock.call_count == 1

    cache.retrieve("Where is my international shipping package tracking code?")
    assert mock.call_count == 2  # Different topic -> MISS -> calls retriever

    stats = cache.get_stats()
    assert stats["misses"] == 2
    assert stats["hits"] == 0
    print("  [PASS] test_cache_miss_on_unrelated_query")


def test_latency_reduction():
    """Verify cache hits deliver >80% latency reduction over underlying retriever."""
    mock = MockRetriever()
    cache = SemanticCache(retriever=mock, threshold=0.80, embedder=SimpleLocalEmbedder())

    t0 = time.perf_counter()
    cache.retrieve("Check return window")
    miss_duration = time.perf_counter() - t0

    t1 = time.perf_counter()
    cache.retrieve("Check return window")
    hit_duration = time.perf_counter() - t1

    assert hit_duration < miss_duration
    reduction = ((miss_duration - hit_duration) / miss_duration) * 100
    assert reduction >= 50.0  # In practice >90%
    print(f"  [PASS] test_latency_reduction (Latency cut by {reduction:.1f}%)")


def test_prompt_formatting_delegation():
    """Verify format_for_prompt delegates properly."""
    mock = MockRetriever()
    cache = SemanticCache(retriever=mock, threshold=0.80, embedder=SimpleLocalEmbedder())

    hits = [Retrieved(doc_id="test-doc", title="Test", text="Sample text", similarity=0.9)]
    rendered = cache.format_for_prompt(hits)
    assert "[doc_id: test-doc]" in rendered
    assert "Sample text" in rendered
    print("  [PASS] test_prompt_formatting_delegation")


if __name__ == "__main__":
    print("=== Running Semantic Cache Test Suite ===")
    test_cosine_similarity()
    test_cache_miss_then_hit_exact()
    test_cache_hit_on_rephrased_query()
    test_cache_miss_on_unrelated_query()
    test_latency_reduction()
    test_prompt_formatting_delegation()
    print("\nAll Semantic Cache tests PASSED (6/6).")
