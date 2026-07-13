"""Tests for ObservabilityMiddleware setting _skip_audit for /healthz."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.observability import audit as audit_mod
from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.trace import get_skip_audit


@pytest.fixture
def real_audit_app(tmp_path):
    """App with a real AuditWriter; tests inspect the audit log file."""
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("endpoint_started", {"trace_id", "endpoint", "method"})
    writer.register_event_schema("endpoint_completed", {"trace_id", "status_code", "duration_ms"})

    audit_mod.set_writer(writer)
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/healthz")
    def h():
        return {"skip": get_skip_audit()}

    @app.get("/probe")
    def p():
        return {"skip": get_skip_audit()}

    yield app, writer, log
    writer._file_handler.close()
    audit_mod.set_writer(None)


def test_healthz_handler_sees_skip_flag(real_audit_app):
    app, writer, log = real_audit_app
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["skip"] is True
    # /healthz wrote NOTHING to audit log
    assert log.read_text() == ""


def test_skip_flag_resets_after_healthz(real_audit_app):
    app, writer, log = real_audit_app
    c = TestClient(app)
    c.get("/healthz")
    r = c.get("/probe")
    assert r.json()["skip"] is False


def test_non_healthz_writes_to_audit(real_audit_app):
    app, writer, log = real_audit_app
    c = TestClient(app)
    r = c.get("/probe")
    assert r.status_code == 200
    lines = log.read_text().strip().split("\n")
    assert len(lines) == 2  # started + completed
    events = [json.loads(l)["event"] for l in lines]
    assert events == ["endpoint_started", "endpoint_completed"]