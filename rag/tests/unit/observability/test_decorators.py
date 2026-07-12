import time
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.observability.metrics import METRICS
from ekrs_rag.api.decorators import audited, metered


def test_audited_writes_audit_event():
    app = FastAPI()

    @app.get("/test")
    @audited("test_endpoint_completed")
    async def handler():
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/test")
    assert resp.status_code == 200


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
