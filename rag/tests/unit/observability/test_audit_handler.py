"""Tests for the rotating handler with gzip rotator."""
import gzip
import logging

from ekrs_rag.observability.audit_handler import (
    RebuildingRotatingFileHandler,
    gzip_namer,
    gzip_rotator,
)


def test_gzip_namer_appends_gz():
    assert gzip_namer("audit.log.1") == "audit.log.1.gz"
    assert gzip_namer("/var/log/x.5") == "/var/log/x.5.gz"


def test_gzip_rotator_compresses_file(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello world\n" * 100)
    dst = tmp_path / "dst.txt.gz"
    gzip_rotator(str(src), str(dst))
    assert not src.exists()
    assert dst.exists()
    with gzip.open(str(dst), "rt") as f:
        assert f.read() == "hello world\n" * 100


def _make_handler(log_path, max_bytes, backup_count, on_rollover):
    h = RebuildingRotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        on_rollover=on_rollover,
    )
    h.namer = gzip_namer
    h.rotator = gzip_rotator
    return h


def _make_record(msg):
    return logging.LogRecord(
        name="x", level=logging.INFO, pathname="x", lineno=0,
        msg=msg, args=(), exc_info=None,
    )


def test_rollover_triggers_callback(tmp_path):
    log = tmp_path / "app.log"
    calls = []
    h = _make_handler(log, max_bytes=50, backup_count=2,
                      on_rollover=lambda: calls.append(1))
    h.setFormatter(logging.Formatter("%(message)s"))
    for _ in range(20):
        h.emit(_make_record("x" * 30))
    h.close()
    assert len(calls) >= 1


def test_rollover_swallows_callback_exception(tmp_path):
    """A buggy on_rollover must not crash the request thread."""
    log = tmp_path / "app.log"
    h = _make_handler(
        log,
        max_bytes=50,
        backup_count=1,
        on_rollover=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    # Should not raise — exception caught and logged.
    for _ in range(20):
        h.emit(_make_record("x" * 30))
    h.close()


def test_handler_has_gzip_defaults_from_constructor(tmp_path):
    """RebuildingRotatingFileHandler self-applies gzip namer/rotator."""
    log = tmp_path / "app.log"
    h = RebuildingRotatingFileHandler(
        str(log),
        maxBytes=50,
        backupCount=1,
        on_rollover=lambda: None,
    )
    # No external attribute assignment needed.
    assert h.namer is gzip_namer
    assert h.rotator is gzip_rotator
    h.close()


def test_handler_overridable_namer_rotator(tmp_path):
    """Caller can override namer/rotator via constructor kwargs."""
    log = tmp_path / "app.log"

    def custom_namer(name):
        return name + ".custom"

    def custom_rotator(src, dst):
        import shutil
        shutil.copyfile(src, dst)

    h = RebuildingRotatingFileHandler(
        str(log),
        maxBytes=50,
        backupCount=1,
        namer=custom_namer,
        rotator=custom_rotator,
        on_rollover=lambda: None,
    )
    assert h.namer is custom_namer
    assert h.rotator is custom_rotator
    h.close()