"""Integration tests for /v1/admin/embedding-cache/flush (Phase 7 T7).

Decision §4: endpoint requires X-Admin-Key and returns the number of
cache entries cleared. Reached via FastAPI TestClient against a
minimal lifespan — we set app.state.embedding_service directly to
avoid pulling in Qdrant / Redis / parsers.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.routes.admin_embedding_cache import router as cache_admin_router


class _FakeEmbeddingService:
    """Minimal stand-in for EmbeddingService exposing the cache hooks."""

    def __init__(self) -> None:
        self._cleared = 0
        self._size = 5  # pretend cache has 5 entries before flush
        self._model_version = "model.onnx=deadbeef"

    def flush_cache(self) -> int:
        self._cleared = self._size
        self._size = 0
        return self._cleared

    def cache_size(self) -> int:
        return self._size

    @property
    def model_version(self) -> str:
        return self._model_version


@pytest.fixture
def client_with_admin_key(monkeypatch):
    """Build a tiny app with the cache admin router and X-Admin-Key set."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"
    app = FastAPI()
    app.include_router(cache_admin_router)
    fake_svc = _FakeEmbeddingService()
    app.state.embedding_service = fake_svc
    return TestClient(app), fake_svc


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_flush_requires_x_admin_key() -> None:
    """Without X-Admin-Key, the endpoint MUST return 401."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"
    app = FastAPI()
    app.include_router(cache_admin_router)
    app.state.embedding_service = _FakeEmbeddingService()
    client = TestClient(app)
    r = client.post("/v1/admin/embedding-cache/flush")  # no header
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"


def test_flush_requires_x_admin_key_wrong_value() -> None:
    """Wrong X-Admin-Key value MUST also return 401."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"
    app = FastAPI()
    app.include_router(cache_admin_router)
    app.state.embedding_service = _FakeEmbeddingService()
    client = TestClient(app)
    r = client.post(
        "/v1/admin/embedding-cache/flush",
        headers={"X-Admin-Key": "wrong-key"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_flush_returns_cleared_count(
    client_with_admin_key: tuple[TestClient, _FakeEmbeddingService],
) -> None:
    """Authenticated POST returns status=ok + cleared=5 + size_after=0."""
    client, fake_svc = client_with_admin_key
    r = client.post(
        "/v1/admin/embedding-cache/flush",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["cleared"] == 5
    assert body["cache_size_after"] == 0
    assert body["model_version"].startswith("model.onnx=")


def test_flush_is_idempotent(
    client_with_admin_key: tuple[TestClient, _FakeEmbeddingService],
) -> None:
    """A second flush returns cleared=0 — flush is safe to call repeatedly."""
    client, _ = client_with_admin_key
    headers = {"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"}
    r1 = client.post("/v1/admin/embedding-cache/flush", headers=headers)
    r2 = client.post("/v1/admin/embedding-cache/flush", headers=headers)
    assert r1.json()["cleared"] == 5
    assert r2.json()["cleared"] == 0
    assert r2.json()["cache_size_after"] == 0


# ---------------------------------------------------------------------------
# Service unavailable path
# ---------------------------------------------------------------------------


def test_flush_returns_503_when_embedder_missing() -> None:
    """If app.state.embedding_service is None, endpoint MUST return 503."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"
    app = FastAPI()
    app.include_router(cache_admin_router)
    app.state.embedding_service = None
    client = TestClient(app)
    r = client.post(
        "/v1/admin/embedding-cache/flush",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert r.status_code == 503
    assert "EmbeddingService" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Real EmbeddingService integration (cache populated → flush clears it)
# ---------------------------------------------------------------------------


def test_flush_with_real_embedding_service(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: populate cache via encode(), then flush via the endpoint,
    verify cleared count matches and subsequent encode() hits the model
    again (cache miss)."""
    from ekrs_rag.core import config as cfg
    cfg.settings.ADMIN_KEY = "test-admin-key-32chars-aaaaaaaaaaaaaaaa"
    cfg.settings.PARSER_TOKEN = "test-parser-token-32chars-aaaaaaaaaaaaaaaa"
    (tmp_path / "model.onnx").write_bytes(b"x")
    sha = __import__("hashlib").sha256(b"x").hexdigest()
    (tmp_path / "bge-m3.sha256").write_text(f"{sha}  model.onnx\n")

    call_count = {"n": 0}

    def fake_encode(texts, return_dense=True, return_sparse=True):
        call_count["n"] += 1
        return {
            "dense_vecs": [[0.0] * 1024 for _ in texts],
            "lexical_weights": [{} for _ in texts],
        }

    mock_model = MagicMock()
    mock_model.encode.side_effect = fake_encode

    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        return_value=mock_model,
    ):
        from ekrs_rag.retrieval.embedding_service import EmbeddingService
        svc = EmbeddingService(model_dir=tmp_path)

    # Populate cache with 2 distinct texts.
    svc.encode(["alpha", "beta"])
    assert svc.cache_size() == 2

    # Wire into FastAPI app + flush via HTTP.
    app = FastAPI()
    app.include_router(cache_admin_router)
    app.state.embedding_service = svc
    client = TestClient(app)
    r = client.post(
        "/v1/admin/embedding-cache/flush",
        headers={"X-Admin-Key": "test-admin-key-32chars-aaaaaaaaaaaaaaaa"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cleared"] == 2
    assert body["cache_size_after"] == 0

    # Next encode() must invoke the model again (cache miss for both texts).
    # encode() batches both texts into a single model call, so we expect
    # call_count to grow by exactly 1 (not 2).
    n_before = call_count["n"]
    svc.encode(["alpha", "beta"])
    assert call_count["n"] == n_before + 1, (
        f"Cache was not flushed — encode() invoked model {call_count['n'] - n_before} times (expected 1)"
    )