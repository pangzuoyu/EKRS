"""Unit tests for the Prometheus metrics registry.

Covers: METRICS namespace presence for all 13 counters/gauges/histograms,
is_route_template guard, safe_inc/safe_observe label-cardinality protection.
"""
import logging

from ekrs_rag.observability.metrics import (
    METRICS, safe_inc, safe_observe, is_route_template,
)


def test_all_metrics_registered():
    """All 13 documented metrics exist on METRICS namespace."""
    assert hasattr(METRICS, "http_requests_total")
    assert hasattr(METRICS, "http_request_duration_seconds")
    assert hasattr(METRICS, "http_requests_inprogress")
    assert hasattr(METRICS, "ingestion_total")
    assert hasattr(METRICS, "ingestion_duration_seconds")
    assert hasattr(METRICS, "ingestion_chunks_written")
    assert hasattr(METRICS, "constraint_solve_total")
    assert hasattr(METRICS, "constraint_solve_duration_seconds")
    assert hasattr(METRICS, "constraint_branches_count")
    assert hasattr(METRICS, "lock_acquire_total")
    assert hasattr(METRICS, "compensation_pending_tasks")
    assert hasattr(METRICS, "compensation_retries_total")
    assert hasattr(METRICS, "qdrant_write_failures_total")


def test_is_route_template_accepts_only_templates():
    # Route template must have placeholder pattern or be plain
    assert is_route_template("/v1/constraints") is True
    assert is_route_template("/v1/docs/{doc_id}") is True
    # Interpolated values must be rejected
    assert is_route_template("/v1/docs/abc-123-def") is False
    assert is_route_template("/v1/docs/123") is False


def test_safe_inc_rejects_interpolated_label(caplog):
    """safe_inc with bad label value logs warning, does not raise."""
    with caplog.at_level(logging.WARNING):
        safe_inc(METRICS.http_requests_total,
                 endpoint="/v1/docs/abc-123",
                 method="GET", status="2xx")
    # Counter should not have been incremented
    val = METRICS.http_requests_total.labels(
        endpoint="/v1/docs/abc-123", method="GET", status="2xx"
    )._value.get()
    assert val == 0


def test_safe_inc_accepts_template_label():
    safe_inc(METRICS.http_requests_total,
             endpoint="/v1/docs/{doc_id}",
             method="GET", status="2xx")
    val = METRICS.http_requests_total.labels(
        endpoint="/v1/docs/{doc_id}", method="GET", status="2xx"
    )._value.get()
    assert val == 1


def test_safe_observe_works():
    safe_observe(METRICS.constraint_solve_duration_seconds, 0.123)
    # Just verify no exception; histogram state verified via prometheus_client
