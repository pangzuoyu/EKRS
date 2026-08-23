"""Phase 13a T1 — /healthz slim liveness + /ready dependency probe.

Two endpoints, two contracts:

- ``/healthz`` — slim liveness. ``{"status":"ok","uptime_s":N}`` only;
  NO dependency probes. Target latency <10ms (eng-review Issue 4 校正).
  Used by k8s liveness probes to detect process death, NOT dep health.

- ``/ready``   — dependency probe. 200 ``{"status":"ready"}`` iff BOTH
  qdrant ``count_points()`` AND redis ``PING()`` succeed; 503
  ``{"detail":"dependency unavailable"}`` otherwise. Budget <200ms
  (allows dep probe overhead).

Why separate endpoints (eng-review Issue 4):
A failing dep should NOT trigger a pod restart (liveness). It SHOULD
remove the pod from the service load balancer (readiness) so traffic
shifts to a healthy replica. Conflating both into one /healthz makes
liveness flap on transient dep hiccups and produces cascading
restarts.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Module-level start time for uptime. Captured at import (process boot).
_START = time.time()

# Single 503 body used by every failure path in /ready (no leak of
# which dep failed — avoids reconnaissance by unauthenticated probes).
_UNAVAILABLE_BODY: dict[str, str] = {"detail": "dependency unavailable"}


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Slim liveness: status + uptime only. No I/O, no dep probes.

    Per eng-review Issue 4: must stay under 10ms. The body intentionally
    contains only ``status`` and ``uptime_s`` — adding fields here
    encourages callers to misinterpret liveness as readiness.
    """
    return {"status": "ok", "uptime_s": round(time.time() - _START, 3)}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Dependency probe: qdrant ``count_points`` + redis ``PING``.

    Returns 200 only if BOTH probes succeed. 503 if either probe raises
    OR the dep is missing from ``app.state`` (not initialized yet).
    Per eng-review Issue 4: budget <200ms (allows dep probe overhead).

    Failure detail is intentionally NOT leaked in the body (constant
    ``_UNAVAILABLE_BODY``) — operators read logs/audit for diagnosis.
    """
    qdrant = getattr(request.app.state, "qdrant", None)
    redis = getattr(request.app.state, "redis", None)
    if qdrant is None or redis is None:
        logger.warning(
            "/ready: dep uninitialized qdrant=%s redis=%s",
            qdrant is not None, redis is not None,
        )
        return JSONResponse(status_code=503, content=_UNAVAILABLE_BODY)

    try:
        qdrant.count_points()
    except Exception as e:
        logger.warning("/ready: qdrant probe failed: %s", e)
        return JSONResponse(status_code=503, content=_UNAVAILABLE_BODY)

    try:
        await redis.ping()
    except Exception as e:
        logger.warning("/ready: redis probe failed: %s", e)
        return JSONResponse(status_code=503, content=_UNAVAILABLE_BODY)

    return JSONResponse(status_code=200, content={"status": "ready"})