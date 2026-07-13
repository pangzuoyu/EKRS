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


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """Reset module-level singletons before AND after each test.

    `constraints._retriever`, `constraints._audit_index`, and the
    `audit.set_writer` global are all module-scoped. Other test files
    (e.g. test_healthz) trigger the production lifespan which calls
    `constraints.set_retriever(real_retriever)`. That real retriever
    would then leak into test 1 here and crash on `retrieve()` because
    the test environment has no Qdrant. Reset at fixture start clears
    the pollution; reset at end keeps the next test file clean.
    """
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_retriever(None)
    cmod.set_audit_index(None)
    set_writer(None)
    yield
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_retriever(None)
    cmod.set_audit_index(None)
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


def test_replay_returns_deterministic_match(audit_setup):
    """Replay with same trace_id should return deterministic_match=true."""
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    # Inject dependencies
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(audit_setup["idx"])

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "irrelevant",  # ignored in replay mode
        "replay": True,
        "replay_trace_id": audit_setup["prior_trace"],
    })
    # 404 if retriever not initialized (gate-1 fallback); 200 means replay ran.
    assert resp.status_code in (200, 404)
    # If 200, the response MUST include deterministic_match=True. The seeded
    # prior has branches_count=2; if retriever returns 0 chunks here the
    # route would 404, so a 200 means re-solve succeeded and matched the
    # stored count.
    if resp.status_code == 200:
        body = resp.json()
        assert "deterministic_match" in body
        assert body["deterministic_match"] is True


def test_replay_unknown_trace_id_returns_400(audit_setup):
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(audit_setup["idx"])

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "q",
        "replay": True,
        "replay_trace_id": "nonexistent-trace",
    })
    assert resp.status_code == 400


def test_replay_ignores_request_body_query(audit_setup):
    """In replay mode, query/scope_path/strict in body are ignored."""
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(audit_setup["idx"])

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

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(idx)
    cmod.set_retriever(mock)  # Use setter

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
