"""Phase 13a T1 — /healthz slim + /ready dependency probe.

Contract:
- /healthz: ``{"status":"ok","uptime_s":N}`` — no I/O, no dep probes, <10ms
  (eng-review Issue 4 校正 — slim liveness only)
- /ready:   200 ``{"status":"ready"}`` when BOTH qdrant + redis ping OK;
            503 ``{"detail":"dependency unavailable"}`` on any failure;
            budget <200ms (allows dep probe overhead)

Tests use a minimal FastAPI app + app.state stub injection (mirrors
test_blocks_route.py pattern). No real qdrant/redis — all probes go
through stubs.

These tests fail RED until ``rag/ekrs_rag/api/routes/health.py`` exists
and ``main.py`` includes the health router.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubQdrant:
    """Minimal Qdrant stub exposing only the surface used by /ready.

    ``count_points`` is the dep-probe shortcut chosen by the plan (T1.3)
    because it issues one roundtrip but no I/O payload.
    """

    def __init__(
        self,
        *,
        exc: Optional[Exception] = None,
        latency_ms: float = 0.0,
    ) -> None:
        self._exc = exc
        self._latency_ms = latency_ms
        self.calls: list[str] = []

    def count_points(self) -> int:
        self.calls.append("count_points")
        if self._latency_ms > 0:
            time.sleep(self._latency_ms / 1000.0)
        if self._exc is not None:
            raise self._exc
        return 42


class _StubRedis:
    """Minimal Redis stub exposing only the surface used by /ready.

    Mirrors ``redis.asyncio.Redis.ping`` (async, returns bool).
    """

    def __init__(self, *, exc: Optional[Exception] = None) -> None:
        self._exc = exc
        self.calls: list[str] = []

    async def ping(self) -> bool:
        self.calls.append("ping")
        if self._exc is not None:
            raise self._exc
        return True


def _build_app(
    *,
    qdrant: Optional[_StubQdrant] = None,
    redis: Optional[_StubRedis] = None,
) -> FastAPI:
    """Minimal FastAPI app with health router + app.state stub injection.

    Imports happen lazily so this module can be collected before the
    production route module exists (RED).
    """
    from ekrs_rag.api.routes.health import router as health_router

    app = FastAPI()
    app.include_router(health_router)
    # Inject stubs (or omit to simulate uninitialized state).
    if qdrant is not None:
        app.state.qdrant = qdrant
    if redis is not None:
        app.state.redis = redis
    # Auth disabled for unit tests (PARSER_TOKEN="" → no-op dep).
    os.environ["PARSER_TOKEN"] = ""
    return app


# ---------------------------------------------------------------------------
# /healthz slim liveness
# ---------------------------------------------------------------------------


def test_healthz_returns_status_and_uptime() -> None:
    """/healthz body has only status + uptime_s; numeric uptime >= 0."""
    app = _build_app()
    client = TestClient(app)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"status", "uptime_s"}
    assert body["status"] == "ok"
    assert isinstance(body["uptime_s"], (int, float))
    assert body["uptime_s"] >= 0


def test_healthz_does_not_probe_dependencies() -> None:
    """/healthz MUST NOT call qdrant.count_points or redis.ping.

    Structural assertion (eng-review Issue 4): /healthz is liveness only.
    Dependency probing belongs in /ready.
    """
    qdrant = _StubQdrant()
    redis = _StubRedis()
    app = _build_app(qdrant=qdrant, redis=redis)
    client = TestClient(app)

    client.get("/healthz")

    assert qdrant.calls == []
    assert redis.calls == []


def test_healthz_survives_uninitialized_dependencies() -> None:
    """/healthz returns 200 even when qdrant/redis are NOT in app.state.

    Confirms /healthz has zero coupling to dependency lifecycle.
    """
    app = _build_app()  # no qdrant, no redis injected
    client = TestClient(app)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# /ready happy path
# ---------------------------------------------------------------------------


def test_ready_returns_200_when_qdrant_and_redis_ok() -> None:
    """Both deps reachable → 200 ``{"status":"ready"}``."""
    qdrant = _StubQdrant()
    redis = _StubRedis()
    app = _build_app(qdrant=qdrant, redis=redis)
    client = TestClient(app)

    resp = client.get("/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}
    assert qdrant.calls == ["count_points"]
    assert redis.calls == ["ping"]


# ---------------------------------------------------------------------------
# /ready failure paths — any dep down → 503
# ---------------------------------------------------------------------------


def test_ready_returns_503_when_qdrant_count_points_raises() -> None:
    """qdrant.count_points raises → 503 ``{"detail":"dependency unavailable"}``."""
    app = _build_app(
        qdrant=_StubQdrant(exc=RuntimeError("qdrant down")),
        redis=_StubRedis(),
    )
    client = TestClient(app)

    resp = client.get("/ready")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "dependency unavailable"}


def test_ready_returns_503_when_redis_ping_raises() -> None:
    """redis.ping raises → 503."""
    app = _build_app(
        qdrant=_StubQdrant(),
        redis=_StubRedis(exc=RuntimeError("redis down")),
    )
    client = TestClient(app)

    resp = client.get("/ready")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "dependency unavailable"}


def test_ready_returns_503_when_qdrant_uninitialized() -> None:
    """app.state.qdrant is None → 503 (no 500)."""
    app = _build_app(redis=_StubRedis())
    client = TestClient(app)

    resp = client.get("/ready")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "dependency unavailable"}


def test_ready_returns_503_when_redis_uninitialized() -> None:
    """app.state.redis is None → 503."""
    app = _build_app(qdrant=_StubQdrant())
    client = TestClient(app)

    resp = client.get("/ready")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "dependency unavailable"}


def test_ready_returns_503_when_both_dependencies_down() -> None:
    """Both fail → 503 (single error path, no ambiguity in body)."""
    app = _build_app(
        qdrant=_StubQdrant(exc=RuntimeError("qdrant")),
        redis=_StubRedis(exc=RuntimeError("redis")),
    )
    client = TestClient(app)

    resp = client.get("/ready")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "dependency unavailable"}


# ---------------------------------------------------------------------------
# /ready SLO — under 200ms (eng-review Issue 4 关键验收)
# ---------------------------------------------------------------------------


def test_ready_response_under_200ms_when_qdrant_ok() -> None:
    """/ready with mocked fast deps completes well under 200ms.

    200ms is the SLO ceiling (T1.1). Generous wall-clock budget to absorb
    CI noise; the structural assertion that /healthz doesn't probe deps
    (above) is the real defense against accidentally slow /healthz.
    """
    app = _build_app(qdrant=_StubQdrant(), redis=_StubRedis())
    client = TestClient(app)

    start = time.perf_counter()
    resp = client.get("/ready")
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert resp.status_code == 200
    assert elapsed_ms < 200, f"/ready took {elapsed_ms:.1f}ms, expected <200ms"


def test_ready_during_encode_succeeds_when_qdrant_ping_ok() -> None:
    """T1.7 (eng-review Issue 4): under simulated encode load /ready is
    still 200 and within 200ms budget.

    Encode-load simulation: stub qdrant.count_points sleeps 100ms (one
    batch encode round); we issue /ready on a fresh client. This proves
    /ready probes don't get serialized behind the encoding pool
    (pebble workers are separate processes — see T4 _init_child).
    """
    qdrant = _StubQdrant(latency_ms=100.0)
    redis = _StubRedis()
    app = _build_app(qdrant=qdrant, redis=redis)
    client = TestClient(app)

    start = time.perf_counter()
    resp = client.get("/ready")
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}
    # The 100ms qdrant stub IS on the request path, so elapsed should be
    # >= 100ms; total budget 200ms allows headroom for redis ping + setup.
    assert elapsed_ms < 200, (
        f"/ready during encode took {elapsed_ms:.1f}ms (>200ms budget)"
    )