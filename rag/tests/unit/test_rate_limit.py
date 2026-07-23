"""Unit tests for Phase 8 T8-1 — per-IP rate limiting on /v1/* routes.

Scope (per docs/superpowers/plans/2026-07-23-phase8-scope.md T8-1):

- Default rate: 60 / minute per peer IP.
- Apply to /v1/* routes only.
- Exempt: /healthz, /health, /metrics, /docs, /redoc, /openapi.json
  (probes + Swagger UI scrapers would otherwise exhaust the budget).
- 429 response carries Retry-After header.
- Limit is configurable via EKRS_RATE_LIMIT env var.

Following the Phase 6C T3 TDD-fixture convention: build a minimal
FastAPI app with no lifespan (no Qdrant / Redis / parsers).
"""
from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.middleware import rate_limit
from ekrs_rag.api.middleware.rate_limit import (
    EXEMPT_PATHS,
    install_rate_limiter,
)


def _build_minimal_app() -> FastAPI:
    """Minimal FastAPI app with one /v1/* route and the exempt paths
    declared inline. No router, no lifespan, no Qdrant/Redis."""
    app = FastAPI()
    install_rate_limiter(app)

    @app.get("/v1/limited")
    async def limited_route():
        return {"ok": True}

    @app.get("/healthz")
    async def healthz_route():
        return {"ok": True}

    @app.get("/health")
    async def health_route():
        return {"ok": True}

    @app.get("/docs")
    async def docs_route():
        return {"html": "<swagger>"}

    @app.get("/redoc")
    async def redoc_route():
        return {"html": "<redoc>"}

    @app.get("/openapi.json")
    async def openapi_route():
        return {"openapi": "3.1.0"}

    return app


@pytest.fixture(autouse=True)
def _reset_bucket() -> Iterator[None]:
    """Reset the shared token bucket before every test so cross-test
    state is invisible."""
    bucket = rate_limit._token_bucket_for_tests()
    bucket.reset()
    yield
    bucket.reset()


@pytest.fixture
def limited_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A minimal app where the bucket ceiling is set to 3 (so tests
    stay fast) and the window stays at the default 60s."""
    bucket = rate_limit._token_bucket_for_tests()
    bucket._max = 3
    bucket._window = 60
    return _build_minimal_app()


@pytest.fixture
def client(limited_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(limited_app) as c:
        yield c


# ---------------------------------------------------------------------------
# Burst behavior
# ---------------------------------------------------------------------------


def test_under_burst_returns_200(client: TestClient) -> None:
    """Inside the configured burst (limit=3), all requests succeed."""
    for _ in range(3):
        r = client.get("/v1/limited")
        assert r.status_code == 200, r.text


def test_over_burst_returns_429(client: TestClient) -> None:
    """The (limit+1)-th request returns 429."""
    for _ in range(3):
        assert client.get("/v1/limited").status_code == 200
    r = client.get("/v1/limited")
    assert r.status_code == 429, r.text
    assert "rate limit" in r.text.lower()


def test_429_carries_retry_after_header(client: TestClient) -> None:
    """429 response must include Retry-After so clients back off correctly."""
    for _ in range(3):
        client.get("/v1/limited")
    r = client.get("/v1/limited")
    assert r.status_code == 429
    assert "retry-after" in {h.lower() for h in r.headers.keys()}


# ---------------------------------------------------------------------------
# Exempt route surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/healthz", "/health", "/docs", "/redoc", "/openapi.json"],
)
def test_exempt_path_unaffected_by_burst(client: TestClient, path: str) -> None:
    """Exempt paths must NEVER return 429 even when the /v1/* budget
    has been exhausted. Drives the k8s probe + Swagger scraper scenario."""
    # Exhaust the /v1/* budget first
    for _ in range(3):
        client.get("/v1/limited")
    assert client.get("/v1/limited").status_code == 429
    # Now hit each exempt path 10 times. They should all be 200.
    for _ in range(10):
        r = client.get(path)
        assert r.status_code == 200, f"{path} unexpectedly returned {r.status_code}"


def test_exempt_path_set_matches_plan_doc() -> None:
    """Pin the exempt-path membership to the plan-doc acceptance gate.
    Adding a new exempt path without updating docs/USAGE.md + the plan
    doc should fail this test."""
    assert EXEMPT_PATHS == frozenset({
        "/healthz",
        "/health",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
    })


# ---------------------------------------------------------------------------
# Per-IP independence (the "trust proxy" question)
# ---------------------------------------------------------------------------


def test_different_ip_has_independent_budget(
    limited_app: FastAPI,
) -> None:
    """Two distinct client IPs each get their own burst budget.

    Requires EKRS_TRUST_PROXY=true (off by default) so the limiter
    reads X-Forwarded-For. The header is the canonical way a k8s
    Ingress records the real client IP behind a NAT or load
    balancer; without trust-proxy mode, every client behind one
    Ingress shares a single budget, which would let one noisy
    tenant DoS the budget for the whole egress.
    """
    # Patch the module-level flag directly. Patching os.environ after
    # import has no effect because the module reads the env var at
    # construction time.
    with patch.object(rate_limit, "_TRUST_PROXY", True):
        with TestClient(limited_app) as c:
            for _ in range(3):
                assert c.get(
                    "/v1/limited", headers={"X-Forwarded-For": "1.2.3.4"}
                ).status_code == 200
            # IP 1.2.3.4 is now at the cap; switch to 5.6.7.8 — fresh budget
            for _ in range(3):
                assert c.get(
                    "/v1/limited", headers={"X-Forwarded-For": "5.6.7.8"}
                ).status_code == 200
            # 5.6.7.8 is also at the cap now
            r = c.get("/v1/limited", headers={"X-Forwarded-For": "5.6.7.8"})
            assert r.status_code == 429


# ---------------------------------------------------------------------------
# Configurability
# ---------------------------------------------------------------------------


def test_limit_is_configurable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """EKRS_RATE_LIMIT env var overrides the default 60/minute."""
    import importlib

    monkeypatch.setenv("EKRS_RATE_LIMIT", "2")
    # Reload so the module re-reads the env var at construction time.
    importlib.reload(rate_limit)
    try:
        bucket = rate_limit._token_bucket_for_tests()
        bucket._max = 2
        bucket.reset()
        app = _build_minimal_app()
        with TestClient(app) as c:
            assert c.get("/v1/limited").status_code == 200
            assert c.get("/v1/limited").status_code == 200
            assert c.get("/v1/limited").status_code == 429
    finally:
        importlib.reload(rate_limit)


def test_default_limit_is_sixty_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When EKRS_RATE_LIMIT is unset, the default ceiling is 60/minute
    (so production deployments get sensible behavior without extra
    env config). Smoke test: requests 1..60 are 200, request 61 is 429.
    """
    monkeypatch.delenv("EKRS_RATE_LIMIT", raising=False)
    import importlib

    importlib.reload(rate_limit)
    try:
        bucket = rate_limit._token_bucket_for_tests()
        # Confirm the post-reload ceiling is 60
        assert bucket._max == 60, f"expected default 60, got {bucket._max}"
        # Confirm the post-reload window is 60s
        assert bucket._window == 60, f"expected 60s window, got {bucket._window}"
    finally:
        importlib.reload(rate_limit)


# ---------------------------------------------------------------------------
# Cold-cache / re-arm behavior
# ---------------------------------------------------------------------------


def test_window_resets_after_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the rate-limit window expires, requests are allowed again.

    We override the limiter's window to a tiny duration (1 second) so
    the test stays fast. This proves the limiter is not a "block for
    the lifetime of the process" snafu.
    """
    import importlib
    import time as _t

    monkeypatch.setenv("EKRS_TEST_RATE_LIMIT_WINDOW_SEC", "1")
    importlib.reload(rate_limit)
    try:
        bucket = rate_limit._token_bucket_for_tests()
        bucket._max = 1
        bucket.reset()
        app = _build_minimal_app()
        with TestClient(app) as c:
            assert c.get("/v1/limited", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 200
            # Immediately retry — limited by limit=1
            r = c.get("/v1/limited", headers={"X-Forwarded-For": "10.0.0.1"})
            assert r.status_code == 429
            # Wait for the window to roll over, then retry
            _t.sleep(1.1)
            r = c.get("/v1/limited", headers={"X-Forwarded-For": "10.0.0.1"})
            assert r.status_code == 200, (
                f"expected 200 after window reset, got {r.status_code}"
            )
    finally:
        monkeypatch.delenv("EKRS_TEST_RATE_LIMIT_WINDOW_SEC", raising=False)
        importlib.reload(rate_limit)
