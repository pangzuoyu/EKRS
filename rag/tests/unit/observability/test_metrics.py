"""Unit tests for the Prometheus metrics registry.

Covers: METRICS namespace presence for all 14 counters/gauges/histograms,
is_route_template guard, safe_inc/safe_observe label-cardinality protection.
"""
import logging

from ekrs_rag.observability.metrics import (
    METRICS, safe_inc, safe_observe, is_route_template,
)


def test_all_metrics_registered():
    """All 14 documented metrics exist on METRICS namespace."""
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
    assert hasattr(METRICS, "route_failures_total")
    assert hasattr(METRICS, "audit_write_failures_total")


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


def test_is_route_template_rejects_missing_or_empty_segments():
    assert is_route_template("") is False
    assert is_route_template("v1/constraints") is False
    assert is_route_template("/") is False


def test_safe_inc_logs_metric_failure(caplog):
    class BrokenCounter:
        def labels(self, **labels):
            raise RuntimeError("counter unavailable")

    with caplog.at_level(logging.WARNING):
        safe_inc(BrokenCounter(), status="failed")

    assert "metric inc failed: counter unavailable" in caplog.text


def test_safe_observe_supports_labels():
    class RecordingHistogram:
        def labels(self, **labels):
            self.labels_seen = labels
            return self

        def observe(self, value):
            self.value_seen = value

    histogram = RecordingHistogram()
    safe_observe(histogram, 0.25, operation="retrieve")

    assert histogram.labels_seen == {"operation": "retrieve"}
    assert histogram.value_seen == 0.25


def test_safe_observe_logs_metric_failure(caplog):
    class BrokenHistogram:
        def observe(self, value):
            raise RuntimeError("histogram unavailable")

    with caplog.at_level(logging.WARNING):
        safe_observe(BrokenHistogram(), 0.25)

    assert "metric observe failed: histogram unavailable" in caplog.text
