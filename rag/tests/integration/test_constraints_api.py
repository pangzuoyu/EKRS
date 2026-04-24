"""Integration tests for /v1/constraints API endpoint.

TC_STRICT_01: strict=true with inferred constraint -> 400 missing_context
TC_HARD_CONFLICT_01: conflicting constraints -> 409 conflict
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.retrieval.retriever import RetrievalResult

if TYPE_CHECKING:
    from ekrs_shared.models import Chunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_chunk(text: str, scope_path: list[str] | None = None) -> "Chunk":
    """Create a Chunk with empty numeric_hints (filled in by tests)."""
    from ekrs_shared.models import Chunk
    return Chunk(
        text=text,
        scope_path=scope_path or ["national", "GB"],
        source_block_ids=["b1"],
        token_count=10,
        doc_hash="test-hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


class MockRetriever:
    """Standalone mock retriever that always returns predefined chunks."""

    def __init__(self, chunks: list["Chunk"]):
        self._chunks = chunks

    def retrieve(self, query: str, top_k: int = 40, active_scope=None) -> RetrievalResult:
        return RetrievalResult(
            chunks=self._chunks,
            vector_scores=[1.0] * len(self._chunks),
            scope_scores=[1.0] * len(self._chunks),
            final_scores=[1.0] * len(self._chunks),
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _app():
    """Minimal FastAPI app with only the constraints router."""
    from fastapi import FastAPI
    from ekrs_rag.api.routes import constraints
    app = FastAPI()
    app.include_router(constraints.router)
    return app


@pytest.fixture
def client(_app):
    """TestClient bound to the shared minimal app."""
    with TestClient(_app) as c:
        yield c


def _inject_retriever(_app, retriever):
    """Helper to inject retriever into app.state (must be called before request)."""
    _app.state.retriever = retriever


# ---- Mock retrievers -------------------------------------------------------


@pytest.fixture
def mock_retriever_normal():
    """Retriever returning a chunk with a temperature <=80°C constraint."""
    from ekrs_shared.models import Chunk, NumericHint
    chunk = Chunk(
        text="温度不得超过80°C",
        scope_path=["national", "GB"],
        source_block_ids=["b1"],
        token_count=10,
        doc_hash="test-normal",
        version=1,
        page_numbers=[1],
        numeric_hints=[
            NumericHint(
                parameter_hint="temperature",
                value=80.0,
                unit="°C",
                span=(5, 9),
                source_text="80°C",
                block_id="b1",
                page_num=1,
                scope_path=["national", "GB"],
            )
        ],
    )
    return MockRetriever([chunk])


@pytest.fixture
def mock_retriever_conflicting():
    """Retriever returning two chunks that produce conflicting temperature bounds."""
    from ekrs_shared.models import Chunk, NumericHint
    # Chunk 1: upper <= 300K = 26.85°C
    chunk1 = Chunk(
        text="温度不得超过300K",
        scope_path=["national", "GB"],
        source_block_ids=["b1"],
        token_count=10,
        doc_hash="test-conflict1",
        version=1,
        page_numbers=[1],
        numeric_hints=[
            NumericHint(
                parameter_hint="temperature",
                value=300.0,
                unit="K",
                span=(5, 9),
                source_text="300K",
                block_id="b1",
                page_num=1,
                scope_path=["national", "GB"],
            )
        ],
    )
    # Chunk 2: lower >= 30°C
    chunk2 = Chunk(
        text="温度不低于30°C",
        scope_path=["national", "GB"],
        source_block_ids=["b2"],
        token_count=10,
        doc_hash="test-conflict2",
        version=1,
        page_numbers=[1],
        numeric_hints=[
            NumericHint(
                parameter_hint="temperature",
                value=30.0,
                unit="°C",
                span=(5, 9),
                source_text="30°C",
                block_id="b2",
                page_num=1,
                scope_path=["national", "GB"],
            )
        ],
    )
    return MockRetriever([chunk1, chunk2])


@pytest.fixture
def mock_retriever_empty():
    """Retriever returning zero chunks (triggers Gate 1)."""
    return MockRetriever([])


@pytest.fixture
def mock_retriever_no_hints():
    """Retriever returning a chunk with no numeric hints (triggers Gate 2)."""
    return MockRetriever([make_chunk("这是一段没有数字约束的文本", scope_path=["national"])])


# ---------------------------------------------------------------------------
# TC_STRICT_01: strict mode rejects inferred
# ---------------------------------------------------------------------------


class TestStrictMode:
    """TC_STRICT_01: strict=true forbids inferred constraints with 400."""

    def test_strict_rejects_inferred_constraint(self, client, mock_retriever_normal, _app):
        """strict=true + inferred constraint -> 400 missing_context."""
        from ekrs_shared.models import ConstraintV2

        inferred_constraint = ConstraintV2(
            parameter="temperature",
            value_type="interval",
            interval={"lower": None, "upper": 80.0, "lower_inclusive": True, "upper_inclusive": True},
            unit="°C",
            inferred=True,
            priority={"explicit_level": 100, "recency_score": 0.0, "authority_score": 0.0},
            scope_path=["national", "GB"],
        )

        _inject_retriever(_app, mock_retriever_normal)
        with patch("ekrs_rag.api.routes.constraints.EvidenceBuilder") as MockEB:
            MockEB.build.return_value = [inferred_constraint]
            resp = client.post("/v1/constraints", json={
                "query": "temperature limit",
                "strict": True,
            })

        assert resp.status_code == 400
        assert "missing_context" in resp.json()["detail"]

    def test_strict_allows_explicit_constraint(self, client, mock_retriever_normal, _app):
        """strict=true + explicit constraint -> 200 OK."""
        from ekrs_shared.models import ConstraintV2

        explicit_constraint = ConstraintV2(
            parameter="temperature",
            value_type="interval",
            interval={"lower": None, "upper": 80.0, "lower_inclusive": True, "upper_inclusive": True},
            unit="°C",
            inferred=False,
            priority={"explicit_level": 100, "recency_score": 0.0, "authority_score": 0.0},
            scope_path=["national", "GB"],
        )

        _inject_retriever(_app, mock_retriever_normal)
        with patch("ekrs_rag.api.routes.constraints.EvidenceBuilder") as MockEB:
            MockEB.build.return_value = [explicit_constraint]
            resp = client.post("/v1/constraints", json={
                "query": "temperature limit",
                "strict": True,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["branches"]["general"]["temperature"]["range"][1] == 80.0


# ---------------------------------------------------------------------------
# TC_HARD_CONFLICT_01: conflicting constraints -> 409
# ---------------------------------------------------------------------------


class TestHardConflict:
    """TC_HARD_CONFLICT_01: conflicting constraints return 409."""

    def test_conflicting_constraints_return_409(self, client, mock_retriever_conflicting, _app):
        """Two constraints with no overlap -> 409 conflict."""
        _inject_retriever(_app, mock_retriever_conflicting)
        resp = client.post("/v1/constraints", json={
            "query": "temperature range",
            "strict": False,
        })

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "conflicts" in detail
        assert len(detail["conflicts"]) >= 1
        assert detail["conflicts"][0]["parameter"] == "temperature"

    def test_no_conflict_returns_200(self, client, mock_retriever_normal, _app):
        """Non-conflicting constraints -> 200 OK."""
        _inject_retriever(_app, mock_retriever_normal)
        resp = client.post("/v1/constraints", json={
            "query": "temperature limit",
            "strict": False,
        })

        assert resp.status_code == 200
        data = resp.json()
        assert "branches" in data


# ---------------------------------------------------------------------------
# Three-Gate tests
# ---------------------------------------------------------------------------


class TestThreeGatePipeline:
    """Test the three-gate pipeline: recall -> extract -> solve."""

    def test_gate1_insufficient_recall(self, client, mock_retriever_empty, _app):
        """Gate 1: < MIN_RECALL_CHUNKS -> 404."""
        _inject_retriever(_app, mock_retriever_empty)
        resp = client.post("/v1/constraints", json={
            "query": "temperature",
        })
        assert resp.status_code == 404
        assert "Insufficient recall" in resp.json()["detail"]

    def test_gate2_no_constraints_extracted(self, client, mock_retriever_no_hints, _app):
        """Gate 2: no constraints extracted -> 404."""
        _inject_retriever(_app, mock_retriever_no_hints)
        resp = client.post("/v1/constraints", json={
            "query": "temperature",
        })
        assert resp.status_code == 404
        assert "No constraints extracted" in resp.json()["detail"]
