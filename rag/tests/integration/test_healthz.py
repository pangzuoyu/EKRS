"""Integration test for Phase 13a T1 slim liveness contract.

Verifies the new /healthz + /ready router is wired correctly via
``create_app()`` and the contracts hold end-to-end.

TestClient without context manager does NOT trigger lifespan, so
``app.state.qdrant`` / ``app.state.redis`` stay None — /ready is
expected to 503 (dependency uninitialized), /healthz 200 (no dep
coupling).

AUDIT_LOG_PATH / TASK_DB_PATH / DOCUMENTS_DB_PATH must be set BEFORE
``create_app()`` because Settings reads env at module load time.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import create_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("DOCUMENTS_DB_PATH", str(tmp_path / "documents.db"))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

    app = create_app()
    return TestClient(app)


def test_healthz_slim_contract_via_create_app(client):
    """/healthz via create_app() returns slim body (no audit_index fields)."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    # T1 contract: ONLY status + uptime_s.
    assert set(body.keys()) == {"status", "uptime_s"}
    assert body["status"] == "ok"


def test_ready_503_when_dependencies_not_initialized(client):
    """/ready via create_app() (lifespan NOT triggered) → 503.

    Confirms /ready gracefully reports dep-unavailable instead of 500.
    """
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "dependency unavailable"}


def test_ready_200_when_lifespan_set_state_qdrant(client):
    """T10 E2E regression: lifespan sets ``app.state.qdrant`` (NOT
    ``app.state.qdrant_manager``); /ready returns 200 when both deps
    ping OK.

    Bug surfaced by T10 real-container E2E: main.py was setting
    ``app.state.qdrant_manager`` (only-ever-written, never read) so
    /ready always 503'd in production. Fix: main.py now writes
    ``app.state.qdrant`` matching the convention used by
    test_notify_step5_wiring / test_blocks_route / test_health_ready.
    """
    import fakeredis.aioredis

    class _PingableQdrant:
        def count_points(self) -> int:
            return 0

    # Starlette State is a custom object — setattr works directly even
    # if the attr didn't exist before.
    client.app.state.qdrant = _PingableQdrant()
    client.app.state.redis = fakeredis.aioredis.FakeRedis()

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_plain_health_endpoint_still_returns_ok(client):
    """/health (plain text, kept for backward compat) is unchanged."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.text == "ok"