"""Behavior tests for end-to-end retrieval and scope filtering.

Phase 6B D5: Retriever no longer takes an embedder; embedding happens
inside qdrant.search via injected EmbeddingService. Mocks here only
target qdrant.search(query_text=..., top_k=...).
"""
from __future__ import annotations

from ekrs_rag.retrieval.retriever import EKRSRetriever


class _Qdrant:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


def _payload(scope_path, text="Temperature shall not exceed 80°C", block_id="b1"):
    return {
        "text": text,
        "scope_path": scope_path,
        "source_block_ids": [block_id],
        "token_count": 7,
        "doc_hash": f"hash-{block_id}",
        "version": 2,
        "page_numbers": [1],
    }


def test_retrieve_returns_empty_when_search_has_no_hits():
    qdrant = _Qdrant([])

    retriever = EKRSRetriever(qdrant=qdrant)

    result = retriever.retrieve("temperature limit", top_k=3)

    assert result.chunks == []
    assert qdrant.calls == [{"query_text": "temperature limit", "top_k": 3}]


def test_retrieve_filters_scope_and_extracts_evidenced_hints():
    hits = [
        (_payload([], block_id="unscoped"), 0.99),
        (_payload(["industry", "API"], block_id="wrong"), 0.95),
        (_payload(["national", "GB", "pressure"], block_id="match"), 0.8),
    ]
    retriever = EKRSRetriever(qdrant=_Qdrant(hits))

    result = retriever.retrieve(
        "temperature limit", active_scope=["national", "GB"]
    )

    assert [chunk.source_block_ids for chunk in result.chunks] == [["match"]]
    assert result.vector_scores == [0.8]
    assert result.scores == result.vector_scores
    hint = result.chunks[0].numeric_hints[0]
    assert hint.span
    assert hint.source_text == "80°C"
    assert hint.block_id == "match"
    assert hint.scope_path == ["national", "GB", "pressure"]


def test_retrieve_ranks_matching_hits_by_composite_score():
    hits = [
        (_payload(["project", "alpha"], block_id="project"), 1.0),
        (_payload(["national", "GB"], block_id="national"), 0.8),
    ]
    retriever = EKRSRetriever(qdrant=_Qdrant(hits))

    result = retriever.retrieve("temperature limit")

    assert [chunk.source_block_ids for chunk in result.chunks] == [
        ["national"],
        ["project"],
    ]
    assert result.vector_scores == [0.8, 1.0]
    assert result.scope_scores == [1.0, 0.4]
    assert result.final_scores == [1.6, 1.4]


def test_scope_match_rejects_shorter_chunk_scope():
    assert EKRSRetriever._scope_matches(
        ["national"], ["national", "GB"]
    ) is False