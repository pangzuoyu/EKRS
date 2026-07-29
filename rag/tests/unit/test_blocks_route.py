"""Phase 10 T10d Td.2 RED — HTTP route contract tests for GET /v1/blocks/{block_id}.

Returns full block payload (text NOT truncated) keyed by ``block_id``
(UUID from ir_parser — matches FTS5 schema, Qdrant payload, and audit
event naming for cross-surface consistency).

Used by the new ``ekrs_get_block`` MCP tool (Td.2.3) and any HTTP consumer
needing document deep-read (vs. the 200-char preview in ``ekrs_search``).

Contract:
- Path: ``GET /v1/blocks/{block_id}`` (block_id is the UUID assigned by
  ir_parser at ingestion time; visible to all surfaces — FTS5 PK, Qdrant
  payload, audit events)
- Auth: ``require_parser_token`` (same as ``/v1/constraints``)
- 200: ``BlockResponse`` with ``block_id, doc_hash, text, scope_path,
  page_numbers, token_count, version, source_block_ids, numeric_hints``
  (``numeric_hints`` is COUNT ONLY, not the full payload)
- 404: block_id not found in Qdrant → ``{"detail": "block_id not found"}``
- 503: ``app.state.qdrant`` not initialized → ``{"detail": "qdrant not initialized"}``
- QdrantException isolation: 500 (R2 purity preserved — proxy never crashes)

All tests fail RED until:
- ``rag/ekrs_rag/api/routes/blocks.py`` is added
- ``QdrantManager.get_payload_by_block_id(block_id)`` is added
- ``rag/ekrs_rag/main.py`` includes the blocks router
"""
from __future__ import annotations

import os
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------
class _StubQdrant:
    """Stub for QdrantManager with only the Td.2 get_payload_by_block_id method."""

    def __init__(
        self,
        *,
        payload: Optional[dict] = None,
        exc: Optional[Exception] = None,
    ) -> None:
        self._payload = payload
        self._exc = exc
        self.calls: list[str] = []

    def get_payload_by_block_id(self, block_id: str) -> Optional[dict]:
        self.calls.append(block_id)
        if self._exc is not None:
            raise self._exc
        return self._payload


def _build_app(qdrant: Optional[_StubQdrant]) -> FastAPI:
    """Build a minimal FastAPI app with the blocks router + state override.

    Imports happen lazily so this module can be collected before the
    production route module exists (RED).
    """
    from ekrs_rag.api.routes.blocks import router as blocks_router

    app = FastAPI()
    app.include_router(blocks_router)
    if qdrant is not None:
        # Inject via app.state for the route's Depends(get_qdrant) to read.
        app.state.qdrant = qdrant
    # Auth disabled for unit tests (PARSER_TOKEN="" → no-op dep).
    os.environ["PARSER_TOKEN"] = ""
    return app


# ---------------------------------------------------------------------------
# Test 1: GET /v1/blocks/{block_id} → 200 with full BlockResponse
# ---------------------------------------------------------------------------
def test_blocks_route_returns_full_block_payload():
    qdrant = _StubQdrant(payload={
        "block_id": "b-uuid-abc12345",
        "doc_hash": "abc12345abcdef",
        "text": "Q345 钢板温度 ≤ 80℃ (full text, no 200-char truncation)",
        "scope_path": ["第1章", "1.1"],
        "page_numbers": [3],
        "token_count": 42,
        "version": 1,
        "source_block_ids": ["b-uuid-abc12345"],
        "numeric_hints": [
            {"value": 80, "unit": "C", "operator": "<="},
        ],
    })
    app = _build_app(qdrant)
    client = TestClient(app)

    resp = client.get("/v1/blocks/b-uuid-abc12345")

    assert resp.status_code == 200
    body = resp.json()
    assert body["block_id"] == "b-uuid-abc12345"
    assert body["doc_hash"] == "abc12345abcdef"
    assert body["text"].startswith("Q345 钢板温度")
    assert body["scope_path"] == ["第1章", "1.1"]
    assert body["page_numbers"] == [3]
    assert body["token_count"] == 42
    assert body["version"] == 1
    assert body["source_block_ids"] == ["b-uuid-abc12345"]
    assert isinstance(body["numeric_hints"], int)  # COUNT only, not the full list
    assert body["numeric_hints"] == 1
    # Qdrant was hit exactly once with the right block_id.
    assert qdrant.calls == ["b-uuid-abc12345"]


# ---------------------------------------------------------------------------
# Test 2: block_id not found in Qdrant → 404 with not-found detail
# ---------------------------------------------------------------------------
def test_blocks_route_returns_404_when_block_id_missing():
    qdrant = _StubQdrant(payload=None)
    app = _build_app(qdrant)
    client = TestClient(app)

    resp = client.get("/v1/blocks/missing-block-id")

    assert resp.status_code == 404
    body = resp.json()
    assert "not found" in body["detail"]


# ---------------------------------------------------------------------------
# Test 3: qdrant not initialized in app.state → 503
# ---------------------------------------------------------------------------
def test_blocks_route_returns_503_when_qdrant_uninitialized():
    app = _build_app(qdrant=None)  # app.state.qdrant unset
    client = TestClient(app)

    resp = client.get("/v1/blocks/any-block")

    assert resp.status_code == 503
    assert "qdrant" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test 4: Qdrant exception isolation → 500 (proxy never crashes the server)
# ---------------------------------------------------------------------------
def test_blocks_route_isolates_qdrant_exceptions():
    qdrant = _StubQdrant(exc=RuntimeError("qdrant unreachable"))
    app = _build_app(qdrant)
    client = TestClient(app)

    resp = client.get("/v1/blocks/any-block")

    assert resp.status_code == 500
    assert "qdrant unreachable" in str(resp.json())