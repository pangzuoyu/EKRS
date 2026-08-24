"""Phase 13b T5.1 / T5.3 — /v1/admin/gpu/invalidate + /v1/admin/gpu/memory-stats.

Verifies:
- Auth gate (X-Admin-Key required, 401 without it, 503 if ADMIN_KEY empty).
- gpu_invalidate flips router state so next probe re-runs self_check.
- gpu_memory_stats returns exact torch.cuda numbers; 503 if CUDA missing.
- 500 isolation when torch.cuda raises (driver fault).

Unit-test only — uses FastAPI TestClient against a minimal app. The real
failover test (audit log scan, transition detection timing) lives in
scripts/phase13b_failover_test.py + @pytest.mark.heavy wrapper.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.routes.admin import gpu_router


@pytest.fixture
def client_with_admin_key(monkeypatch: pytest.MonkeyPatch):
    """Build a tiny app with the gpu admin router and X-Admin-Key set.

    Returns (client, encoding_router_module) so tests can monkeypatch the
    router state directly without touching the singleton module global.
    """
    from ekrs_rag.core import config as cfg
    from ekrs_rag.services import encoding_router

    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"

    app = FastAPI()
    app.include_router(gpu_router)

    # Install a fake router for gpu_invalidate to operate on.
    fake = encoding_router.EncodingRouter()
    fake.try_register_gpu()  # advance to "gpu" or "cpu" depending on env
    monkeypatch.setattr(encoding_router, "get_router", lambda: fake)

    return TestClient(app), encoding_router, fake


# ---------- gpu_invalidate ----------


def test_invalidate_requires_x_admin_key(
    client_with_admin_key: tuple[TestClient, object, object],
) -> None:
    """Without X-Admin-Key, gpu_invalidate MUST return 401."""
    client, _, _ = client_with_admin_key
    resp = client.post("/v1/admin/gpu/invalidate")
    assert resp.status_code == 401
    assert "X-Admin-Key" in resp.json()["detail"]


def test_invalidate_returns_status(
    client_with_admin_key: tuple[TestClient, object, object],
) -> None:
    """With valid X-Admin-Key, gpu_invalidate marks state for re-evaluation."""
    client, _, _ = client_with_admin_key
    resp = client.post(
        "/v1/admin/gpu/invalidate",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalidated"
    assert body["next_probe_will"] == "transition_to_cpu"


def test_invalidate_returns_503_when_router_uninitialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the singleton isn't initialized (worker boot race), 503."""
    from ekrs_rag.core import config as cfg
    from ekrs_rag.services import encoding_router

    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"
    monkeypatch.setattr(encoding_router, "get_router", lambda: None)

    app = FastAPI()
    app.include_router(gpu_router)
    client = TestClient(app)
    resp = client.post(
        "/v1/admin/gpu/invalidate",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 503
    assert "not initialized" in resp.json()["detail"].lower()


def test_invalidate_503_when_admin_key_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADMIN_KEY empty → 503 (auth gate refuses to gate)."""
    from ekrs_rag.core import config as cfg

    cfg.settings.ADMIN_KEY = ""  # disabled

    app = FastAPI()
    app.include_router(gpu_router)
    client = TestClient(app)
    resp = client.post(
        "/v1/admin/gpu/invalidate",
        headers={"X-Admin-Key": "any-key"},
    )
    assert resp.status_code == 503


# ---------- gpu_memory_stats ----------


def test_memory_stats_requires_x_admin_key() -> None:
    """No key → 401."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"

    app = FastAPI()
    app.include_router(gpu_router)
    client = TestClient(app)
    resp = client.post("/v1/admin/gpu/memory-stats")
    assert resp.status_code == 401


def test_memory_stats_returns_503_when_torch_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """torch import failure (CPU-only install) → 503."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"

    # Block the import: replace sys.modules entry with None? Easier: patch
    # the import inside the handler. The handler does
    # ``import torch`` — so we patch ``torch`` itself out by raising
    # ImportError on attribute access.
    app = FastAPI()
    app.include_router(gpu_router)

    with patch.dict("sys.modules", {"torch": None}):
        # Setting a module entry to None triggers ImportError on import.
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/admin/gpu/memory-stats",
            headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
        )
    assert resp.status_code == 503


def test_memory_stats_returns_503_when_cuda_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """torch installed but CUDA unavailable → 503."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"

    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)

    app = FastAPI()
    app.include_router(gpu_router)
    client = TestClient(app)
    resp = client.post(
        "/v1/admin/gpu/memory-stats",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 503
    assert "CUDA" in resp.json()["detail"]


def test_memory_stats_returns_exact_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: returns peak + allocated + device strings."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"

    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    fake_torch.cuda.max_memory_allocated.return_value = 5_500_000_000  # 5.5 GB
    fake_torch.cuda.memory_allocated.return_value = 4_100_000_000  # 4.1 GB
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)

    # Configure device_id=0.
    monkeypatch.setattr(cfg.settings, "BGE_M3_GPU_DEVICE_ID", 0)

    app = FastAPI()
    app.include_router(gpu_router)
    client = TestClient(app)
    resp = client.post(
        "/v1/admin/gpu/memory-stats",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["peak_bytes"] == 5_500_000_000
    assert body["allocated_bytes"] == 4_100_000_000
    assert body["device"] == "cuda:0"


def test_memory_stats_returns_500_on_torch_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """torch.cuda raises (driver reset) → 500 (defensive isolation)."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"

    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    fake_torch.cuda.max_memory_allocated.side_effect = RuntimeError("driver reset")
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)

    monkeypatch.setattr(cfg.settings, "BGE_M3_GPU_DEVICE_ID", 0)

    app = FastAPI()
    app.include_router(gpu_router)
    client = TestClient(app)
    resp = client.post(
        "/v1/admin/gpu/memory-stats",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 500
    assert "driver reset" in resp.json()["detail"]