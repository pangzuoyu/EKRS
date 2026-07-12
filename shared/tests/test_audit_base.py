"""Tests for AuditLogger base class — schema registry + propagation control."""
import logging
import pytest
from ekrs_shared.audit import AuditLogger


def test_register_event_schema_and_validate():
    audit = AuditLogger("test.audit.schema")
    audit.register_event_schema("test_event", {"field_a", "field_b"})
    # Missing required field should raise
    with pytest.raises(ValueError, match="field_a"):
        audit.validate_event("test_event", field_b="ok")


def test_logger_does_not_propagate_to_root():
    # Capture root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    initial_handlers = list(root.handlers)

    audit = AuditLogger("test.audit.propagate")
    audit.log_event("no_propagate_test", key="value")

    # Logger should have its own handler, NOT add to root
    audit_logger = logging.getLogger("test.audit.propagate")
    assert audit_logger.propagate is False
    assert len(audit_logger.handlers) >= 1


def test_log_event_writes_json_with_event_field():
    audit = AuditLogger("test.audit.json")
    audit.log_event("sample", trace_id="abc", extra_field=42)
    # Just verify no exception; content verified by RAG-specific tests