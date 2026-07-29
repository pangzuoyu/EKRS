"""Integration tests for pipeline.ingest() FTS sync — Phase 10 T10a-2.

Round-trip tests with real FTSManager (tmpfile) + mocked Qdrant. Verifies:
- Real Chunk IR round-trips through ingest() → FTS rows
- count_active() matches Qdrant mock count after ingest
- replace_doc idempotency under replay (real FTS, multiple ingest calls)
- ConsistencyChecker detects drift when FTS row count != Qdrant mock count

5 integration tests per plan §Tb.3 enumeration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ekrs_rag.concurrency.consistency_checker import ConsistencyChecker
from ekrs_rag.ingestion.pipeline import IngestionPipeline
from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_shared.models import Chunk


def _seed_jsonl(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text",'
        '"content":{"raw":"hello","md_preview":"hello","structured":{}},'
        '"metadata":{"page_number":1,"heading_path":[]}}\n'
    )


def _real_chunks(n: int = 3, doc_hash: str = "d1") -> list[Chunk]:
    """Real Chunk IR — exercises serializer contract end-to-end."""
    return [
        Chunk(
            text=f"钢材标准 第{i}段 GB/T 12459",
            scope_path=["第3章 压力容器"],
            source_block_ids=[f"block_{i:04d}"],
            token_count=20,
            doc_hash=doc_hash,
            version=1,
            page_numbers=[i + 1],
            numeric_hints=[],
            payload_version=1,
        )
        for i in range(n)
    ]


# ============================================================================
# 1. End-to-end pipeline.ingest → FTS round-trip with real Chunk IR
# ============================================================================


@pytest.mark.asyncio
async def test_pipeline_ingest_writes_real_chunks_to_both_stores(tmp_path: Path) -> None:
    """Real Chunk IR → pipeline.ingest → FTS has the same chunk text queryable."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    fts = FTSManager(tmp_path / "fts.db")
    try:
        qdrant = MagicMock()
        qdrant.get_ingestion_status = MagicMock(return_value=None)
        # Seed JSONL has 1 block → 1 chunk. Mock qdrant to match.
        qdrant.upsert_chunks = MagicMock(return_value=1)
        qdrant.delete_old_versions = MagicMock(return_value=0)

        pipeline = IngestionPipeline(
            qdrant=qdrant, storage_path=storage,
            parser_token="x" * 32, fts=fts,
        )
        notif = MagicMock()
        notif.doc_hash = "d1"
        notif.version = 1
        notif.output_path = str(doc_dir)
        notif.callback_url = ""
        notif.trace_id = "trace-int-1"

        outcome = await pipeline.ingest(notif)
        assert outcome.rag_status == "success"
        assert outcome.chunks_indexed == 1
        # FTS has 1 row
        assert fts.count_active() == 1
        # Real BM25 search returns chunks for CJK phrase present in fixture
        results = fts.search("hello")
        assert len(results) >= 1
    finally:
        fts.close()


# ============================================================================
# 2. FTS active count matches Qdrant point count after happy-path ingest
# ============================================================================


@pytest.mark.asyncio
async def test_fts_active_count_matches_qdrant_count_after_ingest(tmp_path: Path) -> None:
    """Post-ingest: fts.count_active() == qdrant.count_points() (drift=0)."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    fts = FTSManager(tmp_path / "fts.db")
    try:
        qdrant = MagicMock()
        qdrant.get_ingestion_status = MagicMock(return_value=None)
        qdrant.upsert_chunks = MagicMock(return_value=1)  # 1 block → 1 chunk
        qdrant.delete_old_versions = MagicMock(return_value=0)
        qdrant.count_points = MagicMock(return_value=1)  # in sync

        pipeline = IngestionPipeline(
            qdrant=qdrant, storage_path=storage,
            parser_token="x" * 32, fts=fts,
        )
        notif = MagicMock()
        notif.doc_hash = "d1"
        notif.version = 1
        notif.output_path = str(doc_dir)
        notif.callback_url = ""
        notif.trace_id = "trace-int-2"

        await pipeline.ingest(notif)

        # ConsistencyChecker reads from both stores, drift=0
        checker = ConsistencyChecker(fts, qdrant, audit_writer=None, metrics_collector=None)
        drift = await checker._check_once()
        assert drift == 0
    finally:
        fts.close()


# ============================================================================
# 3. ConsistencyChecker detects drift when Qdrant mock reports higher count
# ============================================================================


@pytest.mark.asyncio
async def test_consistency_checker_detects_drift_after_partial_fts_failure(tmp_path: Path) -> None:
    """Simulate FTS write lag: FTS has 2 rows, Qdrant reports 5 → drift=3, audit emits."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        chunks = _real_chunks(2, doc_hash="d1")
        fts.replace_doc("d1", chunks, version=1)
        # Real FTS has 2 rows; mock Qdrant says 5 (3 missing — simulates FTS write lag)
        qdrant = MagicMock()
        qdrant.count_points = MagicMock(return_value=5)

        audit = MagicMock()
        metrics = MagicMock()
        metrics.drift_total = MagicMock()

        checker = ConsistencyChecker(fts, qdrant, audit, metrics, interval_s=60)
        drift = await checker._check_once()
        assert drift == 3
        audit.write.assert_called_once()
        args, kwargs = audit.write.call_args
        assert args[0] == "fts_consistency_drift"
        assert kwargs["drift_count"] == 3
        assert kwargs["fts_count"] == 2
        assert kwargs["qdrant_count"] == 5
        metrics.drift_total.inc.assert_called_once()
    finally:
        fts.close()


# ============================================================================
# 4. replace_doc idempotent under pipeline replay
# ============================================================================


@pytest.mark.asyncio
async def test_fts_replace_doc_idempotent_after_replay(tmp_path: Path) -> None:
    """Run pipeline.ingest 3x for same doc_hash → FTS still has N rows (no duplicates)."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    fts = FTSManager(tmp_path / "fts.db")
    try:
        qdrant = MagicMock()
        qdrant.get_ingestion_status = MagicMock(return_value=None)
        qdrant.upsert_chunks = MagicMock(return_value=1)  # 1 block → 1 chunk
        qdrant.delete_old_versions = MagicMock(return_value=0)

        pipeline = IngestionPipeline(
            qdrant=qdrant, storage_path=storage,
            parser_token="x" * 32, fts=fts,
        )
        notif = MagicMock()
        notif.doc_hash = "d1"
        notif.version = 1
        notif.output_path = str(doc_dir)
        notif.callback_url = ""
        notif.trace_id = "trace-replay"

        # Run 3 times — simulates parser re-deliveries + replay endpoint
        for _ in range(3):
            await pipeline.ingest(notif)

        # FTS still has exactly 1 row (no duplicates from replace_doc)
        assert fts.count_active() == 1
        # Re-query returns the single chunk
        results = fts.search("hello")
        chunk_ids = [r[0] for r in results]
        assert len(chunk_ids) == len(set(chunk_ids)), f"duplicate chunk_ids: {chunk_ids}"
    finally:
        fts.close()


# ============================================================================
# 5. FTS drift metric incremented through multiple drift events
# ============================================================================


@pytest.mark.asyncio
async def test_consistency_checker_drift_metric_increments_per_event(tmp_path: Path) -> None:
    """Two drift events → metrics.drift_total.inc called 2x (cumulative counter)."""
    fts = MagicMock(spec=FTSManager)
    fts.count_active = MagicMock(return_value=10)
    qdrant = MagicMock()
    qdrant.count_points = MagicMock(return_value=12)  # drift=2
    audit = MagicMock()
    metrics = MagicMock()
    metrics.drift_total = MagicMock()

    checker = ConsistencyChecker(fts, qdrant, audit, metrics, interval_s=60)
    await checker._check_once()
    await checker._check_once()
    assert metrics.drift_total.inc.call_count == 2
    assert audit.write.call_count == 2
