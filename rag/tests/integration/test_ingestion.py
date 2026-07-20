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
    # Match pre-T3 behavior: settings.PARSER_TOKEN default is the test secret.
    # T3 moved auth to Depends(require_parser_token) which reads the env var;
    # without this, missing/invalid-token tests return 202 instead of 403.
    monkeypatch.setenv("PARSER_TOKEN", "change-me-to-a-secure-random-string-32chars")
    # T1: redirect SHARED_STORAGE_PATH to a tmpdir so lifespan's existence
    # check does not depend on the prod default /parsed_lib being mounted.
    # The module-level `settings` singleton was instantiated at import time,
    # so re-point the attribute directly (env-only patching wouldn't reach it).
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "SHARED_STORAGE_PATH", tmp_path)
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
        pipeline = IngestionPipeline(mock_qdrant, settings.SHARED_STORAGE_PATH)
        from ekrs_rag.api.routes.ingestion import (
            get_pipeline, get_redis_lock, get_task_repo,
        )
        app.dependency_overrides[get_pipeline] = lambda: pipeline
        app.dependency_overrides[get_redis_lock] = lambda: mock_redis_lock
        app.dependency_overrides[get_task_repo] = lambda: mock_task_repo

        with TestClient(app) as c:
            yield c


@pytest.fixture
def sample_jsonl():
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
    tmpdir = tempfile.mkdtemp()
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
            headers={"X-Parser-Token": "change-me-to-a-secure-random-string-32chars"},
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
            headers={"X-Parser-Token": "change-me-to-a-secure-random-string-32chars"},
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
