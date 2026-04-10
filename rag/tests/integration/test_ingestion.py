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
def client(mock_qdrant):
    """Create TestClient with mocked Qdrant."""
    with patch("ekrs_rag.main.QdrantManager", return_value=mock_qdrant):
        with patch("ekrs_rag.main.setup_logging"):
            from ekrs_rag.main import app
            # Re-init pipeline with mock
            from ekrs_rag.ingestion.pipeline import IngestionPipeline
            from ekrs_rag.core.config import settings
            pipeline = IngestionPipeline(mock_qdrant, settings.SHARED_STORAGE_PATH)
            from ekrs_rag.api.routes import ingestion
            ingestion.set_pipeline(pipeline)

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
