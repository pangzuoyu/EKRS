"""Structured JSON logging setup for EKRS RAG service."""

from __future__ import annotations

import logging
import sys
from pythonjsonlogger import json as json_logger


class CustomJsonFormatter(json_logger.JsonFormatter):
    """Adds standard EKRS fields to every log entry."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("level", record.levelname)
        log_record.setdefault("module", record.module)
        log_record.setdefault("message", record.getMessage())


def setup_logging(debug: bool = False) -> None:
    """Configure root logger with JSON formatter.

    Fields in every log line: timestamp, level, module, message.
    Optional fields (added by context): trace_id, doc_hash, duration_ms.
    """
    level = logging.DEBUG if debug else logging.INFO

    formatter = CustomJsonFormatter(
        fmt="%(timestamp)s %(level)s %(module)s %(message)s",
        rename_fields={"timestamp": "timestamp", "levelname": "level"},
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("qdrant_client").setLevel(logging.WARNING)
