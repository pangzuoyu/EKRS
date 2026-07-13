"""Tests for AuditWriter honoring the _skip_audit ContextVar."""
from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.trace import set_skip_audit, reset_skip_audit


def test_write_returns_false_when_skip_set(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("e", {"x"})

    token = set_skip_audit(True)
    try:
        result = w.write("e", x=1)
    finally:
        reset_skip_audit(token)

    assert result is False
    # Nothing written to file
    assert log.read_text() == ""


def test_write_resumes_after_skip_reset(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("e", {"x"})

    token = set_skip_audit(True)
    try:
        w.write("e", x="dropped")
    finally:
        reset_skip_audit(token)

    # Skip flag cleared — next write goes through
    assert w.write("e", x="kept") is True
    lines = log.read_text().strip().split("\n")
    assert len(lines) == 1
    import json
    assert json.loads(lines[0])["x"] == "kept"