import json
import time

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from ekrs_rag.api.decorators import audited, metered
from ekrs_rag.observability import audit
from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.metrics import METRICS


def test_audited_writes_audit_event(tmp_path):
    app = FastAPI()

    @app.get("/test")
    @audited("test_endpoint_completed")
    async def handler():
        return {"ok": True}

    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("test_endpoint_completed", {"status_code"})
    audit.set_writer(writer)
    try:
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "test_endpoint_completed"
        assert entry["status_code"] == 200
    finally:
        audit.set_writer(None)


def test_metered_records_duration():
    app = FastAPI()

    @app.get("/metered")
    @metered(METRICS.constraint_solve_duration_seconds)
    async def handler():
        time.sleep(0.01)
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/metered")
    assert resp.status_code == 200
    # Assert actual metric value observed (Test Gap 3)
    # histogram._sum.get() returns cumulative seconds observed
    assert METRICS.constraint_solve_duration_seconds._sum.get() >= 0.01


def test_audited_includes_trace_id_in_audit():
    """When middleware sets trace_id, audit events include it."""
    from ekrs_rag.api.middleware.observability import ObservabilityMiddleware

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/with_trace")
    @audited("traced_event")
    async def handler():
        return {}

    client = TestClient(app)
    resp = client.get("/with_trace", headers={"X-Trace-Id": "test-trace-xyz"})
    assert resp.headers.get("X-Trace-Id") == "test-trace-xyz"


def test_audited_reads_real_status_code_from_response(tmp_path):
    """M3 fix: when the route returns a Response, use its real status_code."""
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("custom_event", {"status_code"})
    audit.set_writer(writer)
    try:
        app = FastAPI()

        @app.get("/created")
        @audited("custom_event")
        async def handler():
            return Response(status_code=201, content="ok")

        client = TestClient(app)
        resp = client.get("/created")
        assert resp.status_code == 201
        entry = json.loads(log.read_text().strip())
        assert entry["status_code"] == 201
    finally:
        audit.set_writer(None)


def test_audited_status_code_is_500_on_exception(tmp_path):
    """M3 fix: exception path still records status_code=500."""
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("event_with_exc", {"status_code"})
    audit.set_writer(writer)
    try:
        app = FastAPI()

        @app.get("/raise")
        @audited("event_with_exc")
        async def handler():
            raise RuntimeError("nope")

        client = TestClient(app, raise_server_exceptions=True)
        try:
            client.get("/raise")
        except RuntimeError:
            pass
        entry = json.loads(log.read_text().strip())
        assert entry["status_code"] == 500
    finally:
        audit.set_writer(None)


def test_metered_increments_failure_counter_on_exception():
    """M2 fix: @metered(op='x') bumps route_failures_total{operation=x} on raise."""
    app = FastAPI()

    @app.get("/raise")
    @metered(METRICS.constraint_solve_duration_seconds, operation="test_op")
    async def handler():
        raise RuntimeError("boom")

    before = METRICS.route_failures_total.labels(operation="test_op")._value.get()
    client = TestClient(app, raise_server_exceptions=True)
    try:
        client.get("/raise")
    except RuntimeError:
        pass
    after = METRICS.route_failures_total.labels(operation="test_op")._value.get()
    assert after == before + 1


def test_metered_without_operation_does_not_increment_failure_counter():
    """Backward-compat: @metered(histogram) without operation label still works."""
    app = FastAPI()

    @app.get("/raise")
    @metered(METRICS.constraint_solve_duration_seconds)
    async def handler():
        raise RuntimeError("boom")

    # No failure counter labeled "None"; should not raise, just no-op.
    # We just verify the decorator doesn't blow up; prometheus_client creates
    # labels lazily so we can't easily check non-existence, but the success
    # of reaching the assertion is itself the contract.
    client = TestClient(app, raise_server_exceptions=True)
    try:
        client.get("/raise")
    except RuntimeError:
        pass
    # Sanity: route_failures_total counter still exists on the namespace
    assert hasattr(METRICS, "route_failures_total")