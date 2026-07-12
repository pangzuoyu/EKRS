"""Tests for debug.log RotatingFileHandler setup (Task 9)."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from ekrs_rag.core.logging import setup_logging


def test_debug_log_creates_rotating_handler(tmp_path):
    log = tmp_path / "debug.log"
    setup_logging(debug=True, debug_log_path=str(log))

    root = logging.getLogger()
    rot_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rot_handlers) == 1
    assert rot_handlers[0].maxBytes == 100 * 1024 * 1024
    assert rot_handlers[0].backupCount == 5


def test_no_debug_log_when_debug_false(tmp_path):
    log = tmp_path / "debug.log"
    setup_logging(debug=False, debug_log_path=str(log))

    root = logging.getLogger()
    rot_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert rot_handlers == []


def test_debug_log_directory_created(tmp_path):
    log = tmp_path / "subdir" / "debug.log"
    setup_logging(debug=True, debug_log_path=str(log))
    # Should not raise; parent dir created
    assert log.parent.exists()


def test_debug_log_handler_is_ekrs_tagged(tmp_path):
    """Our rotating handler carries the _ekrs_tag so re-setup can clean up."""
    log = tmp_path / "debug.log"
    setup_logging(debug=True, debug_log_path=str(log))

    root = logging.getLogger()
    rot_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert getattr(rot_handlers[0], "_ekrs_tag", False) is True


def test_setup_logging_is_idempotent(tmp_path):
    """Calling setup_logging twice must not pile up handlers."""
    log = tmp_path / "debug.log"
    setup_logging(debug=True, debug_log_path=str(log))
    setup_logging(debug=True, debug_log_path=str(log))

    root = logging.getLogger()
    rot_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rot_handlers) == 1


def test_setup_logging_does_not_clear_non_ekrs_handlers(tmp_path):
    """Re-setup must preserve foreign handlers (e.g. pytest caplog, framework)."""
    log = tmp_path / "debug.log"

    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)

    try:
        setup_logging(debug=True, debug_log_path=str(log))
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)
