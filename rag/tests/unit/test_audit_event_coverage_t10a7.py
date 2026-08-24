"""Phase 10 T10a-7 — Audit event coverage for fts_synced + fts_searched.

Verifies:
- Schema registration validation for both new event types
- Retriever emits ``fts_searched`` with FusionStats fields when ``fts``
  is configured (parent §T10a-7)
- ``audit_writer=None`` is a no-op (Phase 9 byte-level baseline)
- ``fts=None`` does NOT emit (degraded path = Phase 9 baseline)
- Audit exception is best-effort: emission failure does not block retriever
- main.py ``_EVENT_SCHEMAS`` contains both schemas (event count 20→22)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.retrieval.rank_fusion import FusionStats
from ekrs_rag.retrieval.retriever import EKRSRetriever
from ekrs_shared.audit import AuditLogger


# ============================================================================
# Section 1: Schema registration (AuditLogger validation)
# ============================================================================


def test_fts_synced_schema_registration_accepts_required_fields(tmp_path: Path) -> None:
    """``fts_synced`` schema registered in main.py accepts doc_hash/version/chunks_written."""
    audit = AuditLogger("ekrs.audit.test_synced")
    audit.register_event_schema(
        "fts_synced", {"doc_hash", "version", "chunks_written"}
    )
    # Should not raise — all required fields present
    audit.validate_event(
        "fts_synced",
        doc_hash="abc123",
        version=1,
        chunks_written=42,
    )


def test_fts_synced_schema_registration_rejects_missing_fields() -> None:
    """``fts_synced`` schema rejects when any required field missing."""
    audit = AuditLogger("ekrs.audit.test_synced_missing")
    audit.register_event_schema(
        "fts_synced", {"doc_hash", "version", "chunks_written"}
    )
    with pytest.raises(ValueError, match="missing required fields"):
        audit.validate_event("fts_synced", doc_hash="abc", version=1)
    with pytest.raises(ValueError, match="missing required fields"):
        audit.validate_event("fts_synced", doc_hash="abc", chunks_written=1)
    with pytest.raises(ValueError, match="missing required fields"):
        audit.validate_event("fts_synced", version=1, chunks_written=1)


def test_fts_searched_schema_registration_accepts_required_fields() -> None:
    """``fts_searched`` schema registered accepts vector_hits/fts_hits/both_hits."""
    audit = AuditLogger("ekrs.audit.test_searched")
    audit.register_event_schema(
        "fts_searched", {"vector_hits", "fts_hits", "both_hits"}
    )
    # Should not raise — all required fields present
    audit.validate_event(
        "fts_searched",
        vector_hits=5,
        fts_hits=3,
        both_hits=2,
    )


def test_fts_searched_schema_registration_rejects_missing_fields() -> None:
    """``fts_searched`` schema rejects when any required field missing."""
    audit = AuditLogger("ekrs.audit.test_searched_missing")
    audit.register_event_schema(
        "fts_searched", {"vector_hits", "fts_hits", "both_hits"}
    )
    with pytest.raises(ValueError, match="missing required fields"):
        audit.validate_event("fts_searched", fts_hits=3, both_hits=2)


# ============================================================================
# Section 2: main.py _EVENT_SCHEMAS — both events registered
# ============================================================================


def test_main_event_schemas_contains_fts_synced_and_searched() -> None:
    """``rag/ekrs_rag/main.py`` ``_EVENT_SCHEMAS`` includes both new event types.

    Lock the T10a-2 (drift) + T10a-7 (sync+searched) add to 22 entries.
    Phase 6B/6C/7 added many; this test catches accidental deletion or
    schema renames during future refactors.
    """
    from ekrs_rag.main import _EVENT_SCHEMAS

    assert "fts_synced" in _EVENT_SCHEMAS, (
        "main.py _EVENT_SCHEMAS must register fts_synced (T10a-7)"
    )
    assert "fts_searched" in _EVENT_SCHEMAS, (
        "main.py _EVENT_SCHEMAS must register fts_searched (T10a-7)"
    )
    assert _EVENT_SCHEMAS["fts_synced"] == {
        "doc_hash", "version", "chunks_written",
    }
    assert _EVENT_SCHEMAS["fts_searched"] == {
        "vector_hits", "fts_hits", "both_hits",
    }


def test_main_event_schemas_count_24() -> None:
    """``_EVENT_SCHEMAS`` has 25 entries (T10a-7 closed: +2 from 20 baseline;
    Phase 13a T6 closed: +2 admission_rejected + task_timeout_killed = 24;
    Phase 13b T3 closed: +1 channel_switched = 25)."""
    from ekrs_rag.main import _EVENT_SCHEMAS

    assert len(_EVENT_SCHEMAS) == 25, (
        f"Expected 25 event schemas (T3 closure: channel_switched); "
        f"got {len(_EVENT_SCHEMAS)}"
    )


def test_main_event_schemas_contains_channel_switched() -> None:
    """``rag/ekrs_rag/main.py`` ``_EVENT_SCHEMAS`` includes channel_switched.

    Phase 13b T3.3: 4-step discipline step #1 — schema registered for
    required-field validation. Without this entry, the AuditLogger would
    silently accept malformed channel_switched payloads (no field check).
    """
    from ekrs_rag.main import _EVENT_SCHEMAS

    assert "channel_switched" in _EVENT_SCHEMAS, (
        "main.py _EVENT_SCHEMAS must register channel_switched (T3)"
    )
    assert _EVENT_SCHEMAS["channel_switched"] == {
        "from_channel", "to_channel", "reason",
    }


# ============================================================================
# Section 3: Retriever audit_writer DI
# ============================================================================


class _StubFTS:
    """Minimal duck-typed FTS replacement for tests."""

    def __init__(self, hits: List) -> None:
        self._hits = hits
        self.calls: List[str] = []

    def search_with_payload(self, query: str) -> List:
        self.calls.append(query)
        return self._hits


class _StubQdrant:
    """Minimal duck-typed Qdrant replacement — returns empty hits."""

    def __init__(self) -> None:
        self.calls: List = []

    async def search(self, query_text: str, top_k: int) -> List:
        self.calls.append((query_text, top_k))
        return []


def _run(coro):
    """Run async coroutine in tests (no pytest-asyncio dependency).

    ``asyncio.run`` creates a fresh loop each call (Python 3.10+) so
    tests run cleanly inside pytest's own loop-isolation machinery.
    """
    return asyncio.run(coro)


def test_retriever_audit_writer_none_does_not_emit() -> None:
    """``audit_writer=None`` (default) → no emit (Phase 9 byte-level baseline)."""
    qdrant = _StubQdrant()
    fts = _StubFTS(hits=[])
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=None)
    _run(retriever.retrieve("test query"))
    # audit_writer is None; nothing to assert beyond not raising.
    # The fts pipeline still runs (FTS is configured), but no audit emit.
    assert len(fts.calls) == 1


def test_retriever_fts_none_does_not_emit() -> None:
    """``fts=None`` (degraded path) → no ``fts_searched`` emit."""
    audit = MagicMock(spec=AuditWriter)
    qdrant = _StubQdrant()
    retriever = EKRSRetriever(qdrant=qdrant, fts=None, audit_writer=audit)
    _run(retriever.retrieve("test query"))
    # FTS not configured; fts_searched should not be emitted.
    audit.write.assert_not_called()


def test_retriever_emits_fts_searched_with_fusion_stats() -> None:
    """When fts is configured and audit_writer is set, ``fts_searched`` is emitted."""
    audit = MagicMock(spec=AuditWriter)
    qdrant = _StubQdrant()
    fts = _StubFTS(hits=[])
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)
    _run(retriever.retrieve("test query"))

    # Exactly one fts_searched emit
    fts_searched_calls = [
        c for c in audit.write.call_args_list
        if c.args and c.args[0] == "fts_searched"
    ]
    assert len(fts_searched_calls) == 1, (
        f"Expected 1 fts_searched emit, got {len(fts_searched_calls)}: "
        f"{audit.write.call_args_list}"
    )
    call = fts_searched_calls[0]
    kwargs = call.kwargs
    assert "vector_hits" in kwargs
    assert "fts_hits" in kwargs
    assert "both_hits" in kwargs
    assert isinstance(kwargs["vector_hits"], int)
    assert isinstance(kwargs["fts_hits"], int)
    assert isinstance(kwargs["both_hits"], int)


def test_retriever_fts_searched_with_zero_fts_hits_still_emits() -> None:
    """When FTS returns 0 hits, ``fts_searched`` is still emitted (operational visibility)."""
    audit = MagicMock(spec=AuditWriter)
    qdrant = _StubQdrant()
    fts = _StubFTS(hits=[])
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)
    _run(retriever.retrieve("test query"))

    fts_searched_calls = [
        c for c in audit.write.call_args_list
        if c.args and c.args[0] == "fts_searched"
    ]
    assert len(fts_searched_calls) == 1
    kwargs = fts_searched_calls[0].kwargs
    assert kwargs["fts_hits"] == 0
    # both_hits must also be 0 — FusionStats invariants hold
    assert kwargs["both_hits"] == 0


def test_retriever_emit_does_not_fail_when_audit_writer_raises() -> None:
    """Audit emit exception is isolated — retriever does not propagate."""
    audit = MagicMock(spec=AuditWriter)
    audit.write.side_effect = RuntimeError("audit disk full")
    qdrant = _StubQdrant()
    fts = _StubFTS(hits=[])
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)

    # Must NOT raise — best-effort audit per parent §204
    result = _run(retriever.retrieve("test query"))
    assert result is not None
    # The mock was called (emit attempted), but the exception was swallowed
    audit.write.assert_called()


# ============================================================================
# Section 4: FusionStats integration
# ============================================================================


def test_retriever_emits_fusion_stats_fields_when_both_retrievers_hit() -> None:
    """When both vector and FTS return hits, FusionStats fields reflect overlap."""
    audit = MagicMock(spec=AuditWriter)

    # Vector returns chunk A (vector-only), FTS returns chunks A + B (overlap + fts-only).
    # Expected: vector_hits=0, fts_hits=1, both_hits=1.
    chunk_id_a = "abc123-0001"
    chunk_id_b = "abc123-0002"
    payload_a = {
        "text": "chunk A",
        "scope_path": ["第1章"],
        "source_block_ids": ["b1"],
        "token_count": 10,
        "doc_hash": "abc123",
        "version": 1,
        "page_numbers": [1],
        "chunk_id": chunk_id_a,
    }
    payload_b = {
        "text": "chunk B",
        "scope_path": ["第2章"],
        "source_block_ids": ["b2"],
        "token_count": 8,
        "doc_hash": "abc123",
        "version": 1,
        "page_numbers": [2],
        "chunk_id": chunk_id_b,
    }

    class _VecQdrant:
        async def search(self, query_text, top_k):
            return [(payload_a, 0.9)]

    class _FtsWithHit:
        def search_with_payload(self, query):
            # FTS found A (overlap) and B (fts-only)
            return [(chunk_id_a, payload_a, 1.5), (chunk_id_b, payload_b, 1.2)]

    qdrant = _VecQdrant()
    fts = _FtsWithHit()
    retriever = EKRSRetriever(qdrant=qdrant, fts=fts, audit_writer=audit)
    _run(retriever.retrieve("test"))

    fts_searched_calls = [
        c for c in audit.write.call_args_list
        if c.args and c.args[0] == "fts_searched"
    ]
    assert len(fts_searched_calls) == 1
    kwargs = fts_searched_calls[0].kwargs
    # A is in both → both_hits=1
    # B is fts-only → fts_hits=1
    # No vector-only chunk → vector_hits=0
    assert kwargs["vector_hits"] == 0
    assert kwargs["fts_hits"] == 1
    assert kwargs["both_hits"] == 1


# ============================================================================
# Section 5: AuditWriter round-trip — actual write validates schema
# ============================================================================


def test_audit_writer_round_trip_validates_fts_synced(tmp_path: Path) -> None:
    """AuditWriter.write on fts_synced validates against registered schema.

    Uses a real AuditWriter (tmp_path) with the schema pre-registered,
    confirming the validation path used by pipeline.ingest.
    """
    log_path = tmp_path / "audit.log"
    writer = AuditWriter(str(log_path))
    writer.register_event_schema(
        "fts_synced", {"doc_hash", "version", "chunks_written"}
    )

    # Valid payload — should return True
    assert writer.write(
        "fts_synced", doc_hash="abc", version=1, chunks_written=10
    ) is True

    # Invalid payload (missing chunks_written) — should return False
    assert writer.write("fts_synced", doc_hash="abc", version=1) is False


def test_audit_writer_round_trip_validates_fts_searched(tmp_path: Path) -> None:
    """AuditWriter.write on fts_searched validates against registered schema."""
    log_path = tmp_path / "audit.log"
    writer = AuditWriter(str(log_path))
    writer.register_event_schema(
        "fts_searched", {"vector_hits", "fts_hits", "both_hits"}
    )

    assert writer.write(
        "fts_searched", vector_hits=5, fts_hits=3, both_hits=2
    ) is True

    # Missing field → False (per AuditWriter.write: returns False on failure)
    assert writer.write(
        "fts_searched", vector_hits=5, fts_hits=3
    ) is False