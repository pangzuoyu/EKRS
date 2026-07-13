"""Integration tests for Ingestion Replay endpoint (POST /v1/ingestion/replay).

Spec: Phase 5 — re-runs JSONL → parse → chunk → upsert for an already-indexed
document by request_id. Bypasses notify idempotency + parser callback, but
re-uses the sha256-validated source pointer stored on the task row at notify
time.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.api.routes.ingestion import router as ingestion_router
from ekrs_rag.storage.task_repo import TaskRepo


class MockPipeline:
    """Test double for IngestionPipeline.replay()."""

    def __init__(self, chunks: int = 7):
        self._chunks = chunks
        self.replay_calls: list[tuple[str, str, int]] = []

    async def replay(self, jsonl_path, doc_hash, version):
        self.replay_calls.append((str(jsonl_path), doc_hash, version))
        return self._chunks


def test_ingestion_replay_route_uses_dependency_overrides():
    """Ingestion /replay route gets repo + pipeline via Depends."""
    from unittest.mock import MagicMock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ekrs_rag.api.routes.ingestion import router, get_task_repo, get_pipeline

    sentinel_repo = MagicMock()
    sentinel_repo.get.return_value = None  # task unknown → 404
    sentinel_pipeline = MagicMock()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_task_repo] = lambda: sentinel_repo
    app.dependency_overrides[get_pipeline] = lambda: sentinel_pipeline

    client = TestClient(app)
    resp = client.post(
        "/v1/ingestion/replay",
        json={"request_id": "x", "replayed_by": "test"},
        headers={"X-Parser-Token": "test-token"},
    )
    assert resp.status_code == 404
    sentinel_repo.get.assert_called_with("x")


def _build_app(repo: TaskRepo, pipeline=None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(ingestion_router)
    app.state.task_repo = repo
    if pipeline is not None:
        from ekrs_rag.api.routes.ingestion import get_pipeline
        app.dependency_overrides[get_pipeline] = lambda: pipeline
    return app


def test_replay_completed_task_succeeds(tmp_path):
    """Happy path: COMPLETED task + matching sha256 → 200, chunks counted."""
    # Make sure PARSER_TOKEN is empty so auth is a no-op
    os.environ["PARSER_TOKEN"] = ""

    jsonl = tmp_path / "doc.jsonl"
    jsonl.write_text('{"doc_id": "d1", "blocks": []}\n')
    expected_sha = hashlib.sha256(jsonl.read_bytes()).hexdigest()

    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-replay-1"
    repo.try_insert_with_source(rid, "d1", str(jsonl), expected_sha)
    repo.mark_status(rid, "COMPLETED")

    mock = MockPipeline(chunks=5)
    app = _build_app(repo, pipeline=mock)
    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": rid,
        "replayed_by": "ops-test",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == rid
    assert body["status"] == "completed"
    assert body["chunks_written"] == 5
    assert body["duration_ms"] >= 0
    # Mock was actually called with the right args
    assert len(mock.replay_calls) == 1
    assert mock.replay_calls[0][1] == "d1"
    repo.close()


def test_replay_pre_phase5_task_returns_409(tmp_path):
    """Task with NULL source_path is pre-Phase 5 data → 409 pre_phase5."""
    os.environ["PARSER_TOKEN"] = ""
    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-pre-phase5"
    repo.try_insert(rid, "d-old")  # no source_path/payload_sha256
    repo.mark_status(rid, "COMPLETED")

    app = _build_app(repo, pipeline=MockPipeline())
    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": rid,
        "replayed_by": "ops",
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "pre_phase5"
    repo.close()


def test_replay_in_flight_task_returns_409(tmp_path):
    """Tasks still PENDING cannot be replayed → 409 in_flight."""
    os.environ["PARSER_TOKEN"] = ""
    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-inflight"
    repo.try_insert_with_source(rid, "d", "/some/path.jsonl", "h")
    # Leave status as PENDING (default from try_insert)

    app = _build_app(repo, pipeline=MockPipeline())
    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": rid,
        "replayed_by": "ops",
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "in_flight"
    repo.close()


def test_replay_unknown_request_id_returns_404(tmp_path):
    """request_id not in tasks table → 404."""
    os.environ["PARSER_TOKEN"] = ""
    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()

    app = _build_app(repo, pipeline=MockPipeline())
    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": "nonexistent-request-id",
        "replayed_by": "ops",
    })
    assert resp.status_code == 404
    repo.close()


def test_replay_sha256_mismatch_returns_409(tmp_path):
    """If JSONL content changes since notify, sha256 check rejects → 409."""
    os.environ["PARSER_TOKEN"] = ""
    jsonl = tmp_path / "doc.jsonl"
    jsonl.write_text('{"doc_id": "d1", "blocks": []}\n')

    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-sha-mismatch"
    # Insert with wrong sha — what notify wrote at notify time
    repo.try_insert_with_source(rid, "d1", str(jsonl), "wrong-hash-expected-at-notify")
    repo.mark_status(rid, "COMPLETED")

    app = _build_app(repo, pipeline=MockPipeline())
    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": rid,
        "replayed_by": "ops",
    })
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "sha256_mismatch"
    repo.close()
