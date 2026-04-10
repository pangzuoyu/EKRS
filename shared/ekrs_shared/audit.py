"""Audit log base class for EKRS.

Provides structured JSON audit logging with trace_id propagation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


class AuditLogger:
    """Base audit logger that writes structured JSON events.

    Usage:
        audit = AuditLogger("ekrs.audit")
        audit.log_event("constraint_solved", trace_id="abc", query="温度", duration_ms=150)
    """

    def __init__(self, name: str = "ekrs.audit", level: int = logging.INFO):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def log_event(self, event_type: str, **kwargs: Any) -> None:
        """Log a structured audit event."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **kwargs,
        }
        self._logger.info(json.dumps(entry, ensure_ascii=False, default=str))
