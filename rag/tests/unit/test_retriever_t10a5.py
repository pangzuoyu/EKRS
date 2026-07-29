"""Retriever chunk_id key_fn switch — Phase 10 T10a-5.

Verifies:
- Chunks with `chunk_id` → RRF key is chunk_id (stable across docs)
- Chunks without `chunk_id` (legacy) → RRF key falls back to
  `f"{doc_hash}:{source_block_ids[0]}"` (T10a-4 fallback path)

We force RRF to run by configuring `fts=` (even with empty FTS) — when
fts is set, the retriever takes the dual-path RRF branch instead of
the fts=None byte-level baseline. We capture the keys passed to
`reciprocal_rank_fusion` to verify the key_fn.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from ekrs_rag.retrieval.retriever import EKRSRetriever
from ekrs_shared.models import Chunk


class _MockQdrant:
    def __init__(self, hits=None):
        self.hits = hits or []

    def search(self, **kwargs):
        return self.hits


def _fts_stub_empty():
    """Empty FTS stub — triggers RRF branch in retriever."""
    stub = type("_FTSStub", (), {})()
    stub.search_with_payload = lambda query, *, limit=40, scope_filter=None: []
    stub.search = lambda query, *, limit=40, scope_filter=None: []
    return stub


def _payload(scope_path, *, block_id="b1", text="hello", doc_hash="d1", chunk_id=None):
    p = {
        "text": text,
        "scope_path": scope_path,
        "source_block_ids": [block_id],
        "token_count": 7,
        "doc_hash": doc_hash,
        "version": 1,
        "page_numbers": [1],
    }
    if chunk_id is not None:
        p["chunk_id"] = chunk_id
    return p


@pytest.mark.asyncio
async def test_retrieve_key_fn_uses_chunk_id_when_present() -> None:
    """Chunks with chunk_id → key_fn uses chunk_id, not the fallback."""
    captured_keys: list[str] = []

    def fake_rrf(ranked_lists, key_fn, k=60):
        for sublist in ranked_lists:
            for item in sublist:
                captured_keys.append(key_fn(item))
        return [], type("Stats", (), {"vector_hits": 0, "fts_hits": 0, "both_hits": 0})()

    vec_hits = [
        (_payload([], block_id="b1", chunk_id="chunk-c1"), 0.9),
        (_payload([], block_id="b2", chunk_id="chunk-c2"), 0.8),
    ]
    retriever = EKRSRetriever(qdrant=_MockQdrant(vec_hits), fts=_fts_stub_empty())

    # Patch where the retriever LOOKS UP the function, not where it's defined.
    with patch(
        "ekrs_rag.retrieval.retriever.reciprocal_rank_fusion",
        side_effect=fake_rrf,
    ):
        await retriever.retrieve("q")

    assert captured_keys == ["chunk-c1", "chunk-c2"]


@pytest.mark.asyncio
async def test_retrieve_key_fn_falls_back_to_doc_hash_for_legacy_chunks() -> None:
    """Chunks without chunk_id (legacy ingestion) → key is the fallback
    `f"{doc_hash}:{source_block_ids[0]}"` (T10a-4 behavior preserved)."""
    captured_keys: list[str] = []

    def fake_rrf(ranked_lists, key_fn, k=60):
        for sublist in ranked_lists:
            for item in sublist:
                captured_keys.append(key_fn(item))
        return [], type("Stats", (), {"vector_hits": 0, "fts_hits": 0, "both_hits": 0})()

    # NO chunk_id in payloads (legacy doc, ingested before T10a-5)
    vec_hits = [
        (_payload([], block_id="b1", doc_hash="d1"), 0.9),
        (_payload([], block_id="b2", doc_hash="d1"), 0.8),
    ]
    retriever = EKRSRetriever(qdrant=_MockQdrant(vec_hits), fts=_fts_stub_empty())

    with patch(
        "ekrs_rag.retrieval.retriever.reciprocal_rank_fusion",
        side_effect=fake_rrf,
    ):
        await retriever.retrieve("q")

    assert captured_keys == ["d1:b1", "d1:b2"]