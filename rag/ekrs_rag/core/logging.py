"""Structured JSON logging setup for EKRS RAG service."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pythonjsonlogger import json as json_logger


class CustomJsonFormatter(json_logger.JsonFormatter):
    """Adds standard EKRS fields to every log entry."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("level", record.levelname)
        log_record.setdefault("module", record.module)
        log_record.setdefault("message", record.getMessage())


def _tag_handler(h):
    h._ekrs_tag = True


def _is_our_handler(h):
    return getattr(h, "_ekrs_tag", False)


def setup_logging(
    debug: bool = False, debug_log_path: str = "logs/debug.log"
) -> None:
    """Configure root logger.

    Always: StreamHandler to stdout with JSON formatter.
    If debug=True: also RotatingFileHandler at debug_log_path (100MB x 5 backups).

    Idempotent: removes only handlers previously installed by this function,
    leaving framework handlers (pytest caplog, etc.) intact.
    """
    level = logging.DEBUG if debug else logging.INFO

    formatter = CustomJsonFormatter(
        fmt="%(timestamp)s %(level)s %(module)s %(message)s",
        rename_fields={"timestamp": "timestamp", "levelname": "level"},
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Remove only OUR previous handlers — preserves framework handlers
    root.handlers = [h for h in root.handlers if not _is_our_handler(h)]

    # Always stdout
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    _tag_handler(stdout_handler)
    root.addHandler(stdout_handler)

    # Optional debug file (RotatingFileHandler, 100MB x 5)
    if debug:
        log_path = Path(debug_log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=100 * 1024 * 1024,  # 100MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        _tag_handler(file_handler)
        root.addHandler(file_handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("qdrant_client").setLevel(logging.WARNING)
