"""Integration test: ObservabilityMiddleware emits endpoint_started/completed audit.

Exercises a FastAPI app wired with the middleware against a real AuditWriter.
Verifies that trace_id appears in the audit log lines, that the response
echoes the trace_id, and that inbound X-Trace-Id headers are honored.
Also verifies that the real response.status_code (not a hardcoded 200) is
written into the endpoint_completed audit event.
"""
import json

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.observability import audit
from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.trace import get_trace_id


def _make_app():
    """Minimal FastAPI app wired with the ObservabilityMiddleware."""
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/probe")
    async def probe():
        # Confirm the contextvar is visible inside the request
        return {"trace_id": get_trace_id()}

    @app.get("/not-found")
    async def not_found():
        # FastAPI converts to 404 only via HTTPException — explicit Response
        return Response(status_code=404, content="nope")

    @app.get("/boom")
    async def boom():
        return Response(status_code=500, content="kaboom")

    return app


def test_middleware_emits_audit_events_and_propagates_trace_id(tmp_path):
    """End-to-end: trace_id flows from header → contextvar → audit log → response."""
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("endpoint_started", set())
    writer.register_event_schema("endpoint_completed", set())
    audit.set_writer(writer)

    try:
        app = _make_app()
        with TestClient(app) as client:
            # Case A: no inbound header → server-generated uuid4
            resp = client.get("/probe")
            assert resp.status_code == 200
            generated_trace = resp.headers["x-trace-id"]
            assert len(generated_trace) == 36  # uuid4 with dashes
            assert resp.json()["trace_id"] == generated_trace

            # Case B: inbound X-Trace-Id header → used verbatim
            provided = "my-custom-trace-abc"
            resp = client.get("/probe", headers={"X-Trace-Id": provided})
            assert resp.status_code == 200
            assert resp.headers["x-trace-id"] == provided
            assert resp.json()["trace_id"] == provided

        # After TestClient context exits, contextvar must be reset
        assert get_trace_id() == "unknown"

        # Audit log: 4 lines (started+completed × 2 requests), grouped by trace_id
        lines = [json.loads(l) for l in log.read_text().strip().split("\n")]
        assert len(lines) == 4

        started_a = next(
            e for e in lines
            if e["event"] == "endpoint_started" and e["trace_id"] == generated_trace
        )
        completed_a = next(
            e for e in lines
            if e["event"] == "endpoint_completed" and e["trace_id"] == generated_trace
        )
        assert started_a["endpoint"] == "/probe"
        assert started_a["method"] == "GET"
        assert "duration_ms" in completed_a

        # The provided trace_id was honored in both audit events for case B
        provided_events = [e for e in lines if e["trace_id"] == provided]
        assert len(provided_events) == 2
        assert {e["event"] for e in provided_events} == {
            "endpoint_started", "endpoint_completed"
        }
    finally:
        audit.set_writer(None)


def test_endpoint_completed_uses_real_response_status_code(tmp_path):
    """M1 fix: audit's status_code must reflect the real Response, not 200.

    Visits /not-found (404) and /boom (500) and asserts the audit log
    endpoint_completed events carry those status codes.
    """
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("endpoint_started", set())
    writer.register_event_schema("endpoint_completed", {"status_code"})
    audit.set_writer(writer)

    try:
        app = _make_app()
        with TestClient(app) as client:
            r404 = client.get("/not-found")
            assert r404.status_code == 404
            r500 = client.get("/boom")
            assert r500.status_code == 500

        lines = [json.loads(l) for l in log.read_text().strip().split("\n")]
        completed = [e for e in lines if e["event"] == "endpoint_completed"]
        # One completed event per request
        assert len(completed) == 2
        codes = sorted(e["status_code"] for e in completed)
        assert codes == [404, 500]
    finally:
        audit.set_writer(None)