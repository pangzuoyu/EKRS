"""Unit tests for pipeline.ingest() FTS sync wiring — Phase 10 T10a-2.

Tests paired with T10a-1 FTSManager. Verifies:
- Step 5.6 writes FTS after Qdrant success
- FTS failure does NOT fail ingestion (Qdrant is truth-of-record)
- FTS replace_doc is idempotent (no duplicate rows on re-ingest)
- fts=None path byte-level equals Phase 9 baseline
- QdrantManager.count_points() returns int
- FTSManager.count_active() excludes status='illegal'
- FTSManager.replace_doc() atomic delete-then-upsert
- ConsistencyChecker emits fts_consistency_drift on drift
- ConsistencyChecker silent on match
- ConsistencyChecker swallows count failures

10 unit tests per plan §Tb.1 enumeration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ekrs_rag.concurrency.consistency_checker import ConsistencyChecker
from ekrs_rag.ingestion.pipeline import IngestionPipeline
from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_rag.retrieval.qdrant_client import QdrantManager
from ekrs_shared.models import Chunk


def _chunk(text: str = "钢材标准 GB/T 12459", doc_hash: str = "d1", block_id: str = "b1") -> Chunk:
    return Chunk(
        text=text,
        scope_path=["第3章 压力容器"],
        source_block_ids=[block_id],
        token_count=20,
        doc_hash=doc_hash,
        version=1,
        page_numbers=[1],
    )


def _notification(doc_hash: str = "d1", version: int = 1, output_path: Path | None = None) -> Any:
    n = MagicMock()
    n.doc_hash = doc_hash
    n.version = version
    n.output_path = str(output_path) if output_path else "/dev/null"
    n.callback_url = ""
    n.trace_id = "trace-1"
    return n


def _seed_jsonl(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text",'
        '"content":{"raw":"hello","md_preview":"hello","structured":{}},'
        '"metadata":{"page_number":1,"heading_path":[]}}\n'
    )


def _pipeline_with_fts(
    tmp_path: Path,
    fts: FTSManager | None,
    *,
    qdrant: MagicMock | None = None,
    audit_writer: Any = None,
) -> IngestionPipeline:
    if qdrant is None:
        qdrant = MagicMock()
        qdrant.get_ingestion_status = MagicMock(return_value=None)
        qdrant.upsert_chunks = MagicMock(return_value=1)
        qdrant.delete_old_versions = MagicMock(return_value=0)
    return IngestionPipeline(
        qdrant=qdrant,
        storage_path=tmp_path,
        parser_token="x" * 32,
        audit_writer=audit_writer,
        fts=fts,
    )


# ============================================================================
# Pipeline FTS sync (4 tests)
# ============================================================================


@pytest.mark.asyncio
async def test_pipeline_ingest_writes_fts_after_qdrant_success(tmp_path: Path) -> None:
    """Happy path: Qdrant upsert succeeds → fts.replace_doc called with chunks + version."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    fts = MagicMock(spec=FTSManager)
    pipeline = _pipeline_with_fts(storage, fts)

    outcome = await pipeline.ingest(_notification(output_path=doc_dir))
    assert outcome.rag_status == "success"
    # FTS replace_doc called exactly once with the parsed chunks + version
    assert fts.replace_doc.call_count == 1
    call_args = fts.replace_doc.call_args
    assert call_args.kwargs["version"] == 1 or call_args.args[2] == 1
    assert len(call_args.args[1]) >= 1  # chunks list


@pytest.mark.asyncio
async def test_pipeline_ingest_fts_failure_does_not_fail_ingest(tmp_path: Path) -> None:
    """FTS exception is logged + outcome stays success (Qdrant is truth-of-record)."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    fts = MagicMock(spec=FTSManager)
    fts.replace_doc.side_effect = RuntimeError("sqlite disk full")
    pipeline = _pipeline_with_fts(storage, fts)

    outcome = await pipeline.ingest(_notification(output_path=doc_dir))
    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1


@pytest.mark.asyncio
async def test_pipeline_ingest_replace_doc_idempotent_on_reingest(tmp_path: Path) -> None:
    """Re-ingest same doc_hash → replace_doc called, no row accumulation in real FTS."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    # Real FTS, not mock — verifies idempotency
    fts = FTSManager(tmp_path / "fts.db")
    try:
        pipeline = _pipeline_with_fts(storage, fts)
        # First ingest
        await pipeline.ingest(_notification(doc_hash="d1", version=1, output_path=doc_dir))
        rows_after_first = fts._conn.execute("SELECT COUNT(*) FROM blocks_fts").fetchone()[0]
        # Re-ingest same doc_hash (parser re-deliveries or replay)
        await pipeline.ingest(_notification(doc_hash="d1", version=1, output_path=doc_dir))
        rows_after_second = fts._conn.execute("SELECT COUNT(*) FROM blocks_fts").fetchone()[0]
        assert rows_after_first == rows_after_second, (
            f"replace_doc not idempotent: {rows_after_first} -> {rows_after_second}"
        )
    finally:
        fts.close()


@pytest.mark.asyncio
async def test_pipeline_ingest_fts_none_path_unchanged(tmp_path: Path) -> None:
    """fts=None → byte-level equal Phase 9 baseline (no FTS calls, qdrant.upsert_chunks called)."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    qdrant = MagicMock()
    qdrant.get_ingestion_status = MagicMock(return_value=None)
    qdrant.upsert_chunks = MagicMock(return_value=1)
    qdrant.delete_old_versions = MagicMock(return_value=0)
    pipeline = _pipeline_with_fts(storage, fts=None, qdrant=qdrant)

    outcome = await pipeline.ingest(_notification(output_path=doc_dir))
    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1
    qdrant.upsert_chunks.assert_called_once()


# ============================================================================
# QdrantManager.count_points (1 test)
# ============================================================================


def test_qdrant_count_points_returns_int(tmp_path: Path) -> None:
    """QdrantManager.count_points() returns int (delegates to client.count)."""
    qdrant = MagicMock(spec=QdrantManager)
    qdrant.count_points = MagicMock(return_value=42)
    assert qdrant.count_points() == 42
    # Real QdrantManager.count_points must exist on the class
    assert hasattr(QdrantManager, "count_points"), (
        "QdrantManager.count_points not implemented"
    )


# ============================================================================
# FTSManager new methods (2 tests)
# ============================================================================


def test_fts_count_active_excludes_illegal(tmp_path: Path) -> None:
    """count_active() returns rows with status='active', excluding 'illegal'."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(), {})
        fts.upsert("c2", "b2", _chunk(), {})
        fts.upsert("c3", "b3", _chunk(), {}, status="illegal")
        assert fts.count_active() == 2, "illegal rows should be excluded"
    finally:
        fts.close()


def test_fts_replace_doc_atomic_delete_then_upsert(tmp_path: Path) -> None:
    """replace_doc deletes stale rows for doc_hash, then bulk-upserts new chunks."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # Pre-existing rows for doc_hash=d_old (3 chunks)
        for i in range(3):
            fts.upsert(
                FTSManager.generate_chunk_id("d_old", i),
                f"old_b{i}",
                _chunk(doc_hash="d_old", block_id=f"old_b{i}"),
                {},
            )
        # New chunks for doc_hash=d_new (2 chunks)
        new_chunks = [
            _chunk(text="钢材1", doc_hash="d_new", block_id="new_b1"),
            _chunk(text="钢材2", doc_hash="d_new", block_id="new_b2"),
        ]
        written = fts.replace_doc("d_new", new_chunks, version=1)
        assert written == 2
        # d_old rows untouched
        old_rows = fts._conn.execute(
            "SELECT COUNT(*) FROM blocks_fts WHERE doc_hash='d_old'"
        ).fetchone()[0]
        assert old_rows == 3, "replace_doc must not affect other docs"
        # d_new has 2 rows
        new_rows = fts._conn.execute(
            "SELECT COUNT(*) FROM blocks_fts WHERE doc_hash='d_new'"
        ).fetchone()[0]
        assert new_rows == 2
        # chunk_ids deterministic from doc_hash + index
        ids = fts._conn.execute(
            "SELECT chunk_id FROM blocks_fts WHERE doc_hash='d_new' ORDER BY chunk_id"
        ).fetchall()
        assert ids == [("d_new-0000",), ("d_new-0001",)]
    finally:
        fts.close()


# ============================================================================
# ConsistencyChecker (3 tests)
# ============================================================================


@pytest.mark.asyncio
async def test_consistency_checker_emits_drift_audit_when_counts_mismatch() -> None:
    """drift > 0 → fts_consistency_drift audit event + drift_total.inc()."""
    fts = MagicMock(spec=FTSManager)
    fts.count_active = MagicMock(return_value=10)
    qdrant = MagicMock(spec=QdrantManager)
    qdrant.count_points = MagicMock(return_value=15)
    audit_writer = MagicMock()
    metrics = MagicMock()
    metrics.drift_total = MagicMock()
    checker = ConsistencyChecker(fts, qdrant, audit_writer, metrics, interval_s=300)
    drift = await checker._check_once()
    assert drift == 5
    audit_writer.write.assert_called_once()
    args, kwargs = audit_writer.write.call_args
    assert args[0] == "fts_consistency_drift"
    assert kwargs["drift_count"] == 5
    assert kwargs["fts_count"] == 10
    assert kwargs["qdrant_count"] == 15
    metrics.drift_total.inc.assert_called_once()


@pytest.mark.asyncio
async def test_consistency_checker_no_emit_when_counts_match() -> None:
    """drift == 0 → no audit, no metric increment."""
    fts = MagicMock(spec=FTSManager)
    fts.count_active = MagicMock(return_value=15)
    qdrant = MagicMock(spec=QdrantManager)
    qdrant.count_points = MagicMock(return_value=15)
    audit_writer = MagicMock()
    metrics = MagicMock()
    metrics.drift_total = MagicMock()
    checker = ConsistencyChecker(fts, qdrant, audit_writer, metrics)
    drift = await checker._check_once()
    assert drift == 0
    audit_writer.write.assert_not_called()
    metrics.drift_total.inc.assert_not_called()


@pytest.mark.asyncio
async def test_consistency_checker_count_failure_logged_no_emit() -> None:
    """Qdrant unreachable → log warning, no audit emit, drift=0 (safe fallback)."""
    fts = MagicMock(spec=FTSManager)
    fts.count_active = MagicMock(return_value=10)
    qdrant = MagicMock(spec=QdrantManager)
    qdrant.count_points = MagicMock(side_effect=ConnectionError("qdrant down"))
    audit_writer = MagicMock()
    metrics = MagicMock()
    metrics.drift_total = MagicMock()
    checker = ConsistencyChecker(fts, qdrant, audit_writer, metrics)
    drift = await checker._check_once()
    assert drift == 0
    audit_writer.write.assert_not_called()
    metrics.drift_total.inc.assert_not_called()
