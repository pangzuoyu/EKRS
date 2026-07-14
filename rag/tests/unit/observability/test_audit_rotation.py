"""Tests for AuditWriter's rotating handler integration."""
import gzip
from pathlib import Path

from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.audit_handler import RebuildingRotatingFileHandler


def test_audit_writer_uses_rotation_with_correct_params(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    assert w._file_handler.maxBytes == 100 * 1024 * 1024
    assert w._file_handler.backupCount == 5
    # gzip rotator attached
    assert w._file_handler.namer is not None
    assert w._file_handler.rotator is not None


def test_audit_writer_passes_on_rollover(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log), on_rollover=lambda: None)
    assert w._file_handler._on_rollover is not None


def test_audit_writer_rotation_creates_gz_backup(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("big", {"payload"})
    # Shrink maxBytes to force rotation
    w._file_handler.maxBytes = 200
    for i in range(40):
        w.write("big", payload="x" * 100, i=i)
    w._file_handler.close()
    gz_files = list(tmp_path.glob("audit.log.*.gz"))
    assert len(gz_files) >= 1


def test_audit_writer_gz_backup_is_valid_gzip(tmp_path):
    """After rollover, the .1.gz backup is readable via gzip.open."""
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("e", {"i"})
    w._file_handler.maxBytes = 200
    for i in range(40):
        w.write("e", i=i, payload="x" * 100)
    w._file_handler.close()
    gz = next(tmp_path.glob("audit.log.*.gz"), None)
    assert gz is not None
    content = gzip.open(str(gz), "rt").read()
    assert len(content) > 0
    assert "\n" in content  # multi-line events


def test_new_instance_drops_prior_rebuilding_handlers(tmp_path):
    """A second AuditWriter replaces prior RebuildingRotatingFileHandlers
    on the shared `ekrs.audit` logger to prevent file-handler accumulation
    (singleton logger bug)."""
    log1 = tmp_path / "audit1.log"
    log2 = tmp_path / "audit2.log"
    w1 = AuditWriter(str(log1))
    w1.register_event_schema("e", {"i"})
    assert any(
        isinstance(h, RebuildingRotatingFileHandler) for h in w1._logger.handlers
    )

    w2 = AuditWriter(str(log2))
    # Only ONE RebuildingRotatingFileHandler should remain — w1's was closed
    # and removed by w2's __init__.
    rotating = [
        h for h in w2._logger.handlers
        if isinstance(h, RebuildingRotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0] is w2._file_handler
    assert rotating[0].baseFilename == str(log2)