"""Unit tests for AuditIndex — trace_id → file_offset in-memory index."""
import json

from ekrs_rag.observability.audit_index import AuditIndex


def _write_audit_line(path, event_type, trace_id, **extra):
    entry = {"timestamp": "2026-07-12T00:00:00Z", "event": event_type,
             "trace_id": trace_id, **extra}
    line = json.dumps(entry)
    with open(path, "a") as f:
        offset = f.tell()
        f.write(line + "\n")
    return offset


def test_index_builds_from_clean_audit_log(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "constraint_solve_started", "t1", query="q")
    _write_audit_line(log, "constraint_solved", "t1", branches_count=2)
    _write_audit_line(log, "constraint_solve_started", "t2", query="q2")
    _write_audit_line(log, "constraint_solved", "t2", branches_count=1)

    idx = AuditIndex(str(log))
    idx.build()

    result = idx.seek("t1")
    assert result is not None
    assert len(result) == 2
    assert result[0].event == "constraint_solve_started"
    assert result[1].event == "constraint_solved"


def test_index_skips_non_replay_events(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "endpoint_started", "t1", endpoint="/v1/x")
    _write_audit_line(log, "ingestion_completed", "t1", doc_id="d")

    idx = AuditIndex(str(log))
    idx.build()

    # Only constraint events are indexed, so seek returns None
    assert idx.seek("t1") is None


def test_index_resilient_to_corrupted_lines(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "constraint_solve_started", "t1", query="q")
    # Inject corrupted line
    with open(log, "a") as f:
        f.write("THIS IS NOT JSON\n")
    _write_audit_line(log, "constraint_solved", "t1", branches_count=1)

    idx = AuditIndex(str(log))
    idx.build()  # should not raise

    result = idx.seek("t1")
    assert result is not None
    assert len(result) == 2


def test_index_grows_on_runtime_writes(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "constraint_solve_started", "t1", query="q")
    idx = AuditIndex(str(log))
    idx.build()

    # Simulate runtime write
    offset = _write_audit_line(log, "constraint_solved", "t1", branches_count=3)
    idx.append("constraint_solved", "t1", offset)

    result = idx.seek("t1")
    assert len(result) == 2


def test_index_returns_none_for_missing_trace_id(tmp_path):
    log = tmp_path / "audit.log"
    idx = AuditIndex(str(log))
    idx.build()
    assert idx.seek("nonexistent") is None


def test_runtime_writes_via_auditwriter_become_indexable(tmp_path):
    """Test Gap 2: AuditWriter.write must register new lines with attached index.

    Without this, freshly written traces after startup won't be replayable
    until process restart (Issue 5).
    """
    from ekrs_rag.observability.audit import AuditWriter, attach_index, reset_index_for_test
    from ekrs_rag.observability import audit as audit_mod

    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})

    idx = AuditIndex(str(log))
    idx.build()
    # Clear any prior module-level index (defensive — parallel tests may leak)
    reset_index_for_test()
    attach_index(idx)

    try:
        # Runtime write — should be picked up by attached index
        trace_id = "rt-trace-1"
        writer.write("constraint_solve_started", trace_id=trace_id, query="q")
        writer.write("constraint_solved", trace_id=trace_id, branches_count=1)

        # Immediately seekable without rescan
        lines = idx.seek(trace_id)
        assert lines is not None
        assert len(lines) == 2
    finally:
        # Clean up module-level state so other tests aren't affected
        reset_index_for_test()
        # Touch module name to silence unused-import warning
        _ = audit_mod


def test_seek_skips_line_truncated_after_index_build(tmp_path, caplog):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "constraint_solve_started", "truncated", query="q")
    idx = AuditIndex(str(log))
    idx.build()
    log.write_text("{\"event\":", encoding="utf-8")

    assert idx.seek("truncated") == []
    assert "seek: corrupted line at offset 0" in caplog.text
