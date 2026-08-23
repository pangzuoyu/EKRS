"""Unit tests for step5_helpers — Phase 13a Pre-Task A (eng-review Issue 1).

Tests the extracted pure functions from ingestion.pipeline:
- _prepare_step5: 解析 JSONL + chunk + 幂等 skip 检查;不触 encode/qdrant.upsert
- _run_step5: qdrant.upsert + fts.replace_doc + delete_old_versions;无 I/O 副作用

Single source of truth — pipeline.ingest 老路径仍消费 helper。

10 tests per plan Pre-Task A enumeration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ekrs_rag.ingestion.outcome import IngestionOutcome
from ekrs_rag.ingestion.pipeline import AuditEmitter
from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_rag.retrieval.qdrant_client import QdrantManager
from ekrs_rag.services.step5_helpers import (
    Step5Preparation,
    _prepare_step5,
    _run_step5,
)
from ekrs_shared.models import Chunk


def _chunk(text: str = "钢材标准 GB/T 12459", doc_hash: str = "d1", block_id: str = "b1") -> Chunk:
    return Chunk(
        chunk_id=None,
        text=text,
        doc_hash=doc_hash,
        block_id=block_id,
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


def _seed_jsonl(path: Path, raw: str = "hello") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "data.jsonl").write_text(
        f'{{"doc_id":"d1","block_id":"b1","type":"text",'
        f'"content":{{"raw":"{raw}","md_preview":"{raw}","structured":{{}}}},'
        f'"metadata":{{"page_number":1,"heading_path":[]}}}}\n'
    )


def _qdrant_mock(*, ingestion_status: Any = None, upsert_count: int = 1) -> Any:
    q = MagicMock(spec=QdrantManager)
    q.get_ingestion_status = MagicMock(return_value=ingestion_status)
    q.upsert_chunks = MagicMock(return_value=upsert_count)
    q.delete_old_versions = MagicMock(return_value=0)
    return q


# ============================================================================
# _prepare_step5 (5 tests)
# ============================================================================


def test_prepare_step5_returns_chunks_for_valid_jsonl(tmp_path: Path) -> None:
    """Happy path: valid JSONL → returns Step5Preparation with chunks, no outcome."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)
    qdrant = _qdrant_mock()
    audit = MagicMock(spec=AuditEmitter)

    prep = _prepare_step5(_notification(output_path=doc_dir), qdrant, storage, audit)

    assert prep.chunks is not None
    assert len(prep.chunks) >= 1
    assert prep.outcome is None
    assert prep.skip_reason is None
    qdrant.get_ingestion_status.assert_called_once_with("d1")


def test_prepare_step5_idempotent_skip_returns_duplicate_outcome(tmp_path: Path) -> None:
    """Already indexed: returns outcome with rag_status='success' (idempotent skip)."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)
    existing = MagicMock()
    existing.status = "success"
    existing.version = 1
    existing.chunks_indexed = 42
    qdrant = _qdrant_mock(ingestion_status=existing)
    audit = MagicMock(spec=AuditEmitter)

    prep = _prepare_step5(_notification(output_path=doc_dir), qdrant, storage, audit)

    assert prep.chunks is None
    assert prep.outcome is not None
    assert prep.outcome.rag_status == "success"
    assert prep.outcome.chunks_indexed == 42
    assert prep.skip_reason == "duplicate"


def test_prepare_step5_missing_jsonl_returns_failed_outcome(tmp_path: Path) -> None:
    """output_path has no data.jsonl → returns failed outcome."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    doc_dir.mkdir(parents=True)
    qdrant = _qdrant_mock()
    audit = MagicMock(spec=AuditEmitter)

    prep = _prepare_step5(_notification(output_path=doc_dir), qdrant, storage, audit)

    assert prep.chunks is None
    assert prep.outcome is not None
    assert prep.outcome.rag_status == "failed"
    assert prep.outcome.error_code == "jsonl_missing"
    assert prep.skip_reason == "jsonl_missing"


def test_prepare_step5_empty_jsonl_returns_business_failure(tmp_path: Path) -> None:
    """JSONL exists but is empty → returns failed outcome."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    doc_dir.mkdir(parents=True)
    (doc_dir / "data.jsonl").write_text("")
    qdrant = _qdrant_mock()
    audit = MagicMock(spec=AuditEmitter)

    prep = _prepare_step5(_notification(output_path=doc_dir), qdrant, storage, audit)

    assert prep.chunks is None
    assert prep.outcome is not None
    assert prep.outcome.rag_status == "failed"
    assert prep.outcome.error_code == "jsonl_empty"


def test_prepare_step5_ir_parse_error_returns_failed_outcome(tmp_path: Path) -> None:
    """Malformed JSONL → returns failed outcome with ir_parse_error."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    doc_dir.mkdir(parents=True)
    (doc_dir / "data.jsonl").write_text("{not valid json")
    qdrant = _qdrant_mock()
    audit = MagicMock(spec=AuditEmitter)

    prep = _prepare_step5(_notification(output_path=doc_dir), qdrant, storage, audit)

    assert prep.chunks is None
    assert prep.outcome is not None
    assert prep.outcome.rag_status == "failed"
    assert prep.outcome.error_code == "ir_parse_error"


# ============================================================================
# _run_step5 (5 tests)
# ============================================================================


def test_run_step5_happy_path_qdrant_fts_audit(tmp_path: Path) -> None:
    """Success path: qdrant.upsert → fts.replace_doc → delete_old_versions all called."""
    qdrant = _qdrant_mock(upsert_count=3)
    fts = MagicMock(spec=FTSManager)
    audit = MagicMock(spec=AuditEmitter)
    chunks = [_chunk(), _chunk(text="block2", block_id="b2"), _chunk(text="block3", block_id="b3")]

    outcome = _run_step5(chunks, qdrant, fts, audit, doc_hash="d1", version=2)

    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 3
    qdrant.upsert_chunks.assert_called_once_with(chunks)
    fts.replace_doc.assert_called_once()
    qdrant.delete_old_versions.assert_called_once_with("d1", keep_version=2)
    audit.write.assert_called_once()
    audit_call = audit.write.call_args
    assert audit_call.args[0] == "fts_synced"
    assert audit_call.kwargs["doc_hash"] == "d1"
    assert audit_call.kwargs["version"] == 2
    assert audit_call.kwargs["chunks_written"] == 3


def test_run_step5_qdrant_failure_returns_failed_outcome(tmp_path: Path) -> None:
    """qdrant.upsert raises → returns failed outcome, fts NOT called."""
    qdrant = MagicMock(spec=QdrantManager)
    qdrant.upsert_chunks = MagicMock(side_effect=RuntimeError("qdrant down"))
    qdrant.delete_old_versions = MagicMock()
    fts = MagicMock(spec=FTSManager)
    audit = MagicMock(spec=AuditEmitter)
    chunks = [_chunk()]

    outcome = _run_step5(chunks, qdrant, fts, audit, doc_hash="d1", version=1)

    assert outcome.rag_status == "failed"
    assert outcome.error_code == "qdrant_upsert_failed"
    fts.replace_doc.assert_not_called()
    qdrant.delete_old_versions.assert_not_called()


def test_run_step5_fts_failure_does_not_fail_outcome(tmp_path: Path) -> None:
    """FTS replace_doc raises → qdrant write still stands, outcome=success."""
    qdrant = _qdrant_mock(upsert_count=2)
    fts = MagicMock(spec=FTSManager)
    fts.replace_doc = MagicMock(side_effect=RuntimeError("fts sync error"))
    audit = MagicMock(spec=AuditEmitter)
    chunks = [_chunk(), _chunk(text="b2", block_id="b2")]

    outcome = _run_step5(chunks, qdrant, fts, audit, doc_hash="d1", version=1)

    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 2


def test_run_step5_fts_none_path_byte_level_baseline(tmp_path: Path) -> None:
    """fts=None: no FTS write, no audit emit, outcome still success."""
    qdrant = _qdrant_mock(upsert_count=1)
    audit = MagicMock(spec=AuditEmitter)
    chunks = [_chunk()]

    outcome = _run_step5(chunks, qdrant, fts=None, audit_writer=audit, doc_hash="d1", version=1)

    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1
    audit.write.assert_not_called()


def test_run_step5_delete_old_versions_failure_does_not_fail_outcome(tmp_path: Path) -> None:
    """delete_old_versions raises → outcome still success (best-effort)."""
    qdrant = MagicMock(spec=QdrantManager)
    qdrant.upsert_chunks = MagicMock(return_value=1)
    qdrant.delete_old_versions = MagicMock(side_effect=RuntimeError("delete failed"))
    fts = MagicMock(spec=FTSManager)
    audit = MagicMock(spec=AuditEmitter)
    chunks = [_chunk()]

    outcome = _run_step5(chunks, qdrant, fts, audit, doc_hash="d1", version=1)

    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1