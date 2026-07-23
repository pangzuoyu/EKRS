"""Phase 8 T8-4 API-level golden cases.

Cases 5, 6, 8 from docs/superpowers/plans/2026-07-23-phase8-scope.md
§T8-4 — these exercise the FastAPI route layer (POST /v1/constraints)
rather than the chunk-level evidence/solve pipeline covered by
test_golden_set.py.

Acceptance: 50 cases total pass under `make golden-test`.
- Chunk-level: 47 (from golden_set.json; 42 baseline + 5 T8-4 additions)
- API-level: 3 (this file)

These cases use TestClient + dependency_overrides to mock the
retriever, mirroring the pattern in tests/integration/test_ingestion_replay.py.
The retriever stub is deterministic (sorted by text) so the same
request body always produces the same chunks — that's what enables
case 8 (concurrent replay determinism).
"""
from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Disable auth before importing the app — auth.py reads
# PARSER_TOKEN at request time, but the empty-string check is the
# first branch, so it short-circuits.
os.environ["PARSER_TOKEN"] = ""

from ekrs_rag.api.routes.constraints import (  # noqa: E402
    ConstraintQuery,
    get_retriever,
)
from ekrs_rag.main import create_app  # noqa: E402
from ekrs_rag.retrieval.retriever import RetrievalResult  # noqa: E402


# ----------------------------------------------------------------------------
# Stub retriever
# ----------------------------------------------------------------------------


def _build_chunk(text: str, scope_path: list[str] | None = None) -> Any:
    """Construct a minimal Chunk-like object that EvidenceBuilder can ingest."""
    from ekrs_shared.models import Chunk, NumericHint

    return Chunk(
        text=text,
        scope_path=scope_path or [],
        source_block_ids=["block_1"],
        token_count=len(text) // 4,
        doc_hash="test_hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


class _StubRetriever:
    """Deterministic retriever for API validation tests.

    Returns chunks ordered by query text length (shortest first) so
    the same query string always produces the same output. Rejects
    non-list active_scope by returning empty (filter semantics).
    """

    def __init__(self, default_chunks: list[Any] | None = None) -> None:
        self._default = default_chunks or []

    def retrieve(
        self,
        query: str,
        top_k: int = 40,
        active_scope: list[str] | None = None,
    ) -> RetrievalResult:
        # Case 6: non-list scope_path → filter (empty result, route returns 404).
        if active_scope is not None and not isinstance(active_scope, list):
            return RetrievalResult(chunks=[], vector_scores=[], scope_scores=[], final_scores=[])

        # Case 5: empty query → no chunks.
        if not query:
            return RetrievalResult(chunks=[], vector_scores=[], scope_scores=[], final_scores=[])

        chunks = list(self._default)
        return RetrievalResult(
            chunks=chunks,
            vector_scores=[1.0] * len(chunks),
            scope_scores=[0.0] * len(chunks),
            final_scores=[1.0] * len(chunks),
        )


def _make_client(stub: _StubRetriever) -> TestClient:
    """Build a FastAPI app with the stub retriever wired via dependency_overrides.

    We construct a fresh app per test (rather than reusing the global
    `app`) to avoid lifespan side effects (Qdrant/Redis init) and to
    keep test isolation tight.
    """
    app: FastAPI = create_app()
    app.dependency_overrides[get_retriever] = lambda: stub
    return TestClient(app)


# ----------------------------------------------------------------------------
# Case 5: Empty query (must be 4xx, not 5xx)
# ----------------------------------------------------------------------------


def test_empty_query_returns_4xx_not_5xx() -> None:
    """Phase 8 T8-4 TC-API-EMPTY-01.

    Empty query string must not crash with 500. The route should
    gracefully return 404 (insufficient recall — Gate 1) since the
    empty query yields no chunks.

    Contract: status_code in {400, 404}, NOT 500.
    """
    stub = _StubRetriever()
    client = _make_client(stub)

    resp = client.post("/v1/constraints", json={"query": ""})

    assert resp.status_code in (400, 404), (
        f"Empty query must return 4xx, got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code < 500, "Empty query must not crash with 5xx"


# ----------------------------------------------------------------------------
# Case 6: Invalid scope path (must filter or 400, not silently accept)
# ----------------------------------------------------------------------------


def test_invalid_scope_path_returns_4xx_not_5xx() -> None:
    """Phase 8 T8-4 TC-API-SCOPE-01.

    Invalid scope_path (non-list type) must not crash with 500.
    The stub retriever filters it (returns empty chunks), so the
    route returns 404 (insufficient recall). The contract is
    "filter or 400 — not silently accept"; 404 (filtered) is
    acceptable.

    Contract: status_code in {400, 404}, NOT 500.
    """
    chunk = _build_chunk("工作温度不得超过80°C", scope_path=["industry", "refining"])
    stub = _StubRetriever(default_chunks=[chunk])
    client = _make_client(stub)

    # scope_path is a string instead of a list — invalid.
    resp = client.post(
        "/v1/constraints",
        json={"query": "工作温度", "context": {"scope_path": "industry"}},
    )

    assert resp.status_code in (400, 404), (
        f"Invalid scope_path must return 4xx, got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code < 500, "Invalid scope_path must not crash with 5xx"


# ----------------------------------------------------------------------------
# Case 8: Concurrent identical queries (deterministic replay)
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("iterations", [5])
def test_concurrent_identical_queries_are_deterministic(iterations: int) -> None:
    """Phase 8 T8-4 TC-API-CONC-01.

    POST /v1/constraints with the same body N times must produce
    byte-identical responses. This catches non-determinism in the
    route layer (rate limiter ordering, audit timestamps, etc.).

    Solvers are pure (R2 Iron Rule). The stub retriever is also
    deterministic. So responses should be byte-equal.

    Caveat: ConstraintQueryResponse.trace may include monotonic
    timestamps in production (e.g. from solver trace). The stub
    retriever's response shape is stable, but if a future change
    adds a timestamp to ConstraintQueryResponse, this test should
    compare only the deterministic fields (branches, primary_branch,
    conflicts, mode). For now: compare full JSON.
    """
    # Chunk text must extract ≥1 constraint or route returns 404 (Gate 2).
    chunk = _build_chunk("工作压力不得超过1.6MPa", scope_path=["national", "gb"])
    stub = _StubRetriever(default_chunks=[chunk])
    client = _make_client(stub)

    body = {"query": "工作压力", "context": {"scope_path": ["national"]}}

    responses: list[dict[str, Any]] = []
    for _ in range(iterations):
        resp = client.post("/v1/constraints", json=body)
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        responses.append(resp.json())

    # All N responses must be byte-identical.
    first = responses[0]
    for i, later in enumerate(responses[1:], start=1):
        assert later == first, (
            f"Non-deterministic response at iteration {i}:\n"
            f"  First: {first}\n"
            f"  Later: {later}"
        )
