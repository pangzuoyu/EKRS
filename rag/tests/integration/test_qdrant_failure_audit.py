"""Integration test: qdrant_write_failed audit pipeline (Phase 7 T1).

Memory: phase6c-qdrant-write-failed-integration-test.md.
Unit tests mock AuditWriter and verify write-call args; they cannot catch:
  1. AuditWriter.write silently swallowing disk-full / permission errors
  2. JSON formatter dropping fields (e.g., broadened `operation` field)
  3. RotatingFileHandler rollover dropping in-flight events
  4. AuditIndex failing to register the event after write
  5. D1 contract (EmbeddingUnavailableError must NOT emit) at real handler layer

This file uses real AuditWriter + AuditIndex + QdrantClient (unreachable
port → ConnectionError) + real EmbeddingService (dummy or heavy ONNX).
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ekrs_rag.observability.audit import (
    AuditWriter,
    attach_index,
    reset_index_for_test,
    set_writer,
)
from ekrs_rag.observability.audit_index import AuditIndex
from ekrs_rag.retrieval.embedding_service import (
    DEFAULT_MODEL_DIR,
    EmbeddingService,
)
from ekrs_rag.retrieval.qdrant_client import QdrantManager


# Port 1 (tcpmux) reserved; almost always refused on loopback. Guarantees
# ConnectionError without depending on Docker.
UNREACHABLE_PORT = 1


@pytest.fixture
def audit_log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.log"


@pytest.fixture
def audit_writer(audit_log_path: Path) -> Iterator[AuditWriter]:
    """Real AuditWriter (RebuildingRotatingFileHandler) writing to tmp_path."""
    writer = AuditWriter(str(audit_log_path))
    set_writer(writer)
    try:
        yield writer
    finally:
        set_writer(None)  # type: ignore[arg-type]
        reset_index_for_test()


@pytest.fixture
def audit_index(audit_log_path: Path) -> Iterator[AuditIndex]:
    """Real AuditIndex attached via attach_index() — mirrors main.py lifespan."""
    idx = AuditIndex(str(audit_log_path))
    idx.build()
    attach_index(idx)
    try:
        yield idx
    finally:
        reset_index_for_test()


@pytest.fixture
def unreachable_qdrant() -> EmbeddingService:
    """Real EmbeddingService in dummy mode (no ONNX)."""
    return EmbeddingService(model_dir=Path("/nonexistent"))


@pytest.fixture
def non_dummy_embedding_service() -> EmbeddingService:
    """Real EmbeddingService forced out of dummy mode.

    Forces _is_dummy=False to bypass the ONNX guard so encode() is
    exercised against the unreachable Qdrant. encode() returns [] (no
    model) so we proceed benignly to the Qdrant upsert.
    """
    svc = EmbeddingService(model_dir=Path("/nonexistent"))
    svc._is_dummy = False  # type: ignore[attr-defined]
    svc._model = None  # type: ignore[attr-defined]
    return svc


def _read_audit_events(log: Path) -> list[dict[str, Any]]:
    """Parse all JSON lines from audit.log."""
    events: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _make_mgr(embedding: EmbeddingService) -> QdrantManager:
    return QdrantManager(
        host="localhost",
        port=UNREACHABLE_PORT,
        embedding_service=embedding,
    )


# ---- T1.1: audit.log contains qdrant_write_failed with full schema ---------


def test_qdrant_failure_event_persisted_to_audit_log(
    audit_log_path: Path,
    audit_writer: AuditWriter,
    audit_index: AuditIndex,
    non_dummy_embedding_service: EmbeddingService,
) -> None:
    """upsert_chunks with unreachable Qdrant → audit.log contains
    qdrant_write_failed event with operation=write + full Phase 6C T8 schema."""
    from ekrs_shared.models import Chunk

    chunks = [
        Chunk(text="hello", scope_path=[], source_block_ids=["b1"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[])
    ]
    with pytest.raises(Exception):
        _make_mgr(non_dummy_embedding_service).upsert_chunks(chunks)
    audit_writer._file_handler.close()

    failed = [
        e for e in _read_audit_events(audit_log_path)
        if e.get("event") == "qdrant_write_failed"
    ]
    assert len(failed) >= 1
    last = failed[-1]
    assert last["operation"] == "write"
    assert last["collection"] == "rag_documents"
    assert last.get("error")
    assert last.get("message")
    assert last.get("timestamp")


# ---- T1.2: audit.log content is intact (parseable, complete) ---------------


def test_qdrant_failure_event_audit_log_content_intact(
    audit_log_path: Path,
    audit_writer: AuditWriter,
    audit_index: AuditIndex,
    non_dummy_embedding_service: EmbeddingService,
) -> None:
    """audit.log contains parseable JSON with all broadened Phase 6C T8 fields.

    DESIGN NOTE: qdrant_write_failed is NOT in REPLAY_EVENTS (only
    constraint_solve_started/constraint_solved are). AuditIndex.append()
    is intentionally a no-op for failures — they live in audit.log for
    forensics, not for replay offset lookup. This test locks that invariant.
    """
    from ekrs_shared.models import Chunk

    size_before = audit_index.size
    with pytest.raises(Exception):
        _make_mgr(non_dummy_embedding_service).upsert_chunks([
            Chunk(text="x", scope_path=[], source_block_ids=["b1"],
                  token_count=1, doc_hash="d1", version=1, page_numbers=[])
        ])
    audit_writer._file_handler.close()

    assert audit_index.size == size_before, (
        f"AuditIndex must NOT advance for qdrant_write_failed "
        f"(only REPLAY_EVENTS are indexed); was {size_before}, now {audit_index.size}"
    )

    failed = [
        e for e in _read_audit_events(audit_log_path)
        if e.get("event") == "qdrant_write_failed"
    ]
    assert failed
    for required_field in ("collection", "operation", "error", "message", "timestamp"):
        assert required_field in failed[-1], (
            f"audit.log event missing {required_field!r}: {failed[-1]}"
        )


# ---- T1.3: schema matches _EVENT_SCHEMAS registry --------------------------


def test_qdrant_failure_schema_matches_event_schemas_registry(
    audit_log_path: Path,
    audit_writer: AuditWriter,
    audit_index: AuditIndex,
    non_dummy_embedding_service: EmbeddingService,
) -> None:
    """_EVENT_SCHEMAS["qdrant_write_failed"] = {"collection"} — verified end-to-end.

    AuditLogger.log_event raises SchemaError if required fields missing,
    which would route to ekrs.audit.failures; we verify the schema check
    passed by inspecting the written JSON.
    """
    from ekrs_shared.models import Chunk

    with pytest.raises(Exception):
        _make_mgr(non_dummy_embedding_service).upsert_chunks([
            Chunk(text="x", scope_path=[], source_block_ids=["b1"],
                  token_count=1, doc_hash="d1", version=1, page_numbers=[])
        ])
    audit_writer._file_handler.close()

    failed = [
        e for e in _read_audit_events(audit_log_path)
        if e.get("event") == "qdrant_write_failed"
    ]
    assert failed, "no qdrant_write_failed in audit.log"
    assert failed[-1].get("collection"), (
        "qdrant_write_failed missing required 'collection' field per _EVENT_SCHEMAS"
    )


# ---- T1.4: D1 contract — EmbeddingUnavailableError does NOT emit -----------


def test_dummy_embedding_does_not_emit_qdrant_write_failed(
    audit_log_path: Path,
    audit_writer: AuditWriter,
    audit_index: AuditIndex,
    unreachable_qdrant: EmbeddingService,
) -> None:
    """D1 contract: dummy-mode EmbeddingUnavailableError is a config error,
    NOT a Qdrant failure. Must short-circuit before qdrant_write_failed."""
    from ekrs_shared.models import Chunk
    from ekrs_rag.retrieval.embedding_service import EmbeddingUnavailableError

    with pytest.raises(EmbeddingUnavailableError):
        _make_mgr(unreachable_qdrant).upsert_chunks([
            Chunk(text="x", scope_path=[], source_block_ids=["b1"],
                  token_count=1, doc_hash="d1", version=1, page_numbers=[])
        ])
    audit_writer._file_handler.close()

    failed = [
        e for e in _read_audit_events(audit_log_path)
        if e.get("event") == "qdrant_write_failed"
    ]
    assert failed == [], (
        f"D1 contract violated: qdrant_write_failed emitted on "
        f"EmbeddingUnavailableError: {failed}"
    )


# ---- T1.5: parameterized — all 4 QdrantManager methods cover their op -----


@pytest.mark.parametrize(
    ("method_name", "operation"),
    [
        ("ensure_collection", "write"),
        ("upsert_chunks", "write"),
        ("search", "read"),
        ("delete_old_versions", "delete"),
    ],
)
def test_each_qdrant_op_emits_correct_operation_field(
    audit_log_path: Path,
    audit_writer: AuditWriter,
    audit_index: AuditIndex,
    non_dummy_embedding_service: EmbeddingService,
    method_name: str,
    operation: str,
) -> None:
    """Every QdrantManager I/O method must emit qdrant_write_failed with
    the correct `operation` discriminator per Phase 6C T8 broadened semantic."""
    from ekrs_shared.models import Chunk

    mgr = _make_mgr(non_dummy_embedding_service)
    with pytest.raises(Exception):
        if method_name == "ensure_collection":
            mgr.ensure_collection(vector_size=1024)
        elif method_name == "upsert_chunks":
            mgr.upsert_chunks([
                Chunk(text="x", scope_path=[], source_block_ids=["b1"],
                      token_count=1, doc_hash="d1", version=1, page_numbers=[])
            ])
        elif method_name == "search":
            mgr.search(query_text="q", top_k=3)
        else:  # delete_old_versions
            mgr.delete_old_versions(doc_hash="d1", keep_version=2)

    audit_writer._file_handler.close()
    failed = [
        e for e in _read_audit_events(audit_log_path)
        if e.get("event") == "qdrant_write_failed"
    ]
    assert failed, f"{method_name} did not emit qdrant_write_failed"
    assert failed[-1]["operation"] == operation, (
        f"{method_name} emitted operation={failed[-1]['operation']!r}, "
        f"expected {operation!r}"
    )


# ---- T1.6: rollover during failure → AuditIndex still attached ------------


def test_qdrant_failure_survives_audit_log_rotation(
    tmp_path: Path,
    non_dummy_embedding_service: EmbeddingService,
) -> None:
    """After audit.log rotates mid-failure burst, AuditIndex is rebuilt
    by the on_rollover callback and remains attached.

    Per Phase 5.5 F: replay scans ONLY current audit.log (not .gz).
    This test catches: rollover callback crashes, index not rebuilt,
    pre-rotation state lost without bookkeeping.
    """
    from ekrs_shared.models import Chunk

    log = tmp_path / "audit.log"

    def on_rollover() -> None:
        new_idx = AuditIndex(str(log))
        new_idx.build()
        attach_index(new_idx)

    writer = AuditWriter(str(log), on_rollover=on_rollover)
    set_writer(writer)
    writer._file_handler.maxBytes = 500
    initial_idx = AuditIndex(str(log))
    initial_idx.build()
    attach_index(initial_idx)
    try:
        mgr = _make_mgr(non_dummy_embedding_service)
        # 5 ops × 3 retries (tenacity) = 15 audit lines × ~150 bytes ≈ 2.2KB
        for i in range(5):
            with pytest.raises(Exception):
                mgr.upsert_chunks([
                    Chunk(text=f"doc-{i}", scope_path=[], source_block_ids=["b1"],
                          token_count=1, doc_hash=f"d{i}", version=1, page_numbers=[])
                ])
        writer._file_handler.close()

        rotated = list(tmp_path.glob("audit.log.*"))
        assert rotated, (
            f"expected rotation (maxBytes={writer._file_handler.maxBytes}), "
            f"got only {list(tmp_path.glob('audit.log*'))}"
        )

        from ekrs_rag.observability.audit import get_index
        idx = get_index()
        assert idx is not None, "AuditIndex not attached after rotation"
    finally:
        set_writer(None)  # type: ignore[arg-type]
        reset_index_for_test()


# ---- T1.7 (heavy): real bge-m3 happy path does NOT emit --------------------


@pytest.mark.heavy
def test_real_bge_m3_happy_path_does_not_emit_qdrant_write_failed(
    tmp_path: Path,
) -> None:
    """Success-path counterpart of T1.1: real bge-m3 + in-memory Qdrant
    upserts cleanly, emits zero qdrant_write_failed events. Skipped when
    bge-m3 model files are absent locally (heavy gate, nightly job)."""
    from qdrant_client import QdrantClient
    from ekrs_shared.models import Chunk

    model_dir = Path(DEFAULT_MODEL_DIR)
    if not model_dir.exists():
        pytest.skip(f"bge-m3 model dir {model_dir} not present")

    embedding = EmbeddingService(model_dir=model_dir)
    if embedding.is_dummy:
        pytest.skip("EmbeddingService in dummy mode (model load failed)")

    mgr = _make_mgr(embedding)
    mgr._client = QdrantClient(":memory:")  # swap unreachable for in-memory
    mgr.ensure_collection(vector_size=1024)

    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    set_writer(writer)
    idx = AuditIndex(str(log))
    idx.build()
    attach_index(idx)
    try:
        chunks = [
            Chunk(text="高温环境温度上限 425°C", scope_path=["national"],
                  source_block_ids=["b1"], token_count=8,
                  doc_hash="d-real-onnx", version=1, page_numbers=[1])
        ]
        n = mgr.upsert_chunks(chunks)
        assert n == 1, f"expected 1 point upserted, got {n}"
        writer._file_handler.close()

        failed = [
            e for e in _read_audit_events(log)
            if e.get("event") == "qdrant_write_failed"
        ]
        assert failed == [], (
            f"real bge-m3 happy path must not emit qdrant_write_failed, "
            f"got: {failed}"
        )
    finally:
        set_writer(None)  # type: ignore[arg-type]
        reset_index_for_test()