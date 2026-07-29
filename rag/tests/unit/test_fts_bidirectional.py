"""FTSManager 双向映射测试 — Phase 10 T10a-5.

Verifies the new `get_block_id_by_chunk_id` (inverse of T10a-1's
`get_chunk_id`):

- write chunk → get_block_id_by_chunk_id round-trip
- missing chunk_id → None

Also re-verifies T10a-1 `get_chunk_id` so we lock the bidirectional
contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_shared.models import Chunk


def _make_chunk(doc_hash: str, text: str, block_id: str) -> Chunk:
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


def test_get_block_id_by_chunk_id_returns_block_id(tmp_path: Path) -> None:
    """FTS row written → get_block_id_by_chunk_id returns the stored block_id."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        chunk = _make_chunk("d1", "hello world", "block_uuid_xyz")
        chunk_id = FTSManager.generate_chunk_id("d1", 0)
        fts.upsert(chunk_id, "block_uuid_xyz", chunk, {"doc_hash": "d1"})

        assert fts.get_block_id_by_chunk_id(chunk_id) == "block_uuid_xyz"
    finally:
        fts.close()


def test_get_block_id_by_chunk_id_returns_none_for_missing(tmp_path: Path) -> None:
    """Unknown chunk_id → None (no exception)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        assert fts.get_block_id_by_chunk_id("nonexistent-chunk-id") is None
    finally:
        fts.close()


def test_round_trip_block_id_and_chunk_id(tmp_path: Path) -> None:
    """T10a-1 get_chunk_id + T10a-5 get_block_id_by_chunk_id round-trip."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        chunk = _make_chunk("d2", "钢材标准 GB/T 12459", "block_uuid_42")
        chunk_id = FTSManager.generate_chunk_id("d2", 0)
        fts.upsert(chunk_id, "block_uuid_42", chunk, {"doc_hash": "d2"})

        # block_id → chunk_id (T10a-1)
        assert fts.get_chunk_id("block_uuid_42") == chunk_id
        # chunk_id → block_id (T10a-5 NEW)
        assert fts.get_block_id_by_chunk_id(chunk_id) == "block_uuid_42"
    finally:
        fts.close()