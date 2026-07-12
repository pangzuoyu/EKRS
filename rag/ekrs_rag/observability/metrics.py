"""Prometheus metrics registry for EKRS RAG.

12 metrics across HTTP/ingestion/solve/concurrency/qdrant, plus one
internal audit-durability counter. Cardinality guard: any label named
``endpoint`` must be a route template (literal segments or ``{param}``
placeholders) — interpolated path values are rejected before reaching
the registry.

Helpers ``safe_inc`` / ``safe_observe`` / ``safe_set`` wrap the
prometheus_client methods with try/except so that a metric failure
never propagates into the caller.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger("ekrs.observability.metrics")

# Buckets (per Phase 5 spec)
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SOLVE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
INGEST_BUCKETS = (0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0)
BRANCH_BUCKETS = (1, 2, 3, 5, 10)

# Endpoint label validation: must be route template.
# Templates: literal path segments OR {param} placeholders.
# Literal segments must NOT contain dashes and must NOT be all-digit
# (those patterns look like interpolated IDs and would blow up cardinality).
_SEGMENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"^\{[a-zA-Z_][a-zA-Z0-9_]*\}$")
_ALL_DIGIT_RE = re.compile(r"^[0-9]+$")


def is_route_template(path: str) -> bool:
    """True iff path is a route template (no interpolated values)."""
    if not path or not path.startswith("/"):
        return False
    segments = path.split("/")[1:]  # drop leading empty
    if not segments:
        return False
    for seg in segments:
        if _PLACEHOLDER_RE.match(seg):
            continue
        if _ALL_DIGIT_RE.match(seg):
            # All-digit segments are almost certainly interpolated values.
            return False
        if _SEGMENT_RE.match(seg):
            continue
        return False
    return True


# Metric definitions

http_requests_total = Counter(
    "rag_http_requests_total",
    "Total HTTP requests",
    ["endpoint", "method", "status"],
)

http_request_duration_seconds = Histogram(
    "rag_http_request_duration_seconds",
    "HTTP request latency",
    ["endpoint", "method"],
    buckets=HTTP_BUCKETS,
)

http_requests_inprogress = Gauge(
    "rag_http_requests_inprogress",
    "Currently in-flight HTTP requests",
    ["endpoint"],
)

ingestion_total = Counter(
    "rag_ingestion_total",
    "Ingestion attempts by terminal status",
    ["status"],
)

ingestion_duration_seconds = Histogram(
    "rag_ingestion_duration_seconds",
    "End-to-end ingestion latency",
    buckets=INGEST_BUCKETS,
)

ingestion_chunks_written = Counter(
    "rag_ingestion_chunks_written",
    "Total chunks written to Qdrant",
)

constraint_solve_total = Counter(
    "rag_constraint_solve_total",
    "Constraint solve attempts by outcome",
    ["outcome"],
)

constraint_solve_duration_seconds = Histogram(
    "rag_constraint_solve_duration_seconds",
    "Solver latency",
    buckets=SOLVE_BUCKETS,
)

constraint_branches_count = Histogram(
    "rag_constraint_branches_count",
    "Branches returned per solve",
    buckets=BRANCH_BUCKETS,
)

lock_acquire_total = Counter(
    "rag_lock_acquire_total",
    "Redis lock acquisition attempts",
    ["result"],
)

compensation_pending_tasks = Gauge(
    "rag_compensation_pending_tasks",
    "Tasks eligible for compensation retry",
)

compensation_retries_total = Counter(
    "rag_compensation_retries_total",
    "Total compensation retries",
    ["result"],
)

qdrant_write_failures_total = Counter(
    "rag_qdrant_write_failures_total",
    "Qdrant write failures",
    ["operation"],
)

# Internal: not in spec but useful for audit durability
audit_write_failures_total = Counter(
    "rag_audit_write_failures_total",
    "Audit log write failures",
)


METRICS = SimpleNamespace(
    http_requests_total=http_requests_total,
    http_request_duration_seconds=http_request_duration_seconds,
    http_requests_inprogress=http_requests_inprogress,
    ingestion_total=ingestion_total,
    ingestion_duration_seconds=ingestion_duration_seconds,
    ingestion_chunks_written=ingestion_chunks_written,
    constraint_solve_total=constraint_solve_total,
    constraint_solve_duration_seconds=constraint_solve_duration_seconds,
    constraint_branches_count=constraint_branches_count,
    lock_acquire_total=lock_acquire_total,
    compensation_pending_tasks=compensation_pending_tasks,
    compensation_retries_total=compensation_retries_total,
    qdrant_write_failures_total=qdrant_write_failures_total,
    audit_write_failures_total=audit_write_failures_total,
)


def _validate_endpoint_label(labels: dict[str, Any]) -> bool:
    """If 'endpoint' label is present, enforce route-template format."""
    endpoint = labels.get("endpoint")
    if endpoint is None:
        return True
    if not is_route_template(endpoint):
        logger.warning(
            "metric label rejected: endpoint=%s is not a route template",
            endpoint,
        )
        return False
    return True


def safe_inc(counter: Counter, **labels: Any) -> None:
    """Increment counter; reject labels with interpolated path values."""
    if not _validate_endpoint_label(labels):
        return
    try:
        counter.labels(**labels).inc()
    except Exception as e:
        logger.warning("metric inc failed: %s", e)


def safe_observe(histogram: Histogram, value: float, **labels: Any) -> None:
    """Observe histogram value; reject labels with interpolated path values."""
    if not _validate_endpoint_label(labels):
        return
    try:
        if labels:
            histogram.labels(**labels).observe(value)
        else:
            histogram.observe(value)
    except Exception as e:
        logger.warning("metric observe failed: %s", e)


def safe_set(gauge: Gauge, value: float, **labels: Any) -> None:
    """Set gauge value; reject labels with interpolated path values."""
    if not _validate_endpoint_label(labels):
        return
    try:
        if labels:
            gauge.labels(**labels).set(value)
        else:
            gauge.set(value)
    except Exception as e:
        logger.warning("metric set failed: %s", e)
