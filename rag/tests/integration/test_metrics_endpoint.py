"""Integration tests for the /metrics Prometheus scrape endpoint.

The endpoint must:
  1. Return Prometheus text exposition format (200 + correct content-type).
  2. Expose every metric registered by ekrs_rag.observability.metrics.
  3. Reflect real counter increments (not a static stub).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from ekrs_rag.api.routes.metrics import router as metrics_router
from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.observability.metrics import METRICS, safe_inc


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(metrics_router)

    @app.get("/trigger")
    async def trigger():
        safe_inc(METRICS.ingestion_total, status="completed")
        return {"ok": True}

    return app


def test_metrics_endpoint_returns_prometheus_format():
    """GET /metrics returns 200 with Prometheus content-type and HELP/TYPE lines."""
    app = _make_app()
    client = TestClient(app)

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(CONTENT_TYPE_LATEST.split(";")[0].strip())
    body = resp.text
    assert "# HELP rag_http_requests_total" in body
    assert "# TYPE rag_http_requests_total counter" in body


def test_metrics_endpoint_includes_all_phase5_metrics():
    """All 13 metrics from Task 5 are exposed with # HELP lines."""
    app = _make_app()
    client = TestClient(app)

    resp = client.get("/metrics")
    body = resp.text

    expected = [
        "rag_http_requests_total",
        "rag_http_request_duration_seconds",
        "rag_http_requests_inprogress",
        "rag_ingestion_total",
        "rag_ingestion_duration_seconds",
        "rag_ingestion_chunks_written",
        "rag_constraint_solve_total",
        "rag_constraint_solve_duration_seconds",
        "rag_constraint_branches_count",
        "rag_lock_acquire_total",
        "rag_compensation_pending_tasks",
        "rag_compensation_retries_total",
        "rag_qdrant_write_failures_total",
    ]
    for name in expected:
        assert f"# HELP {name}" in body, f"missing metric: {name}"


def test_metrics_reflect_actual_traffic():
    """Trigger safe_inc via a real request, then verify counter value increased."""
    app = _make_app()
    client = TestClient(app)

    before = METRICS.ingestion_total.labels(status="completed")._value.get()
    resp = client.get("/trigger")
    assert resp.status_code == 200
    after = METRICS.ingestion_total.labels(status="completed")._value.get()

    assert after == before + 1

    scrape = client.get("/metrics")
    assert "rag_ingestion_total" in scrape.text