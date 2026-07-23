"""FastAPI middleware: per-IP rate limiting on /v1/* routes (Phase 8 T8-1).

Scope (per docs/superpowers/plans/2026-07-23-phase8-scope.md):

- Default rate: 60 / minute per peer IP.
- Apply to /v1/* only.
- Exempt: /healthz, /health, /metrics, /docs, /redoc, /openapi.json
  (probes + Swagger UI scrapers would otherwise exhaust the budget).
- 429 response carries Retry-After so clients can back off correctly.
- Limit is configurable via EKRS_RATE_LIMIT env var (default 60).
- Per-IP key: X-Forwarded-For first hop IF EKRS_TRUST_PROXY=true,
  else request.client.host. Default false because trusting the
  header without an Ingress in the way lets a malicious caller
  exhaust another tenant's budget by forging the header.

Why a hand-rolled limiter and not SlowAPI:
SlowAPI ships route-decorator-only enforcement. To limit an entire
URL prefix generically would require decorating every /v1/* route
declaration (6 routers, dozens of routes, easy to forget new ones
when added). A 60-line token-bucket middleware gives us the same
behavior with a single source of truth for the exempt list and
the limit. Memory footprint: O(unique IP addresses). For a service
exposing on Kubernetes Ingress, this stays in single-digit MB
in normal operation. Multi-worker Redis-backed limiters are a
post-deploy concern per ekrs-handbook.md §6.2 PD-3.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


_DEFAULT_LIMIT_PER_MINUTE = int(os.environ.get("EKRS_RATE_LIMIT", "60"))
# Test-time window override so `test_window_resets_after_expiry` does
# not have to wait 60 seconds. Production MUST NOT set this env var.
_WINDOW_SECONDS = int(os.environ.get("EKRS_TEST_RATE_LIMIT_WINDOW_SEC", "60"))

# Routes exempt from rate limiting. Matches the plan-doc acceptance
# gate (T8-1: /healthz, /metrics, /docs, /redoc, /openapi.json
# unaffected). /metrics is also added because the Sidecar exporter
# shares the FastAPI app in dev mode (METRICS_HOST=127.0.0.1).
EXEMPT_PATHS: frozenset[str] = frozenset({
    "/healthz",
    "/health",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
})

_TRUST_PROXY = os.environ.get("EKRS_TRUST_PROXY", "false").lower() in (
    "1", "true", "yes", "on"
)


def _peer_key(request: Request) -> str:
    """Resolve the per-IP key for the current request."""
    if _TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
    client = request.client
    return client.host if client is not None else "unknown"


class _TokenBucket:
    """Sliding-window counter keyed on peer IP.

    Stores `(window_start_monotonic, count)`. A new request inside
    the window increments `count`. When the window has elapsed the
    counter resets. LRU eviction caps memory when many distinct IPs
    hit the service (the cap is set conservatively — 10k entries is
    plenty for a single-process service exposed on an Ingress).

    Thread safety: a single `threading.Lock` guards the dict. The
    critical section is microseconds (dict + tuple manipulation),
    so contention is a non-issue at any realistic RPS. Multiprocess
    scaling is deferred (PD-3).
    """

    def __init__(
        self,
        max_requests: int = _DEFAULT_LIMIT_PER_MINUTE,
        window_sec: int = _WINDOW_SECONDS,
        capacity: int = 10000,
    ) -> None:
        self._max = max_requests
        self._window = window_sec
        self._capacity = capacity
        self._buckets: "OrderedDict[str, tuple[float, int]]" = OrderedDict()
        self._lock = threading.Lock()

    def hit_and_check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None:
                # New key — start a fresh window
                self._buckets[key] = (now, 1)
                return True
            window_start, count = entry
            if now - window_start >= self._window:
                # Window expired — reset
                self._buckets[key] = (now, 1)
                # Promote to most-recently-used (still alive, just bumped)
                self._buckets.move_to_end(key)
                return True
            if count >= self._max:
                # Limit hit; re-promote so active IPs aren't evicted
                self._buckets.move_to_end(key)
                return False
            # Still within budget
            self._buckets[key] = (window_start, count + 1)
            self._buckets.move_to_end(key)
            # LRU trim
            while len(self._buckets) > self._capacity:
                self._buckets.popitem(last=False)
            return True

    def retry_after(self, key: str) -> int:
        """Seconds until the bucket resets (best-effort, ≥1)."""
        now = time.monotonic()
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None:
                return 1
            window_start, _ = entry
            remaining = self._window - int(now - window_start)
            return max(1, remaining)

    def reset(self) -> None:
        """Drop every bucket. Test helper; not exposed in production paths."""
        with self._lock:
            self._buckets.clear()


# One bucket per process. SlowAPI's `Limiter` is also a module-level
# singleton for the same reason; the alternative would be to attach it
# to app.state, but that makes the import-time wiring in tests
# noisier. A module-level singleton is the lightest dependency
# structure that survives both the production `create_app()` path and
# the minimal-FastAPI test-fixture path.
_bucket = _TokenBucket()


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """Reject over-budget /v1/* requests with 429 + Retry-After."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in EXEMPT_PATHS or not path.startswith("/v1/"):
            return await call_next(request)
        key = _peer_key(request)
        if _bucket.hit_and_check(key):
            return await call_next(request)
        retry_after = _bucket.retry_after(key)
        return JSONResponse(
            status_code=429,
            content={
                "detail": "rate limit exceeded",
                "limit": _bucket._max,
                "retry_after_sec": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )


def install_rate_limiter(app: FastAPI, limit: int | None = None) -> None:
    """Install the rate limiter on `app`.

    `limit` overrides the configured EKRS_RATE_LIMIT for this app
    instance (only useful in tests; production should set the env
    var so the limiter is configured consistently across processes).
    Idempotent — calling twice is a no-op because FastAPI's middleware
    stack rejects duplicates. Tests should mutate `_bucket` directly
    via `_token_bucket_for_tests()` if they need to change the
    configured ceiling without rebuilding the app.
    """
    if limit is not None:
        _bucket._max = limit
        _bucket.reset()
    app.add_middleware(_RateLimitMiddleware)


def _token_bucket_for_tests() -> _TokenBucket:
    """Test-only hook for inspecting or resetting the shared bucket."""
    return _bucket


__all__: tuple[str, ...] = (
    "EXEMPT_PATHS",
    "install_rate_limiter",
    "_token_bucket_for_tests",
)
