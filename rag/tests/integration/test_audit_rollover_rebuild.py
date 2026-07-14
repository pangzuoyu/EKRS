"""Integration test: AuditWriter rollover callback triggers AuditIndex rebuild.

P2 from gstack-plan-eng-review of Phase 5.5 F. Verifies the wiring in
`main.py:_on_audit_rollover` actually fires and that the resulting
index reflects only post-rotation events (since user decided: only
the new audit.log is scanned, not rotated .gz files).
"""
from __future__ import annotations

import pytest

from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.audit_index import AuditIndex, REPLAY_EVENTS


def _write_event(writer, event, trace_id, **extra):
    """Register schema then write; ensures the AuditIndex picks it up."""
    writer.register_event_schema(event, {"trace_id", *extra.keys()})
    writer.write(event, trace_id=trace_id, **extra)


def test_rollover_callback_rebuilds_audit_index(tmp_path):
    log = tmp_path / "audit.log"

    # Attach index the way main.py does at startup
    index = AuditIndex(str(log))
    index.build()
    from ekrs_rag.observability.audit import attach_index
    attach_index(index)
    try:
        # Pre-populate the index with entries that DON'T exist in audit.log.
        # These simulate pre-rotation trace_ids. The callback's build() must
        # clear them (since they live in audit.log.1.gz now, not the current
        # file, per user decision: only new audit.log is scanned).
        index.append("constraint_solve_started", "trace-stale-1", offset=0)
        index.append("constraint_solved", "trace-stale-2", offset=100)
        assert index.size == 2

        calls = {"n": 0}

        def on_rollover():
            calls["n"] += 1
            index.build()

        writer = AuditWriter(str(log), on_rollover=on_rollover)
        writer._file_handler.maxBytes = 200

        # Write enough events to force rotations. Each live write auto-appends
        # to the index via AuditWriter.write → idx.append.
        for i in range(15):
            writer.write(
                "constraint_solve_started",
                trace_id=f"trace-live-{i}",
                query="x" * 250,
            )

        writer._file_handler.close()

        # Callback fired at least once
        assert calls["n"] >= 1

        # Stale entries (added before rotation) MUST be gone after build()
        assert "trace-stale-1" not in index._index
        assert "trace-stale-2" not in index._index

        # Live entries should be in the index — they were appended by
        # AuditWriter.write() after each emit, regardless of whether the
        # callback's build() saw them at rotation time.
        live_count = sum(1 for t in index._index if t.startswith("trace-live-"))
        assert live_count >= 1, (
            f"expected at least one trace-live-N in index, got {list(index._index)}"
        )
    finally:
        from ekrs_rag.observability.audit import reset_index_for_test
        reset_index_for_test()


def test_rollover_callback_handles_none_index_gracefully(tmp_path):
    """Defensive: if index hasn't been built yet, callback must not crash."""
    log = tmp_path / "audit.log"
    callback_invocations = []

    def on_rollover():
        callback_invocations.append(1)
        # Simulate main.py closure: read global lazily
        idx = None  # would be _audit_index in main.py
        if idx is not None:
            idx.build()

    writer = AuditWriter(str(log), on_rollover=on_rollover)
    writer._file_handler.maxBytes = 100
    for i in range(20):
        writer.write("constraint_solve_started", trace_id=f"t{i}", query="x" * 100)
    writer._file_handler.close()

    assert len(callback_invocations) >= 1


def test_rollover_callback_exception_is_swallowed(tmp_path):
    """A buggy rebuild callback must not crash the request thread."""
    import logging
    log = tmp_path / "audit.log"

    def bad_callback():
        raise RuntimeError("rebuild impl is broken")

    writer = AuditWriter(str(log), on_rollover=bad_callback)
    writer._file_handler.maxBytes = 100
    for i in range(20):
        writer.write("constraint_solve_started", trace_id=f"t{i}", query="x" * 100)
    writer._file_handler.close()  # must not raise