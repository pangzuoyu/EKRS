"""Unit tests for get_status FAILED-branch (Phase 13c T3).

Pre-13c: ``ingestion.py:599-606`` had:
    elif status in ("failed", "pending"):
        return IngestionStatus(..., status="pending", ...)
→ returned "pending" for failed rows (wrong).

Phase 13c T3 fix:
1. Literal enum on IngestionStatus.status (4 values)
2. mapper: failed → "failed"
3. ingestion.py split FAILED branch with mapper call
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ekrs_rag.api.routes.ingestion import router as ingestion_router


class _StubRepo:
    """Minimal TaskRepo stub returning a single row.

    Real TaskRepo.get_for_doc returns dict-like row; we follow the same
    contract that ``ingestion.py:589-594`` consumes.
    """

    def __init__(self, row: dict | None) -> None:
        self._row = row

    def get_for_doc(self, doc_hash: str):  # noqa: ARG002
        return self._row


def _build_app(repo: _StubRepo) -> FastAPI:
    app = FastAPI()
    app.include_router(ingestion_router)
    app.state.repo = repo
    return app


def _override_repo(repo: _StubRepo):
    """Build a Depends override function for get_task_repo."""
    from ekrs_rag.api.routes.ingestion import get_task_repo

    def _override():
        return repo

    return _override


class TestGetStatusFailedBranch:
    """Phase 13c T3 fix: FAILED row must return status='failed', NOT 'pending'."""

    def test_failed_row_returns_failed_status(self):
        """Pre-13c bug: failed row returned 'pending'. T3 fix returns 'failed'."""
        repo = _StubRepo(
            row={
                "doc_hash": "doc_failed_123",
                "status": "FAILED",  # TaskRepo internal uppercase
                "version": 1,
                "failure_reason": "qdrant_write_failed",
            }
        )
        app = _build_app(repo)
        app.dependency_overrides[_override_repo(repo).__name__] = lambda: repo
        # Easier path: build full dep override
        from ekrs_rag.api.routes.ingestion import get_task_repo

        app.dependency_overrides[get_task_repo] = lambda: repo

        with TestClient(app) as client:
            r = client.get("/v1/ingestion/status/doc_failed_123")

        assert r.status_code == 200
        body = r.json()
        # KEY ASSERTION — was 'pending' pre-13c:
        assert body["status"] == "failed", (
            f"Expected status='failed' for FAILED row, got {body['status']!r}"
        )

    def test_queued_row_returns_pending_status(self):
        """queued (internal) → pending (external). Mapper function applies."""
        repo = _StubRepo(
            row={
                "doc_hash": "doc_queued_456",
                "status": "QUEUED",
                "version": 1,
            }
        )
        from ekrs_rag.api.routes.ingestion import get_task_repo

        app = _build_app(repo)
        app.dependency_overrides[get_task_repo] = lambda: repo

        with TestClient(app) as client:
            r = client.get("/v1/ingestion/status/doc_queued_456")

        assert r.status_code == 200
        assert r.json()["status"] == "pending"

    def test_running_row_returns_processing_status(self):
        """running (internal) → processing (external)."""
        repo = _StubRepo(
            row={
                "doc_hash": "doc_running_789",
                "status": "RUNNING",
                "version": 1,
            }
        )
        from ekrs_rag.api.routes.ingestion import get_task_repo

        app = _build_app(repo)
        app.dependency_overrides[get_task_repo] = lambda: repo

        with TestClient(app) as client:
            r = client.get("/v1/ingestion/status/doc_running_789")

        assert r.status_code == 200
        assert r.json()["status"] == "processing"