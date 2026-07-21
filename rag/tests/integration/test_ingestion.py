"""Integration tests for ingestion API endpoints.

Uses httpx TestClient with mocked Qdrant.
"""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ekrs_shared.models import IngestionStatus


PARSER_TOKEN = "x" * 32  # T3: real secret; placeholder literal now rejected


@pytest.fixture
def mock_qdrant():
    """Mock QdrantManager for all tests."""
    mock = MagicMock()
    mock.ensure_collection.return_value = None
    mock.upsert_chunks.return_value = 5
    mock.get_ingestion_status.return_value = IngestionStatus(
        status="success", chunks_indexed=5, version=1,
    )
    mock.delete_old_versions.return_value = 0
    return mock


@pytest.fixture
def client(mock_qdrant, tmp_path, monkeypatch):
    """Create TestClient with mocked Qdrant + Phase 4 components."""
    # T3: lifespan startup fails on empty/short PARSER_TOKEN, so we have to
    # mutate the singleton (env var alone won't help — the singleton was
    # already constructed at module-import time).
    from ekrs_rag.core.config import settings as _settings

    monkeypatch.setattr(_settings, "PARSER_TOKEN", PARSER_TOKEN)
    monkeypatch.setenv("PARSER_TOKEN", PARSER_TOKEN)
    # SHARED_STORAGE_PATH is redirected to tmp_path by the integration-level
    # autouse fixture in tests/integration/conftest.py — no need to repeat it
    # here.
    mock_task_repo = MagicMock()
    mock_task_repo.init.return_value = None
    mock_task_repo.try_insert.return_value = True
    mock_doc_repo = MagicMock()
    mock_doc_repo.init.return_value = None
    mock_redis_lock = MagicMock()
    mock_redis_lock.acquire = AsyncMock(return_value="lock-token")
    mock_redis_lock.release = AsyncMock(return_value=True)
    mock_redis = MagicMock()
    mock_scanner = MagicMock()
    mock_scanner.scan = AsyncMock(return_value=0)
    with patch("ekrs_rag.main.QdrantManager", return_value=mock_qdrant), \
         patch("ekrs_rag.main.setup_logging"), \
         patch("ekrs_rag.main.TaskRepo", return_value=mock_task_repo), \
         patch("ekrs_rag.main.DocumentRepo", return_value=mock_doc_repo), \
         patch("ekrs_rag.main.aioredis.from_url", return_value=mock_redis), \
         patch("ekrs_rag.main.RedisLock", return_value=mock_redis_lock), \
         patch("ekrs_rag.main.CompensationScanner", return_value=mock_scanner):
        from ekrs_rag.main import app
        # Re-init pipeline with mock
        from ekrs_rag.ingestion.pipeline import IngestionPipeline
        from ekrs_rag.core.config import settings
        pipeline = IngestionPipeline(
            mock_qdrant,
            settings.SHARED_STORAGE_PATH,
            parser_token="x" * 32,
        )
        from ekrs_rag.api.routes.ingestion import (
            get_pipeline, get_redis_lock, get_task_repo,
        )
        app.dependency_overrides[get_pipeline] = lambda: pipeline
        app.dependency_overrides[get_redis_lock] = lambda: mock_redis_lock
        app.dependency_overrides[get_task_repo] = lambda: mock_task_repo

        with TestClient(app) as c:
            yield c


@pytest.fixture
def sample_jsonl(tmp_path):
    """Create a temporary JSONL file with sample data."""
    blocks = [
        {
            "doc_id": "test_doc",
            "block_id": f"b{i:03d}",
            "type": "text",
            "content": {"md_preview": f"text block {i}", "raw": f"raw {i}"},
            "metadata": {"page_number": 1},
        }
        for i in range(3)
    ]
    tmpdir = tempfile.mkdtemp(dir=tmp_path)
    output_dir = os.path.join(tmpdir, "data")
    os.makedirs(output_dir, exist_ok=True)
    jsonl_path = os.path.join(output_dir, "data.jsonl")

    with open(jsonl_path, "w") as f:
        for block in blocks:
            f.write(json.dumps(block, ensure_ascii=False) + "\n")

    return output_dir


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.text == "ok"


class TestIngestionNotify:
    def test_missing_token(self, client, sample_jsonl):
        resp = client.post("/v1/ingestion/notify", json={
            "doc_hash": "abc",
            "version": 1,
            "output_path": sample_jsonl,
        })
        assert resp.status_code == 403

    def test_invalid_token(self, client, sample_jsonl):
        resp = client.post(
            "/v1/ingestion/notify",
            json={"doc_hash": "abc", "version": 1, "output_path": sample_jsonl},
            headers={"X-Parser-Token": "wrong-token"},
        )
        assert resp.status_code == 403

    def test_valid_notification(self, client, sample_jsonl):
        """Valid notification returns 202 and queues ingestion."""
        resp = client.post(
            "/v1/ingestion/notify",
            json={
                "doc_hash": "test_doc_hash",
                "version": 1,
                "output_path": sample_jsonl,
            },
            headers={"X-Parser-Token": PARSER_TOKEN},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert data["doc_hash"] == "test_doc_hash"

    def test_notification_with_callback(self, client, sample_jsonl):
        """Notification with callback_url returns 202 (callback happens async)."""
        # No callback_url — the background task will log a warning but not fail
        resp = client.post(
            "/v1/ingestion/notify",
            json={
                "doc_hash": "abc_cb",
                "version": 1,
                "output_path": sample_jsonl,
            },
            headers={"X-Parser-Token": PARSER_TOKEN},
        )
        assert resp.status_code == 202


class TestIngestionStatus:
    def test_known_doc(self, client, mock_qdrant):
        resp = client.get("/v1/ingestion/status/test_doc_hash")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["chunks_indexed"] == 5

    def test_unknown_doc(self, client, mock_qdrant):
        mock_qdrant.get_ingestion_status.return_value = None
        resp = client.get("/v1/ingestion/status/nonexistent")
        assert resp.status_code == 404


@pytest.mark.integration
def test_notify_rejects_output_path_outside_storage_root(client, tmp_path):
    """An output_path that escapes SHARED_STORAGE_PATH must 400."""
    outside = tmp_path.parent / "evil.txt"
    resp = client.post(
        "/v1/ingestion/notify",
        headers={"X-Parser-Token": PARSER_TOKEN},
        json={
            "doc_hash": "abc123",
            "version": 1,
            "output_path": str(outside),
            "callback_url": "",
        },
    )
    assert resp.status_code == 400
    assert "SHARED_STORAGE_PATH" in resp.json()["detail"]


@pytest.mark.integration
def test_notify_rejects_relative_traversal(client, tmp_path):
    """output_path with .. segments must 400."""
    base = tmp_path.resolve()
    rel = f"{base}/../../../etc"
    resp = client.post(
        "/v1/ingestion/notify",
        headers={"X-Parser-Token": PARSER_TOKEN},
        json={
            "doc_hash": "abc123",
            "version": 1,
            "output_path": rel,
            "callback_url": "",
        },
    )
    assert resp.status_code == 400
