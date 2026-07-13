"""Audit log base class for EKRS.

Provides structured JSON audit logging with trace_id propagation,
schema validation, and isolated handler (propagation=False).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


class AuditLogger:
    """Base audit logger. Writes structured JSON events.

    Subclasses / instances configure FileHandler; base class owns
    schema registry and propagation control.

    Usage:
        audit = AuditLogger("ekrs.audit")
        audit.register_event_schema("constraint_solved", {"trace_id", "query"})
        audit.log_event("constraint_solved", trace_id="abc", query="温度")
    """

    def __init__(self, name: str = "ekrs.audit", level: int = logging.INFO):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._logger.propagate = False  # do NOT bubble to root
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
        self._schemas: dict[str, set[str]] = {}

    def register_event_schema(
        self, event_type: str, required_fields: set[str]
    ) -> None:
        """Register required fields for an event type (sets; overwrites if already registered)."""
        self._schemas[event_type] = required_fields

    def validate_event(self, event_type: str, **kwargs: Any) -> None:
        """Raise ValueError if required fields for event_type are missing."""
        required = self._schemas.get(event_type, set())
        missing = required - set(kwargs.keys())
        if missing:
            raise ValueError(
                f"audit event '{event_type}' missing required fields: {missing}"
            )

    def log_event(self, event_type: str, **kwargs: Any) -> None:
        """Log a structured audit event. Validates against schema if registered."""
        if event_type in self._schemas:
            self.validate_event(event_type, **kwargs)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **kwargs,
        }
        self._logger.info(json.dumps(entry, ensure_ascii=False, default=str))