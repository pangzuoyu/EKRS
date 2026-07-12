"""Endpoint decorators: @audited (write audit event) + @metered (observe duration).

Both rely on ObservabilityMiddleware having set trace_id contextvar.
Both swallow all exceptions (decorator must never break the route).
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable

from ekrs_rag.observability.audit import get_writer
from ekrs_rag.observability.metrics import safe_observe
from ekrs_rag.observability.trace import get_trace_id

logger = logging.getLogger("ekrs.observability.decorators")


def audited(event_name: str) -> Callable:
    """Decorator: write audit event after route returns (success or error).

    Captures: trace_id, status_code (from response), duration_ms.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            status_code = 500
            try:
                result = await func(*args, **kwargs)
                status_code = 200
                return result
            except Exception as e:
                logger.warning("route %s raised: %s", event_name, e)
                status_code = 500
                raise
            finally:
                duration_ms = int((time.monotonic() - start) * 1000)
                writer = get_writer()
                if writer:
                    writer.write(
                        event_name,
                        trace_id=get_trace_id(),
                        status_code=status_code,
                        duration_ms=duration_ms,
                    )
        return wrapper
    return decorator


def metered(histogram) -> Callable:
    """Decorator: observe duration into the given Histogram instance.

    Type-safe: caller passes the actual Histogram object (e.g.,
    METRICS.constraint_solve_duration_seconds) instead of a magic string.
    Typo at call site → ImportError at decoration time, never silent.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.monotonic() - start
                safe_observe(histogram, duration)
        return wrapper
    return decorator
