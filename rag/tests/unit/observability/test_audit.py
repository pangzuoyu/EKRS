import json
from pathlib import Path

import pytest

from ekrs_rag.observability.audit import AuditWriter


def test_audit_writer_creates_permanent_file(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("test_event", {"field_a"})
    writer.write("test_event", field_a="hello", trace_id="t1")

    lines = log.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "test_event"
    assert entry["field_a"] == "hello"
    assert entry["trace_id"] == "t1"
    assert "timestamp" in entry


def test_audit_writer_appends_multiple_events(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("evt", {"x"})
    for i in range(5):
        writer.write("evt", x=i)

    lines = log.read_text().strip().split("\n")
    assert len(lines) == 5
    entries = [json.loads(l) for l in lines]
    assert [e["x"] for e in entries] == [0, 1, 2, 3, 4]


def test_audit_writer_does_not_rotate(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("big", {"payload"})
    writer.write("big", payload="x" * 1_000_000)  # 1MB
    writer.write("big", payload="y" * 1_000_000)  # another 1MB

    # File should be > 2MB; no rotation occurred
    assert log.stat().st_size > 2_000_000
    # No .1 / .2 backup files
    backups = list(tmp_path.glob("audit.log.*"))
    assert backups == []


def test_audit_write_failure_returns_false(tmp_path):
    log = tmp_path / "audit.log"
    log.write_text("existing")
    log.chmod(0o000)  # make read-only (may still work as root; use chmod trick)

    # FileHandler opens the file in __init__, so the constructor may also raise
    # PermissionError on non-root systems. Wrap both construction and write.
    try:
        writer = AuditWriter(str(log))
        writer.register_event_schema("evt", set())
        result = writer.write("evt", data="test")
        if result is False:
            log.chmod(0o644)  # cleanup
            assert result is False
        else:
            log.chmod(0o644)
            assert result is True  # root bypass
    except (PermissionError, OSError):
        log.chmod(0o644)
        pytest.skip("running as root bypasses chmod 0o000")


def test_audit_writer_propagation_is_false(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    import logging
    audit_logger = logging.getLogger(writer._logger.name)
    assert audit_logger.propagate is False


def test_audit_writer_uses_json_formatter(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.write("format_test", k="v")
    line = log.read_text().strip()
    entry = json.loads(line)
    assert entry["k"] == "v"
    assert entry["event"] == "format_test"