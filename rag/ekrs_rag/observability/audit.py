"""RAG-specific AuditWriter: shared/audit.py base + FileHandler (永久).

audit.log never rotates. Write failures are caught (returns False),
never propagate to callers.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path

from ekrs_shared.audit import AuditLogger


# Module-level writer, set by main.py at startup
_writer: AuditLogger | None = None
# Module-level AuditIndex, set by main.py at startup (Issue 5: runtime writes
# must be indexable for replay without rescan)
_index = None


class AuditWriter(AuditLogger):
    """AuditLogger instance with permanent FileHandler."""

    def __init__(self, audit_log_path: str):
        super().__init__(name="ekrs.audit")
        path = Path(audit_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # FileHandler, NOT RotatingFileHandler — permanent
        handler = logging.FileHandler(str(path), encoding="utf-8")
        # Pass-through formatter (base class already JSON-encodes message)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    def write(self, event_type: str, **kwargs) -> bool:
        """Log an event. Returns False if write fails (never raises)."""
        try:
            # Capture file offset BEFORE write so AuditIndex can locate the line
            offset = self._current_offset()
            self.log_event(event_type, **kwargs)
            # Register new line in module-level AuditIndex (Issue 5)
            idx = get_index()
            if idx is not None:
                trace_id = kwargs.get("trace_id") or self._logger.findCaller  # noqa: F841
                # Simpler: pull trace_id from kwargs (audit contract always passes it)
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
        for h in self._logger.handlers:
            if isinstance(h, logging.FileHandler) and not h.stream.closed:
                try:
                    return h.stream.tell()
                except (OSError, AttributeError):
                    pass
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
