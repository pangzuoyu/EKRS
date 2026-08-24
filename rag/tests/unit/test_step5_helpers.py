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
from ekrs_rag.retrieval.embedding_service import EncodedVector
from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_rag.retrieval.qdrant_client import QdrantManager
from ekrs_rag.services import encoding_router
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


class _StubRouter:
    """Process-local EncodingRouter stub for _run_step5 tests.

    Phase 13b T3.2: _run_step5 dispatches EncodingRouter.route() on every
    ingest. Tests replace get_router() so the dispatch is deterministic and
    doesn't trigger the real GPU self-check or CPU encoder.
    """

    def __init__(self, vec_factory: Any | None = None) -> None:
        self._vec_factory = vec_factory or self._default_vec
        self.route_calls: list[list[str]] = []
        self.current_channel = "cpu"

    @staticmethod
    def _default_vec(texts: list[str]) -> list[EncodedVector]:
        return [EncodedVector(dense=[0.1] * 1024, sparse={i: 0.5}) for i, _ in enumerate(texts)]

    def route(self, texts: list[str]) -> list[EncodedVector]:
        self.route_calls.append(list(texts))
        return self._vec_factory(texts)


@pytest.fixture(autouse=True)
def _stub_encoding_router(monkeypatch: pytest.MonkeyPatch) -> _StubRouter:
    """Auto-stub EncodingRouter.get_router for every test in this module.

    Without this, _run_step5 would call the real router (which spawns
    self_check or loads ONNX). Tests that need a custom stub can override
    via ``monkeypatch.setattr(encoding_router, "get_router", ...)``.
    """
    stub = _StubRouter()
    monkeypatch.setattr(encoding_router, "get_router", lambda: stub)
    return stub


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
    qdrant.upsert_chunks.assert_called_once()
    call = qdrant.upsert_chunks.call_args
    assert call.args[0] == chunks  # positional
    assert "precomputed_encodings" in call.kwargs  # Phase 13b T3.2 kwarg
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


# ============================================================================
# Phase 13b T3.2 — _run_step5 EncodingRouter dispatch
# ============================================================================


def test_run_step5_uses_encoding_router_precomputed_kwarg(
    _stub_encoding_router: _StubRouter,
) -> None:
    """Phase 13b T3.2: _run_step5 calls EncodingRouter.route() and passes result to qdrant.

    Review 🔴 #2 mandate: EncodingRouter is dispatched via the
    precomputed_encodings kwarg (NOT via the T9 _encode_backend seam which
    is dense-only). Verifies route() is invoked with chunk texts and the
    returned EncodedVector list is forwarded to QdrantManager.upsert_chunks.
    """
    vec_a = EncodedVector(dense=[0.1] * 1024, sparse={1: 0.5})
    vec_b = EncodedVector(dense=[0.2] * 1024, sparse={2: 0.3})
    _stub_encoding_router._vec_factory = lambda texts: [vec_a, vec_b]

    qdrant = _qdrant_mock(upsert_count=2)
    audit = MagicMock(spec=AuditEmitter)
    chunks = [_chunk("hello"), _chunk("world", block_id="b2")]

    outcome = _run_step5(chunks, qdrant, fts=None, audit_writer=audit, doc_hash="d1", version=1)

    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 2
    # Router.route() was called with the chunk texts.
    assert _stub_encoding_router.route_calls == [["hello", "world"]]
    # Qdrant received the precomputed vectors via kwarg.
    qdrant.upsert_chunks.assert_called_once()
    call_kwargs = qdrant.upsert_chunks.call_args.kwargs
    assert call_kwargs.get("precomputed_encodings") == [vec_a, vec_b]


def test_run_step5_router_cpu_fallback_propagates(
    _stub_encoding_router: _StubRouter,
) -> None:
    """Phase 13b T3.2: when router.route() returns CPU vectors, precomputed kwarg
    is still populated and upsert proceeds.

    Confirms the router's CPU fallback path lands in the qdrant call —
    no special branching in _run_step5 (review edge case #1).
    """
    cpu_vec = EncodedVector(dense=[0.5] * 1024, sparse={10: 0.4})
    _stub_encoding_router._vec_factory = lambda texts: [cpu_vec]
    _stub_encoding_router.current_channel = "cpu"  # GPU never registered

    qdrant = _qdrant_mock(upsert_count=1)
    audit = MagicMock(spec=AuditEmitter)
    chunks = [_chunk("test")]

    outcome = _run_step5(chunks, qdrant, fts=None, audit_writer=audit, doc_hash="d1", version=1)

    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1
    assert _stub_encoding_router.route_calls == [["test"]]
    call_kwargs = qdrant.upsert_chunks.call_args.kwargs
    assert call_kwargs.get("precomputed_encodings") == [cpu_vec]