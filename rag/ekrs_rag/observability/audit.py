"""RAG-specific AuditWriter: shared/audit.py base + RotatingFileHandler.

audit.log rotates at 100 MB × 5 gzipped backups. Write failures are caught
(returns False), never propagate to callers.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path

from ekrs_shared.audit import AuditLogger

from ekrs_rag.observability.audit_handler import RebuildingRotatingFileHandler


# Module-level writer, set by main.py at startup
_writer: AuditLogger | None = None
# Module-level AuditIndex, set by main.py at startup (Issue 5: runtime writes
# must be indexable for replay without rescan)
_index = None


class AuditWriter(AuditLogger):
    """AuditLogger instance with rotating file handler (100 MB × 5 gzip)."""

    def __init__(self, audit_log_path: str, on_rollover=None):
        super().__init__(name="ekrs.audit")
        path = Path(audit_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # The base AuditLogger uses a shared singleton logger ("ekrs.audit").
        # Remove any prior RebuildingRotatingFileHandler on it so a new
        # AuditWriter instance (FastAPI hot reload, test fixture, etc.)
        # does not accumulate stale handlers pointing at old/closed files.
        for prior in list(self._logger.handlers):
            if isinstance(prior, RebuildingRotatingFileHandler):
                prior.close()
                self._logger.removeHandler(prior)

        handler = RebuildingRotatingFileHandler(
            str(path),
            maxBytes=100 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            on_rollover=on_rollover,
        )
        # Pass-through formatter (base class already JSON-encodes message)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        # Track our own handler so _current_offset is stable across instances.
        self._file_handler = handler

    def write(self, event_type: str, **kwargs) -> bool:
        """Log an event. Returns False if write fails (never raises)."""
        from ekrs_rag.observability.trace import get_skip_audit
        if get_skip_audit():
            return False
        try:
            # Capture file offset BEFORE write so AuditIndex can locate the line
            offset = self._current_offset()
            self.log_event(event_type, **kwargs)
            # Register new line in module-level AuditIndex (Issue 5)
            idx = get_index()
            if idx is not None:
                idx.append(event_type, kwargs.get("trace_id", ""), offset)
            return True
        except Exception:
            # Log to stderr (root logger is still alive for debug.log)
            logging.getLogger("ekrs.audit.failures").error(
                "audit write failed: %s", traceback.format_exc()
            )
            return False

    def _current_offset(self) -> int:
        """Return current byte offset of the file handler (for index registration)."""
        h = getattr(self, "_file_handler", None)
        if h is None or h.stream.closed:
            return 0
        try:
            return h.stream.tell()
        except (OSError, AttributeError):
            return 0


def set_writer(writer: AuditLogger) -> None:
    """Set module-level writer (called at startup)."""
    global _writer
    _writer = writer


def get_writer() -> AuditLogger | None:
    return _writer


def attach_index(index) -> None:
    """Attach an AuditIndex so new writes are indexed for replay (Issue 5).

    Module-level singleton; called once at startup by main.py lifespan.
    """
    global _index
    _index = index


def get_index():
    """Return attached AuditIndex, or None if not yet initialized."""
    return _index


def reset_index_for_test() -> None:
    """Clear module-level attached index (test helper only)."""
    global _index
    _index = None
