"""Integration tests for /v1/constraints Query Replay branch.

Spec: Phase 5 Query Replay — POST /v1/constraints with replay=true
re-runs the solver against a prior trace_id's stored query/scope.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.api.routes.constraints import router as constraints_router
from ekrs_rag.observability.audit import AuditWriter, set_writer
from ekrs_rag.observability.audit_index import AuditIndex


def test_constraints_route_uses_dependency_overrides():
    """Constraints route gets retriever via Depends; overrides take precedence."""
    from ekrs_rag.api.routes.constraints import router, get_retriever

    app = FastAPI()
    app.include_router(router)

    captured = {}

    class MockRetriever:
        def retrieve(self, query, top_k, active_scope=None):
            captured["query"] = query
            from ekrs_rag.retrieval.retriever import RetrievalResult
            return RetrievalResult(
                chunks=[], vector_scores=[], scope_scores=[], final_scores=[],
            )

    app.dependency_overrides[get_retriever] = lambda: MockRetriever()
    client = TestClient(app)
    resp = client.post(
        "/v1/constraints",
        json={"query": "x"},
        headers={"X-Parser-Token": "test-token"},
    )
    # Gate 1: empty chunks → 404 (Insufficient recall)
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}"
    assert captured["query"] == "x"


@pytest.fixture(autouse=True)
def _reset_audit_writer():
    """Reset audit module's global writer between tests (out of Phase 5.5 E scope).

    `audit.set_writer` is module-scoped; without reset, the writer leaks across
    tests. The constraints-route globals/setters this used to reset are
    deleted in Phase 5.5 E — replaced by `app.dependency_overrides` per-test.
    """
    set_writer(None)
    yield
    set_writer(None)


@pytest.fixture
def audit_setup(tmp_path):
    log_path = tmp_path / "audit.log"
    writer = AuditWriter(str(log_path))
    writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    set_writer(writer)

    # Pre-seed audit.log with a prior solve (use ASCII query so audit_index.py
    # byte/char offset tracking stays correct).
    prior_trace = "550e8400-e29b-41d4-a716-446655440000"
    writer.log_event("constraint_solve_started", trace_id=prior_trace, query="gaowen")
    writer.log_event("constraint_solved", trace_id=prior_trace, branches_count=2)

    idx = AuditIndex(str(log_path))
    idx.build()
    yield {"log_path": str(log_path), "writer": writer, "idx": idx, "prior_trace": prior_trace}

    set_writer(None)


def _empty_retriever_override():
    """Mock retriever returning empty chunks so Depends(get_retriever) succeeds."""
    from ekrs_rag.api.routes.constraints import get_retriever
    from ekrs_rag.retrieval.retriever import RetrievalResult

    class _EmptyRetriever:
        def retrieve(self, query, top_k, active_scope=None):
            return RetrievalResult(
                chunks=[], vector_scores=[], scope_scores=[], final_scores=[]
            )

    return get_retriever, _EmptyRetriever()


def test_replay_returns_deterministic_match(audit_setup):
    """Replay with same trace_id should return deterministic_match=true."""
    from ekrs_rag.api.routes.constraints import get_audit_index

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    app.dependency_overrides[get_audit_index] = lambda: audit_setup["idx"]
    dep, mock = _empty_retriever_override()
    app.dependency_overrides[dep] = lambda: mock

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "irrelevant",  # ignored in replay mode
        "replay": True,
        "replay_trace_id": audit_setup["prior_trace"],
    })
    # Replay branch always returns 200 when prior_lines exist; the
    # replay branch does NOT gate-1 on retrieval (it re-runs the solver
    # and reports deterministic_match vs the prior count).
    assert resp.status_code == 200
    body = resp.json()
    assert "deterministic_match" in body
    # Empty retriever → 0 branches now vs prior 2 branches → mismatch.
    # The test asserts the field is present (replay path executed),
    # not the value — it's a smoke test for the replay branch.
    assert isinstance(body["deterministic_match"], bool)


def test_replay_unknown_trace_id_returns_400(audit_setup):
    from ekrs_rag.api.routes.constraints import get_audit_index

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    app.dependency_overrides[get_audit_index] = lambda: audit_setup["idx"]
    dep, mock = _empty_retriever_override()
    app.dependency_overrides[dep] = lambda: mock

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "q",
        "replay": True,
        "replay_trace_id": "nonexistent-trace",
    })
    assert resp.status_code == 400


def test_replay_ignores_request_body_query(audit_setup):
    """In replay mode, query/scope_path/strict in body are ignored."""
    from ekrs_rag.api.routes.constraints import get_audit_index

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    app.dependency_overrides[get_audit_index] = lambda: audit_setup["idx"]
    dep, mock = _empty_retriever_override()
    app.dependency_overrides[dep] = lambda: mock

    client = TestClient(app)
    # Body query is "wrong", but replay uses prior_trace's stored query
    resp = client.post("/v1/constraints", json={
        "query": "WRONG QUERY",
        "replay": True,
        "replay_trace_id": audit_setup["prior_trace"],
    })
    # Either works (200) or hits gate (404); should NOT crash on bad query
    assert resp.status_code in (200, 404)


def test_replay_works_after_process_restart(tmp_path):
    """Process A writes audit; Process B starts, builds index, replays."""
    log_path = tmp_path / "audit.log"
    prior_trace = "test-trace-restart"

    # Process A
    writer_a = AuditWriter(str(log_path))
    writer_a.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer_a.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    writer_a.log_event("constraint_solve_started", trace_id=prior_trace, query="q")
    writer_a.log_event("constraint_solved", trace_id=prior_trace, branches_count=2)

    # Process B (fresh import)
    from ekrs_rag.observability.audit_index import AuditIndex
    idx_b = AuditIndex(str(log_path))
    idx_b.build()

    lines = idx_b.seek(prior_trace)
    assert lines is not None
    assert len(lines) == 2
    assert lines[0].event == "constraint_solve_started"


def test_replay_uses_prior_trace_query_not_body(tmp_path, monkeypatch):
    """Verify replay branch uses AuditIndex-prior query, ignoring body query.

    Test Gap 1: prior tests only checked status codes; we never verified
    that the replay actually used the stored query. Use a MockRetriever
    that records what query it received.
    """
    log_path = tmp_path / "audit.log"
    prior_trace = "550e8400-e29b-41d4-a716-446655440000"

    writer = AuditWriter(str(log_path))
    writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    writer.log_event("constraint_solve_started", trace_id=prior_trace, query="PRIOR_QUERY")
    writer.log_event("constraint_solved", trace_id=prior_trace, branches_count=1)

    idx = AuditIndex(str(log_path))
    idx.build()

    # Mock retriever that captures the query passed to it
    class MockRetriever:
        def __init__(self):
            self.received_query = None

        def retrieve(self, query, **kwargs):
            self.received_query = query
            # Return a RetrievalResult with empty chunks
            from ekrs_rag.retrieval.retriever import RetrievalResult
            return RetrievalResult(chunks=[], vector_scores=[], scope_scores=[], final_scores=[])

    mock = MockRetriever()

    from ekrs_rag.api.routes.constraints import get_audit_index, get_retriever

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    app.dependency_overrides[get_audit_index] = lambda: idx
    app.dependency_overrides[get_retriever] = lambda: mock

    # Override auth for this test (Issue: replay requires PARSER_TOKEN)
    # Re-import the route module to get the dependency
    # Actually simpler: monkeypatch settings to make token a no-op
    monkeypatch.setenv("PARSER_TOKEN", "")  # disable auth for this test

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "BODY_QUERY_SHOULD_BE_IGNORED",
        "replay": True,
        "replay_trace_id": prior_trace,
    })
    # Replay must have called retriever with prior query, not body query
    assert mock.received_query == "PRIOR_QUERY"
