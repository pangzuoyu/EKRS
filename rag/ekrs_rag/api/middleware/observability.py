"""FastAPI middleware: inject trace_id + measure request duration.

The middleware sets a contextvar holding the request's trace_id so all
audit writes and metric increments inside the request see it without
explicit threading. Both `endpoint_started` and `endpoint_completed`
audit events are emitted via the AuditWriter singleton from
`ekrs_rag.observability.audit`.
"""
from __future__ import annotations

import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from ekrs_rag.observability.trace import (
    get_trace_id, reset_trace_id, set_trace_id,
    set_skip_audit, reset_skip_audit,
)

HEADER_NAME = "x-trace-id"


def extract_or_generate_trace_id(headers: dict) -> str:
    """Pull X-Trace-Id from headers (case-insensitive), else generate uuid4."""
    for k, v in headers.items():
        if k.lower() == HEADER_NAME:
            return str(v)
    return str(uuid.uuid4())


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Inject trace_id into contextvar, time the request, audit lifecycle."""

    async def dispatch(self, request: Request, call_next):
        trace_id = extract_or_generate_trace_id(dict(request.headers))
        token = set_trace_id(trace_id)
        # Suppress ALL audit writes for /healthz (k8s probe fires every few
        # seconds; lifecycle events would dominate audit volume otherwise).
        skip_token = set_skip_audit(request.url.path == "/healthz")
        start = time.monotonic()
        # Lazy import to avoid loading audit module when middleware is unused.
        from ekrs_rag.observability.audit import get_writer
        writer = get_writer()
        if writer:
            # Use the resolved route template (e.g. "/v1/constraints/{id}")
            # rather than the raw URL to prevent cardinality explosion
            # from path-param values. Fall back to url.path for unmatched
            # routes (404s).
            route = request.scope.get("route")
            endpoint_label = route.path if route is not None else request.url.path
            writer.write(
                "endpoint_started",
                trace_id=trace_id,
                endpoint=endpoint_label,
                method=request.method,
            )
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            duration_ms = int((time.monotonic() - start) * 1000)
            if writer:
                writer.write(
                    "endpoint_completed",
                    trace_id=trace_id,
                    status_code=getattr(response, "status_code", 200),
                    duration_ms=duration_ms,
                )
            return response
        finally:
            reset_skip_audit(skip_token)
            reset_trace_id(token)
