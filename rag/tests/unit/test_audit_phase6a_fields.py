"""Tests for Phase 6A audit log field additions (lineage_snapshot, conflict_details)."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from ekrs_rag.observability.audit import AuditWriter


def test_log_event_accepts_lineage_snapshot(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("custom_event", {"trace_id"})
    # Should NOT raise: lineage_snapshot is an optional Phase 6A field
    assert w.write("custom_event", trace_id="t1", lineage_snapshot="snap") is True


def test_log_event_accepts_conflict_details(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("custom_event", {"trace_id"})
    assert w.write(
        "custom_event", trace_id="t2", conflict_details=[{"type": "soft_fallback"}]
    ) is True


def test_log_event_without_new_fields_still_works(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("custom_event", {"trace_id"})
    # Backward compat: events without the new fields still write
    assert w.write("custom_event", trace_id="t3") is True


# --- T2: schema registry must wire Phase 6A fields at app startup ---

def test_event_schemas_include_phase6a_fields_on_seven_events():
    """D6: 7 solve/lifecycle endpoints carry the 2 fields; the other 9 don't."""
    from ekrs_rag.main import _EVENT_SCHEMAS
    # Phase 6A expanded the count from 15 to 16 by registering
    # `document_metadata_failed` (orphan-audit-event memory note).
    assert len(_EVENT_SCHEMAS) == 16, "Event count is 16 after Phase 6A registration"

    with_fields = {
        "constraint_solve_started", "constraint_solved", "constraint_solve_failed",
        "endpoint_started", "endpoint_completed",
        "ingestion_received", "ingestion_completed",
    }
    for ev in with_fields:
        assert "lineage_snapshot" in _EVENT_SCHEMAS[ev], f"{ev} missing lineage_snapshot"
        assert "conflict_details" in _EVENT_SCHEMAS[ev], f"{ev} missing conflict_details"

    without_fields = set(_EVENT_SCHEMAS) - with_fields
    for ev in without_fields:
        assert "lineage_snapshot" not in _EVENT_SCHEMAS[ev], f"{ev} should not include lineage_snapshot"
        assert "conflict_details" not in _EVENT_SCHEMAS[ev], f"{ev} should not include conflict_details"


def test_event_names_are_unchanged():
    """Audit event name set is frozen at 16 (15 pre-6A + document_metadata_failed added by T2)."""
    from ekrs_rag.main import _EVENT_SCHEMAS
    expected_names = {
        "endpoint_started", "endpoint_completed",
        "constraint_solve_started", "constraint_solved", "constraint_solve_failed",
        "query_replay_executed",
        "ingestion_received", "ingestion_completed", "ingestion_failed",
        "ingestion_replay_started", "ingestion_replay_completed", "ingestion_replay_sha256_mismatch",
        "compensation_retry", "qdrant_write_failed", "lock_acquire_failed",
        "document_metadata_failed",
    }
    assert set(_EVENT_SCHEMAS) == expected_names
