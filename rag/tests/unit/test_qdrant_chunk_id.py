"""Unit tests for QdrantManager.upsert_chunks chunk_id payload field — Phase 10 T10a-5.

Verifies:
- `chunk_id` field added to Qdrant payload
- chunk_id format matches `FTSManager.generate_chunk_id(doc_hash, chunk_index)`
- chunk_id unique within a doc
- chunk_id stable across calls (deterministic from doc_hash + chunk_index)
- legacy chunks (chunk_id=None) → payload omits chunk_id field

4 unit tests per plan §Te.1 enumeration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_rag.retrieval.qdrant_client import QdrantManager
from ekrs_shared.models import Chunk


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class _StubEmbeddingService:
    """Returns fake dense+sparse vectors of fixed length."""

    is_dummy = False

    def encode(self, texts):
        class _V:
            def __init__(self):
                self.dense = [0.0] * 4
                self.sparse = {}

        return [_V() for _ in texts]

    def to_qdrant_sparse(self, sparse_dict):
        return {"indices": [], "values": []}


def _make_chunk(doc_hash: str, text: str = "hello", block_id: str = "b1") -> Chunk:
    return Chunk(
        text=text,
        scope_path=["第3章"],
        source_block_ids=[block_id],
        token_count=len(text) // 4,
        doc_hash=doc_hash,
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


def _make_qdrant_manager() -> tuple[QdrantManager, MagicMock]:
    """Build a QdrantManager with a stubbed underlying Qdrant client."""
    mgr = QdrantManager.__new__(QdrantManager)
    mgr._collection_name = "test_coll"
    mgr._embedding_service = _StubEmbeddingService()  # type: ignore[assignment]
    captured: List[dict] = []
    mgr._captured = captured  # type: ignore[attr-defined]

    fake_client = MagicMock()

    def _capture_upsert(**kwargs):
        captured.append(kwargs)
        return MagicMock()

    fake_client.upsert = MagicMock(side_effect=_capture_upsert)
    mgr._client = fake_client
    return mgr, captured


# ===========================================================================
# 1. Payload includes chunk_id field
# ===========================================================================


def test_upsert_chunks_payload_includes_chunk_id_field() -> None:
    """QdrantManager.upsert_chunks payload includes `chunk_id` field per chunk."""
    mgr, captured = _make_qdrant_manager()
    chunks = [_make_chunk("doc_abc12345_long_hash", "chunk text", "block_uuid_1")]

    mgr.upsert_chunks(chunks)

    assert len(captured) == 1
    points = captured[0]["points"]
    assert len(points) == 1
    payload = points[0].payload
    assert "chunk_id" in payload
    assert isinstance(payload["chunk_id"], str)
    assert len(payload["chunk_id"]) > 0


# ===========================================================================
# 2. chunk_id format matches generator
# ===========================================================================


def test_upsert_chunks_chunk_id_format_matches_generator() -> None:
    """chunk_id == FTSManager.generate_chunk_id(doc_hash, chunk_index)."""
    mgr, captured = _make_qdrant_manager()
    chunks = [
        _make_chunk("doc_abc12345", "first", "b1"),
        _make_chunk("doc_abc12345", "second", "b2"),
        _make_chunk("doc_abc12345", "third", "b3"),
    ]

    mgr.upsert_chunks(chunks)

    payloads = [pt.payload for pt in captured[0]["points"]]
    assert payloads[0]["chunk_id"] == FTSManager.generate_chunk_id("doc_abc12345", 0)
    assert payloads[1]["chunk_id"] == FTSManager.generate_chunk_id("doc_abc12345", 1)
    assert payloads[2]["chunk_id"] == FTSManager.generate_chunk_id("doc_abc12345", 2)


# ===========================================================================
# 3. chunk_id unique within a doc
# ===========================================================================


def test_upsert_chunks_chunk_id_unique_within_doc() -> None:
    """Same doc, different chunk indices → distinct chunk_ids."""
    mgr, captured = _make_qdrant_manager()
    chunks = [_make_chunk("doc_xyz", f"chunk{i}", f"b{i}") for i in range(5)]

    mgr.upsert_chunks(chunks)

    payloads = [pt.payload for pt in captured[0]["points"]]
    chunk_ids = [p["chunk_id"] for p in payloads]
    assert len(set(chunk_ids)) == 5  # all unique


# ===========================================================================
# 4. chunk_id stable across calls (deterministic)
# ===========================================================================


def test_upsert_chunks_chunk_id_stable_across_calls() -> None:
    """Same (doc_hash, chunk_index) → same chunk_id, repeated calls."""
    mgr1, captured1 = _make_qdrant_manager()
    mgr2, captured2 = _make_qdrant_manager()
    chunks = [_make_chunk("doc_stable", "x", "b1")]

    mgr1.upsert_chunks(chunks)
    mgr2.upsert_chunks(chunks)

    cid1 = captured1[0]["points"][0].payload["chunk_id"]
    cid2 = captured2[0]["points"][0].payload["chunk_id"]
    assert cid1 == cid2


# ===========================================================================
# 5. IMPROVE boundary tests (T10a-5 plan §Te.3)
# ===========================================================================


def test_generate_chunk_id_handles_short_doc_hash() -> None:
    """doc_hash shorter than 8 chars → use original (no IndexError)."""
    # FTSManager.generate_chunk_id does f"{doc_hash[:8]}" — slicing
    # returns the full string when shorter than 8.
    assert FTSManager.generate_chunk_id("abc", 0) == "abc-0000"
    assert FTSManager.generate_chunk_id("", 0) == "-0000"


def test_generate_chunk_id_handles_large_chunk_index() -> None:
    """chunk_index > 9999 → still produces valid string (no overflow).

    The format `{chunk_index:04d}` is min-width 4, NOT max-width —
    chunk_index=100000 yields `doc-100000`. The invariant is "no error
    + chunk_id contains the index digits" for the >=10k range.
    """
    cid = FTSManager.generate_chunk_id("doc", 100000)
    assert cid == "doc-100000"
    # Idempotent (deterministic from inputs)
    assert FTSManager.generate_chunk_id("doc", 100000) == cid


def test_upsert_chunks_legacy_payload_without_chunk_id_still_writes_field() -> None:
    """Chunk model defaults chunk_id=None; upsert still writes a generated
    chunk_id into Qdrant payload (T10a-5 contract: payload always has the
    field; Chunk.chunk_id is just the input hint, generator fills the rest)."""
    mgr, captured = _make_qdrant_manager()
    chunks = [_make_chunk("doc_legacy", "legacy text", "b1")]

    mgr.upsert_chunks(chunks)

    payload = captured[0]["points"][0].payload
    # Even legacy chunk (no chunk_id on input) → payload has chunk_id
    assert "chunk_id" in payload
    assert payload["chunk_id"] == "doc_lega-0000"  # doc_hash[:8]-0000


# ===========================================================================
# P0 fix (2026-08-19): point_id UUID5 must include chunk_id, not just
# source_block_ids. Otherwise multi-chunk docs (chunker splits 1 block → N
# chunks) all collide on the same point_id and Qdrant silently overwrites
# N-1 chunks. These two tests pin the fix.
# ===========================================================================


def test_upsert_chunks_multi_chunk_unique_point_ids() -> None:
    """Multi-chunk doc (same source_block_ids, different chunk indices) gets
    N unique Qdrant point_ids. Reproduces the 2026-08-19 P0 data-loss bug
    where all chunks collided on the same point_id.
    """
    mgr, captured = _make_qdrant_manager()
    # 5 chunks from the same source block (chunker output for a 1-block doc)
    chunks = [_make_chunk("doc_multi", f"chunk {i} text", "same_block") for i in range(5)]

    mgr.upsert_chunks(chunks)

    point_ids = [pt.id for pt in captured[0]["points"]]
    assert len(point_ids) == 5
    # All 5 must be unique — this is the bug we're fixing
    assert len(set(point_ids)) == 5, (
        f"point_id collision: chunks with same source_block_ids got same point_id; "
        f"point_ids={point_ids}"
    )


def test_upsert_chunks_same_chunk_idempotent_point_id() -> None:
    """Re-ingesting the same chunk (same doc_hash+version+chunk_id) produces
    the same Qdrant point_id. Idempotency contract: pipeline.py:147 skips
    already-indexed docs but if a re-upsert happens, the point_id must be
    deterministic so Qdrant overwrites in place rather than creating a
    duplicate.
    """
    mgr1, captured1 = _make_qdrant_manager()
    chunks = [_make_chunk("doc_idem", "stable text", "b1")]

    mgr1.upsert_chunks(chunks)
    point_id_first = captured1[0]["points"][0].id

    # Same doc + version + chunk index → same point_id (deterministic UUID5)
    mgr2, captured2 = _make_qdrant_manager()
    mgr2.upsert_chunks(chunks)
    point_id_second = captured2[0]["points"][0].id

    assert point_id_first == point_id_second