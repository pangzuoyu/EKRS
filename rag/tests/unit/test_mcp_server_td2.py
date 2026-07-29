"""Phase 10 T10d Td.2 RED — MCP server extension contract tests.

Adds 2 new tools to the Td.1 server:
- ``ekrs_query(query, context, scope, policy, overlay_hints, strict, top_k)``
  — full constraint solve via the R3 three-gate pipeline. Direct internal
  call to ``evaluate_constraints`` (no HTTP round-trip).
- ``ekrs_get_block(block_id)`` — direct lookup by block_id. Returns full
  block payload (text NOT truncated — this is document deep-read, not
  search preview).

Contract:
- ``ekrs_query`` accepts same kwargs as ``POST /v1/constraints`` body.
- ``ekrs_query`` returns ``[TextContent(type='text', text=<JSON>)]`` with
  JSON shape ``{"branches": {...}, "primary_branch": ..., "mode": ...,
  "conflicts": [...]}`` (success) or ``{"error": "..."}`` (failure).
- ``ekrs_query`` exception isolation: solver raise / 404 / 409 / 503 all
  converted to ``{"error": "..."}`` MCP content (parent §204).
- ``ekrs_get_block`` returns ``[TextContent(type='text', text=<JSON>)]``
  with full block payload (``block_id, doc_hash, text, scope_path,
  page_numbers, token_count, version, source_block_ids, numeric_hints``).
- ``ekrs_get_block`` not-found → ``{"error": "block_id not found"}`` MCP
  content rather than HTTP 404 — MCP wire is content-based.
- ``build_server`` registers all 4 tools (Td.1 + Td.2).

All tests fail RED until:
- ``ekrs_query`` and ``ekrs_get_block`` are added to ``rag/ekrs_rag/mcp/server.py``
- ``evaluate_constraints`` helper is added to ``rag/ekrs_rag/api/routes/constraints.py``
- ``QdrantManager.get_payload_by_block_id`` is added
- ``GET /v1/blocks/{block_id}`` route is added to ``rag/ekrs_rag/api/routes/blocks.py``
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

import pytest

try:
    from mcp.types import TextContent
except ImportError:  # pragma: no cover
    TextContent = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------
class _StubConstraintResult:
    """Mimics ConstraintQueryResponse shape used by evaluate_constraints."""

    def __init__(
        self,
        *,
        branches: Optional[dict] = None,
        primary_branch: Optional[str] = None,
        mode: str = "single",
        conflicts: Optional[list] = None,
        trace: Optional[list] = None,
    ) -> None:
        self.branches = branches or {}
        self.primary_branch = primary_branch
        self.mode = mode
        self.conflicts = conflicts or []
        self.trace = trace or []

    def model_dump(self) -> dict:
        """Mirror Pydantic BaseModel.model_dump() — the production
        ekrs_query code uses hasattr(.., 'model_dump') to project."""
        return {
            "branches": self.branches,
            "primary_branch": self.primary_branch,
            "mode": self.mode,
            "conflicts": self.conflicts,
            "trace": self.trace,
        }


class _StubEvaluateResult(dict):
    """Mimics evaluate_constraints envelope — a dict (matches production).

    Subclasses dict so ``envelope.get("status")`` works as in production.
    The success path carries a ``response`` key (typed object with
    ``.model_dump``); the error path carries an ``error`` key.
    """

    SUCCESS = "success"
    ERROR = "error"

    def __init__(
        self,
        *,
        status: str,
        response: Optional[_StubConstraintResult] = None,
        error: Optional[dict] = None,
    ) -> None:
        super().__init__(status=status, response=response, error=error)


class _StubSolver:
    """Stub for the internal constraint solver service."""

    def __init__(self, *, result: Optional[_StubEvaluateResult] = None, exc: Optional[Exception] = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result
        self._exc = exc

    async def evaluate_constraints(
        self,
        query: str,
        *,
        context: dict,
        scope: Optional[List[str]],
        policy: Optional[str],
        overlay_hints: Optional[list],
        strict: bool,
        top_k: int,
    ) -> _StubEvaluateResult:
        self.calls.append({
            "query": query,
            "context": context,
            "scope": scope,
            "policy": policy,
            "overlay_hints": overlay_hints,
            "strict": strict,
            "top_k": top_k,
        })
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


class _StubQdrant:
    """Stub for QdrantManager.get_payload_by_block_id."""

    def __init__(self, *, payload: Optional[dict] = None, exc: Optional[Exception] = None) -> None:
        self.calls: list[str] = []
        self._payload = payload
        self._exc = exc

    def get_payload_by_block_id(self, block_id: str) -> Optional[dict]:
        self.calls.append(block_id)
        if self._exc is not None:
            raise self._exc
        return self._payload


@pytest.fixture
def stub_solver() -> _StubSolver:
    return _StubSolver()


@pytest.fixture
def stub_qdrant() -> _StubQdrant:
    return _StubQdrant()


# ---------------------------------------------------------------------------
# Test 1: module imports — RED: new symbols must exist
# ---------------------------------------------------------------------------
def test_mcp_server_td2_imports() -> None:
    from ekrs_rag.mcp import server as mcp_server

    assert hasattr(mcp_server, "ekrs_query"), "ekrs_query missing"
    assert hasattr(mcp_server, "ekrs_get_block"), "ekrs_get_block missing"
    # build_server now takes 4 args (retriever, qdrant, solver, dependencies)
    import inspect
    sig = inspect.signature(mcp_server.build_server)
    params = list(sig.parameters.keys())
    assert "retriever" in params
    assert "qdrant" in params
    assert "solver" in params
    assert "dependencies" in params


# ---------------------------------------------------------------------------
# Test 2: build_server registers 4 tools (Td.1 + Td.2)
# ---------------------------------------------------------------------------
def test_build_server_registers_four_named_tools() -> None:
    from ekrs_rag.mcp.server import build_server

    server = build_server(
        retriever=None,
        qdrant=None,
        solver=None,
        dependencies={"status": "ok"},
    )
    # Use the test helper from Td.1 (no need to reimport; assume pytest
    # collected Td.1 tests first or same module layout).
    from tests.unit.test_mcp_server_td1 import _list_tool_names

    tools = _list_tool_names(server)
    assert tools == {"ekrs_search", "ekrs_status", "ekrs_query", "ekrs_get_block"}, (
        f"unexpected tools: {tools}"
    )


# ---------------------------------------------------------------------------
# Test 3: ekrs_query dispatches to solver with kwargs passthrough
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_query_dispatches_kwargs_to_solver(stub_solver: _StubSolver) -> None:
    from ekrs_rag.mcp.server import ekrs_query

    stub_solver._result = _StubEvaluateResult(
        status=_StubEvaluateResult.SUCCESS,
        response=_StubConstraintResult(branches={"temperature": {"range": [50, 80]}}),
    )

    await ekrs_query(
        stub_solver,
        query="Q345 钢板温度",
        context={"material": "Q345"},
        scope=["第1章"],
        policy="CONSERVATIVE",
        overlay_hints=None,
        strict=True,
        top_k=20,
    )

    assert len(stub_solver.calls) == 1
    call = stub_solver.calls[0]
    assert call["query"] == "Q345 钢板温度"
    assert call["context"] == {"material": "Q345"}
    assert call["scope"] == ["第1章"]
    assert call["policy"] == "CONSERVATIVE"
    assert call["strict"] is True
    assert call["top_k"] == 20


# ---------------------------------------------------------------------------
# Test 4: ekrs_query success output = JSON branches + mode + conflicts
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_query_success_returns_branch_json(stub_solver: _StubSolver) -> None:
    from ekrs_rag.mcp.server import ekrs_query

    stub_solver._result = _StubEvaluateResult(
        status=_StubEvaluateResult.SUCCESS,
        response=_StubConstraintResult(
            branches={"temperature": {"range": [50, 80], "unit": "C"}},
            primary_branch="general",
            mode="single",
            conflicts=[],
        ),
    )

    content = await ekrs_query(stub_solver, query="Q345 温度")

    assert isinstance(content, list) and len(content) == 1
    item = content[0]
    assert isinstance(item, TextContent)
    payload = json.loads(item.text)
    assert payload["branches"]["temperature"]["range"] == [50, 80]
    assert payload["mode"] == "single"
    assert payload["conflicts"] == []


# ---------------------------------------------------------------------------
# Test 5: ekrs_query solver error envelope → MCP content with error field
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_query_solver_error_envelope(stub_solver: _StubSolver) -> None:
    from ekrs_rag.mcp.server import ekrs_query

    stub_solver._result = _StubEvaluateResult(
        status=_StubEvaluateResult.ERROR,
        error={"type": "insufficient_recall", "status_code": 404, "detail": "Insufficient recall"},
    )

    content = await ekrs_query(stub_solver, query="empty query")

    payload = json.loads(content[0].text)
    assert "error" in payload
    assert payload["error"]["type"] == "insufficient_recall"
    assert payload["error"]["status_code"] == 404


# ---------------------------------------------------------------------------
# Test 6: ekrs_query solver raise → error MCP content (no crash, parent §204)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_query_isolates_solver_exceptions(stub_solver: _StubSolver) -> None:
    from ekrs_rag.mcp.server import ekrs_query

    stub_solver._exc = RuntimeError("solver blew up")

    content = await ekrs_query(stub_solver, query="any")

    payload = json.loads(content[0].text)
    assert "error" in payload
    assert "solver blew up" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# Test 7: ekrs_get_block dispatches to QdrantManager.get_payload_by_block_id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_get_block_dispatches_block_id(stub_qdrant: _StubQdrant) -> None:
    from ekrs_rag.mcp.server import ekrs_get_block

    stub_qdrant._payload = {
        "block_id": "b-uuid-abc12345",
        "doc_hash": "abc12345abcdef",
        "text": "Q345 钢板温度 ≤ 80℃",
        "scope_path": ["第1章", "1.1"],
        "page_numbers": [3],
        "token_count": 42,
        "version": 1,
        "source_block_ids": ["b-uuid-abc12345"],
        "numeric_hints": [
            {"value": 80, "unit": "C", "operator": "<="},
        ],
    }

    content = await ekrs_get_block(stub_qdrant, block_id="b-uuid-abc12345")

    assert stub_qdrant.calls == ["b-uuid-abc12345"]
    payload = json.loads(content[0].text)
    assert payload["block_id"] == "b-uuid-abc12345"
    assert payload["text"] == "Q345 钢板温度 ≤ 80℃"
    # Production projects numeric_hints to count-only (parent plan D5
    # + handbook §6 — full list blows past MCP message-size limits).
    assert payload["numeric_hints"] == 1
    assert isinstance(payload["numeric_hints"], int)


# ---------------------------------------------------------------------------
# Test 8: ekrs_get_block not-found → error MCP content (no HTTP 404)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_get_block_not_found_returns_error_content(stub_qdrant: _StubQdrant) -> None:
    from ekrs_rag.mcp.server import ekrs_get_block

    stub_qdrant._payload = None  # not found

    content = await ekrs_get_block(stub_qdrant, block_id="missing-block-id")

    payload = json.loads(content[0].text)
    # Flat error envelope: top-level error string + echoed block_id.
    assert "error" in payload
    assert "not found" in payload["error"]
    assert payload["block_id"] == "missing-block-id"


# ---------------------------------------------------------------------------
# Test 9: ekrs_get_block qdrant exception → error MCP content
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_get_block_isolates_qdrant_exceptions(stub_qdrant: _StubQdrant) -> None:
    from ekrs_rag.mcp.server import ekrs_get_block

    stub_qdrant._exc = RuntimeError("qdrant unreachable")

    content = await ekrs_get_block(stub_qdrant, block_id="any")

    payload = json.loads(content[0].text)
    assert "error" in payload
    assert "qdrant unreachable" in payload["error"]["message"]