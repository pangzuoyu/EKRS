"""Phase 10 T10b-3 — Exact-match short-circuit in retriever.

When user query is a substring of one or more retrieved chunks'
``chunk.text``, retriever skips RRF fusion and returns matched chunks
directly. Global enable (NOT gated on strict mode — short-circuit is
a deterministic optimization, distinct from R6 strict-mode inference).

Tests cover:
- ``_is_exact_match`` predicate semantics (substring, multi-match,
  empty query, case-sensitivity)
- retriever integration: short-circuit triggers on match, RRF runs
  on no match
- audit emit: ``fts_searched`` fires even on short-circuit (ops visibility)
- strict mode parity: same chunk set regardless of strict flag
- scope filter respected under short-circuit
"""
from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from ekrs_rag.retrieval.rank_fusion import FusionStats
from ekrs_rag.retrieval.retriever import EKRSRetriever
from ekrs_shared.models import Chunk


def _run(coro):
    """Run async coroutine via fresh event loop (per T10a-7 pattern)."""
    return asyncio.run(coro)


def _chunk(text: str, chunk_id: str = "doc1-0000", doc_hash: str = "doc1") -> Chunk:
    """Build a minimal Chunk for testing (frozen Pydantic; no nested attrs)."""
    return Chunk(
        text=text,
        scope_path=["第1章"],
        source_block_ids=[f"b_{chunk_id}"],
        doc_hash=doc_hash,
        version=1,
        page_numbers=[1],
        numeric_hints=[],
        chunk_id=chunk_id,
    )


# ============================================================================
# Section 1: Predicate semantics
# ============================================================================


def test_is_exact_match_predicate_returns_matching_indices() -> None:
    """Single chunk whose text contains the query returns its index."""
    from ekrs_rag.retrieval.retriever import EKRSRetriever

    chunks = [_chunk("温度应在50至80℃之间")]
    indices = EKRSRetriever._is_exact_match("50至80", chunks)
    assert indices == [0]


def test_is_exact_match_no_match_returns_empty() -> None:
    """Query not substring of any chunk returns empty list."""
    from ekrs_rag.retrieval.retriever import EKRSRetriever

    chunks = [_chunk("温度不应超过100℃"), _chunk("压力1.6MPa")]
    indices = EKRSRetriever._is_exact_match("200MPa", chunks)
    assert indices == []


def test_is_exact_match_multiple_chunks_match() -> None:
    """Query substring of multiple chunks returns all indices."""
    from ekrs_rag.retrieval.retriever import EKRSRetriever

    chunks = [
        _chunk("A312-TP316 标准管道", chunk_id="c1"),
        _chunk("普通管道规格", chunk_id="c2"),
        _chunk("A312-TP316 弯头", chunk_id="c3"),
    ]
    indices = EKRSRetriever._is_exact_match("A312-TP316", chunks)
    assert sorted(indices) == [0, 2]


def test_is_exact_match_case_sensitive_default() -> None:
    """Default case-sensitive — lowercase query does NOT match uppercase."""
    from ekrs_rag.retrieval.retriever import EKRSRetriever

    chunks = [_chunk("A312-TP316")]
    indices = EKRSRetriever._is_exact_match("a312-tp316", chunks)
    assert indices == []


def test_is_exact_match_empty_query_returns_empty() -> None:
    """Empty/whitespace query returns empty (no false-positive)."""
    from ekrs_rag.retrieval.retriever import EKRSRetriever

    chunks = [_chunk("温度应在50至80℃之间"), _chunk("压力1.6MPa")]
    assert EKRSRetriever._is_exact_match("", chunks) == []
    assert EKRSRetriever._is_exact_match("   ", chunks) == []


# ============================================================================
# Section 2: Retriever integration — short-circuit triggers
# ============================================================================


class _VecQdrantSingleHit:
    """Qdrant stub returning one chunk whose text contains a query substring."""

    def __init__(self, text: str) -> None:
        self.payload = {
            "text": text,
            "scope_path": ["第1章"],
            "source_block_ids": ["b1"],
            "token_count": len(text) // 4,
            "doc_hash": "doc1",
            "version": 1,
            "page_numbers": [1],
            "chunk_id": "doc1-0000",
        }
        self.calls: list = []

    async def search(self, query_text, top_k):
        self.calls.append((query_text, top_k))
        return [(self.payload, 0.85)]


class _StubFTSEmpty:
    """FTS stub returning no hits (short-circuit never depends on FTS)."""

    def __init__(self) -> None:
        self.calls: list = []

    def search_with_payload(self, query):
        self.calls.append(query)
        return []


def test_retrieve_short_circuit_skips_rrf_when_match() -> None:
    """When query matches a chunk's text, retriever returns short_circuit=True
    and does NOT call RRF."""
    qdrant = _VecQdrantSingleHit("A312-TP316 不锈钢管道规格")
    fts = _StubFTSEmpty()
    audit = MagicMock()
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)

    # query "A312-TP316" is substring of chunk text
    rrf_calls = {"n": 0}
    real_rrf = __import__(
        "ekrs_rag.retrieval.rank_fusion", fromlist=["reciprocal_rank_fusion"]
    ).reciprocal_rank_fusion

    def counting_rrf(*args, **kwargs):
        rrf_calls["n"] += 1
        return real_rrf(*args, **kwargs)

    with patch(
        "ekrs_rag.retrieval.retriever.reciprocal_rank_fusion",
        side_effect=counting_rrf,
    ):
        result = _run(retriever.retrieve("A312-TP316", top_k=40))

    # short-circuit triggered
    assert result.short_circuit is True
    # RRF NOT called (short-circuit bypass)
    assert rrf_calls["n"] == 0, f"RRF called {rrf_calls['n']} times"
    # chunk returned
    assert len(result.chunks) == 1
    assert "A312-TP316" in result.chunks[0].text
    # vector score = 1.0 (short-circuit marker)
    assert result.vector_scores == [1.0]


def test_retrieve_no_short_circuit_when_no_match() -> None:
    """When query does NOT match any chunk text, retriever runs RRF normally."""
    qdrant = _VecQdrantSingleHit("A312-TP316 不锈钢管道规格")
    fts = _StubFTSEmpty()
    audit = MagicMock()
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)

    # query "随机查询字符串" — not substring of chunk text
    rrf_calls = {"n": 0}
    real_rrf = __import__(
        "ekrs_rag.retrieval.rank_fusion", fromlist=["reciprocal_rank_fusion"]
    ).reciprocal_rank_fusion

    def counting_rrf(*args, **kwargs):
        rrf_calls["n"] += 1
        return real_rrf(*args, **kwargs)

    with patch(
        "ekrs_rag.retrieval.retriever.reciprocal_rank_fusion",
        side_effect=counting_rrf,
    ):
        result = _run(retriever.retrieve("随机查询字符串", top_k=40))

    # short-circuit NOT triggered
    assert result.short_circuit is False
    # RRF WAS called once
    assert rrf_calls["n"] == 1


def test_retrieve_short_circuit_emits_fts_searched_with_zero_hits() -> None:
    """Short-circuit path still emits ``fts_searched`` for ops visibility."""
    qdrant = _VecQdrantSingleHit("A312-TP316 不锈钢管道规格")
    fts = _StubFTSEmpty()
    audit = MagicMock()
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)

    _run(retriever.retrieve("A312-TP316", top_k=40))

    # fts_searched emitted even on short-circuit
    fts_searched_calls = [
        c for c in audit.write.call_args_list
        if c.args and c.args[0] == "fts_searched"
    ]
    assert len(fts_searched_calls) == 1
    kwargs = fts_searched_calls[0].kwargs
    # Short-circuit: vector returned the chunk, fts wasn't queried (0),
    # overlap concept doesn't apply in short-circuit (both=0)
    assert kwargs["fts_hits"] == 0
    assert kwargs["both_hits"] == 0
    assert kwargs["vector_hits"] >= 1


# ============================================================================
# Section 3: Strict mode parity (parent §25 (c))
# ============================================================================


def test_short_circuit_strict_mode_returns_identical_chunk_set() -> None:
    """Strict vs non-strict short-circuit returns SAME chunk set (parent §25 (c)).

    Short-circuit is a deterministic optimization, not inference; strict
    mode does NOT change the chunk set, only the post-retrieval solver's
    handling of inferred constraints. We verify the retriever side.
    """
    qdrant = _VecQdrantSingleHit("温度 ≤ 80℃ 时使用 A312-TP316 标准")
    fts = _StubFTSEmpty()
    audit = MagicMock()
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)

    # Non-strict
    result_non_strict = _run(retriever.retrieve("A312-TP316", top_k=40))
    # Strict (active_scope is the only strict-mode-relevant retriever param)
    result_strict = _run(
        retriever.retrieve("A312-TP316", top_k=40, active_scope=["第1章"])
    )

    # Same chunk returned in both runs
    assert result_non_strict.short_circuit is True
    assert result_strict.short_circuit is True
    chunk_ids_non_strict = {c.chunk_id for c in result_non_strict.chunks}
    chunk_ids_strict = {c.chunk_id for c in result_strict.chunks}
    # Strict mode adds scope filter, so chunk set may be a subset —
    # but we use scope ["第1章"] matching the chunk's scope_path,
    # so the strict set is the same
    assert chunk_ids_non_strict == chunk_ids_strict


# ============================================================================
# Section 4: Scope filter respected under short-circuit
# ============================================================================


def test_short_circuit_respects_active_scope_filter() -> None:
    """Short-circuit matched chunks outside active_scope are filtered out
    (R4 scope_priority is NOT bypassed by short-circuit)."""
    # Two chunks both contain query, but in different scopes
    payload_a = {
        "text": "温度 ≤ 80℃ 高温区段",  # scope 第1章
        "scope_path": ["第1章"],
        "source_block_ids": ["b_a"],
        "token_count": 10,
        "doc_hash": "doc1",
        "version": 1,
        "page_numbers": [1],
        "chunk_id": "doc1-0001",
    }
    payload_b = {
        "text": "温度 ≤ 80℃ 低温区段",  # scope 第2章
        "scope_path": ["第2章"],
        "source_block_ids": ["b_b"],
        "token_count": 10,
        "doc_hash": "doc2",
        "version": 1,
        "page_numbers": [1],
        "chunk_id": "doc2-0001",
    }

    class _MultiVec:
        async def search(self, query_text, top_k):
            return [(payload_a, 0.9), (payload_b, 0.8)]

    qdrant = _MultiVec()
    fts = _StubFTSEmpty()
    audit = MagicMock()
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)

    # active_scope = ["第1章"] — only payload_a matches
    result = _run(
        retriever.retrieve("80℃", top_k=40, active_scope=["第1章"])
    )

    # Short-circuit triggered (both chunks contain "80℃")
    assert result.short_circuit is True
    # But scope filter only kept 第1章 chunk
    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_id == "doc1-0001"


# ============================================================================
# Section 5: RetrievalResult field defaults
# ============================================================================


def test_retrieval_result_short_circuit_field_default_false() -> None:
    """``RetrievalResult.short_circuit`` defaults to False (back-compat)."""
    from ekrs_rag.retrieval.retriever import RetrievalResult

    result = RetrievalResult(
        chunks=[], vector_scores=[], scope_scores=[], final_scores=[],
    )
    assert result.short_circuit is False
