# rag/tests/unit/api/test_route_dependencies.py
"""Contract tests for route dependency helpers added in Phase 5.5 E.

Each dependency is verified in isolation (no FastAPI app boot) by directly
invoking it with a mock request whose app.state is a real State() instance
configured to None. (P1 fix from gstack-plan-eng-review: MagicMock auto-
creates `state.X` as a truthy attribute, defeating the 503 check.)
"""
import pytest
from starlette.datastructures import State
from fastapi import HTTPException
from unittest.mock import MagicMock

from ekrs_rag.api.routes.constraints import get_retriever, get_audit_index
from ekrs_rag.api.routes.ingestion import (
    get_pipeline, get_redis_lock, get_task_repo,
)


def _mock_request(state_overrides: dict | None = None):
    """Build a mock Request whose app.state is a real State() (no auto-attr)."""
    req = MagicMock()
    req.app.state = State()  # real State; missing attrs raise AttributeError
    if state_overrides:
        for k, v in state_overrides.items():
            setattr(req.app.state, k, v)
    return req


def test_get_retriever_raises_503_when_state_unset():
    req = _mock_request()
    with pytest.raises(HTTPException) as exc:
        get_retriever(req)
    assert exc.value.status_code == 503
    assert "retriever" in exc.value.detail


def test_get_audit_index_returns_none_optional():
    req = _mock_request()
    # Optional dep returns None, does NOT raise
    assert get_audit_index(req) is None


def test_get_pipeline_raises_503_when_state_unset():
    req = _mock_request()
    with pytest.raises(HTTPException) as exc:
        get_pipeline(req)
    assert exc.value.status_code == 503
    assert "pipeline" in exc.value.detail


def test_get_redis_lock_raises_503_when_state_unset():
    req = _mock_request()
    with pytest.raises(HTTPException) as exc:
        get_redis_lock(req)
    assert exc.value.status_code == 503
    assert "redis" in exc.value.detail.lower()


def test_get_task_repo_raises_503_when_state_unset():
    req = _mock_request()
    with pytest.raises(HTTPException) as exc:
        get_task_repo(req)
    assert exc.value.status_code == 503
    assert "task repo" in exc.value.detail.lower()
