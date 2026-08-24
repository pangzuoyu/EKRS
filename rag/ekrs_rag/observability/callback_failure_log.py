"""Callback failure reconciliation log (Phase 13a T8 / P1-5).

When the parser-callback POST fails (timeout / network error / 5xx),
the failure is appended as a single JSON line to a dedicated rotating
log file. Operators (or an offline reconciler) can replay the log to
re-fire callbacks without losing the original notification.

Path is configurable via ``Settings.CALLBACK_FAILURES_LOG_PATH``
(default ``logs/callback_failures.log``); uses the existing
``RebuildingRotatingFileHandler`` (100 MB × 5 gzip backups) for byte-
level parity with audit.log. ``record_callback_failure`` is best-effort
— write failures are caught and never propagate into ingestion.
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit_handler import RebuildingRotatingFileHandler

_loggers: dict[str, CallbackFailureLog] = {}


class CallbackFailureLog:
    """Append-only structured log for callback failures.

    One file per process. Each line is a JSON object with at least::

        {"ts": "<ISO-8601 UTC>", "doc_hash": "...", "reason": "..."}

    Rotation: 100 MB × 5 gzip backups (same as audit.log). The handler
    is the rebuilding variant so future rollover hooks (e.g. crash-safe
    sync) can attach without touching call sites.
    """

    _LOGGER_NAME_PREFIX = "ekrs.callback_failures"

    def __init__(self, log_path: str, max_bytes: int = 100 * 1024 * 1024, backup_count: int = 5):
        # Singleton-per-path so successive write sites share the same
        # handler (and thus the same file handle / rotation state).
        if log_path in _loggers:
            self.__dict__ = _loggers[log_path].__dict__
            return

        self._log_path = log_path
        self._logger = logging.getLogger(f"{self._LOGGER_NAME_PREFIX}.{log_path}")
        # Bump propagate=False so a misconfigured root handler doesn't
        # duplicate the line into the regular log pipeline.
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)

        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            handler = RebuildingRotatingFileHandler(
                log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
        except Exception:
            # Best-effort init: if mkdir / open fails (permission, RO
            # filesystem) the logger has no handler and writes become
            # silent — but record_callback_failure still won't raise.
            # This mirrors AuditWriter.write's never-propagate guarantee.
            logging.getLogger("ekrs.callback_failures.failures").error(
                "callback failure log init failed for %s: %s",
                log_path,
                traceback.format_exc(),
            )

        _loggers[log_path] = self

    @property
    def path(self) -> str:
        return self._log_path


def record_callback_failure(
    log: CallbackFailureLog,
    doc_hash: str,
    reason: str,
    **extra: Any,
) -> None:
    """Append a single structured line to the callback failure log.

    Best-effort: any exception inside the handler (disk full, permission
    denied, broken pipe) is caught and logged to stderr; the caller
    never sees the exception propagate. This is the same belt-and-
    suspenders pattern used by ``AuditWriter.write``.
    """
    try:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "doc_hash": doc_hash,
            "reason": reason[:1024],  # cap to keep log lines bounded
        }
        if extra:
            payload.update(extra)
        log._logger.info(json.dumps(payload, ensure_ascii=False))  # type: ignore[attr-defined]
    except Exception:
        # Last-resort stderr trace so the failure isn't completely silent.
        logging.getLogger("ekrs.callback_failures.failures").error(
            "callback failure log write failed: %s",
            traceback.format_exc(),
        )