"""Behavior tests for end-to-end retrieval and scope filtering."""
from __future__ import annotations

from ekrs_rag.retrieval.retriever import EKRSRetriever


class _Embedder:
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = []

    def encode(self, texts):
        self.calls.append(texts)
        return self.vectors


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


def test_retrieve_returns_empty_when_embedder_has_no_vector():
    qdrant = _Qdrant([(_payload(["national"]), 0.9)])
    retriever = EKRSRetriever(qdrant=qdrant, embedder=_Embedder([]))

    result = retriever.retrieve("temperature limit")

    assert result.chunks == []
    assert result.final_scores == []
    assert qdrant.calls == []


def test_retrieve_returns_empty_when_search_has_no_hits():
    qdrant = _Qdrant([])
    embedder = _Embedder([[0.1, 0.2]])
    retriever = EKRSRetriever(qdrant=qdrant, embedder=embedder)

    result = retriever.retrieve("temperature limit", top_k=3)

    assert result.chunks == []
    assert qdrant.calls == [{"query_vector": [0.1, 0.2], "top_k": 3}]


def test_retrieve_filters_scope_and_extracts_evidenced_hints():
    hits = [
        (_payload([], block_id="unscoped"), 0.99),
        (_payload(["industry", "API"], block_id="wrong"), 0.95),
        (_payload(["national", "GB", "pressure"], block_id="match"), 0.8),
    ]
    retriever = EKRSRetriever(
        qdrant=_Qdrant(hits), embedder=_Embedder([[0.1, 0.2]])
    )

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
    retriever = EKRSRetriever(
        qdrant=_Qdrant(hits), embedder=_Embedder([[0.1, 0.2]])
    )

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
