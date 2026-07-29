"""Phase 10 T10d Td.1 RED — MCP server contract tests.

Contract under test:
- ``ekrs_rag.mcp.server`` module exposes:
    * ``ekrs_search(retriever, query, top_k=40, active_scope=None) -> list[TextContent]``
    * ``ekrs_status(dependencies) -> list[TextContent]``
    * ``build_server(retriever, dependencies) -> FastMCP``
- Server registers exactly 2 tools: ``ekrs_search`` + ``ekrs_status`` (named
  exactly, MCP wire-protocol name).
- ``ekrs_search`` dispatches to ``retriever.retrieve(query, *, top_k, active_scope)``
  passing kwargs through unchanged.
- ``ekrs_search`` output = ``[TextContent(type='text', text=<JSON str>)]``
  with JSON shape ``{"chunks": [{"chunk_id", "text", "doc_hash"}, ...]}``.
- ``ekrs_search`` catches retriever exceptions, returns MCP content with
  ``{"error": "..."}`` instead of letting the server crash (parent §204).
- ``ekrs_status`` returns ``[TextContent(type='text', text=<JSON str>)]``
  whose JSON shape equals ``dependencies`` dict (no retriever needed).

All tests fail RED until ``rag/ekrs_rag/mcp/server.py`` is implemented.
Tests are independent of any real Qdrant/FTS instances — pure stubs.
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

import pytest

# Conditionally import MCP types — if not installed, all tests in this
# module should fail rather than error at collection time.
try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import TextContent
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment,misc]
    TextContent = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Test fixtures: lightweight retriever + result stubs (no Qdrant/FTS needed)
# ---------------------------------------------------------------------------
class _StubResult:
    def __init__(self, chunks: Optional[list] = None) -> None:
        self.chunks = chunks or []


class _StubChunk:
    """Minimal Chunk stand-in: only the 3 fields read by ekrs_search serialization."""

    def __init__(self, *, chunk_id: str, text: str, doc_hash: str) -> None:
        self.chunk_id = chunk_id
        self.text = text
        self.doc_hash = doc_hash


class _StubRetriever:
    """Stand-in for ``EKRSRetriever``. Records last-call kwargs + returns canned result."""

    def __init__(self, *, result: Optional[_StubResult] = None, exc: Optional[Exception] = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result
        self._exc = exc

    async def retrieve(self, query: str, *, top_k: int = 40, active_scope: Optional[List[str]] = None) -> Any:
        self.calls.append({"query": query, "top_k": top_k, "active_scope": active_scope})
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def stub_retriever() -> _StubRetriever:
    return _StubRetriever()


@pytest.fixture
def stub_chunk() -> _StubChunk:
    return _StubChunk(chunk_id="abc12345-0000", text="Q345 钢板温度 ≤ 80℃", doc_hash="abc12345")


# ---------------------------------------------------------------------------
# Test 1: module import — RED fail until mcp/server.py exists
# ---------------------------------------------------------------------------
def test_mcp_server_module_imports() -> None:
    """Module must exist with 3 expected symbols."""
    from ekrs_rag.mcp import server as mcp_server  # noqa: F401

    assert hasattr(mcp_server, "ekrs_search"), "ekrs_search missing"
    assert hasattr(mcp_server, "ekrs_status"), "ekrs_status missing"
    assert hasattr(mcp_server, "build_server"), "build_server missing"


# ---------------------------------------------------------------------------
# Test 2: build_server registers exactly 2 tools with wire-protocol names
# ---------------------------------------------------------------------------
def test_build_server_registers_two_named_tools(stub_retriever: _StubRetriever) -> None:
    from ekrs_rag.mcp.server import build_server

    server = build_server(stub_retriever, dependencies={"status": "ok"})
    tools = _list_tool_names(server)
    assert tools == {"ekrs_search", "ekrs_status"}, f"unexpected tools: {tools}"


# ---------------------------------------------------------------------------
# Test 3: ekrs_search dispatches to retriever with kwargs passed through
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_search_dispatches_kwargs_to_retriever(stub_retriever: _StubRetriever) -> None:
    from ekrs_rag.mcp.server import ekrs_search

    await ekrs_search(
        stub_retriever,
        "Q345 钢板",
        top_k=10,
        active_scope=["第1章"],
    )

    assert len(stub_retriever.calls) == 1, "retriever.retrieve not called exactly once"
    call = stub_retriever.calls[0]
    assert call["query"] == "Q345 钢板"
    assert call["top_k"] == 10
    assert call["active_scope"] == ["第1章"]


# ---------------------------------------------------------------------------
# Test 4: ekrs_search returns MCP TextContent list with JSON chunks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_search_returns_textcontent_json_chunks(stub_chunk: _StubChunk) -> None:
    from ekrs_rag.mcp.server import ekrs_search

    retriever = _StubRetriever(result=_StubResult(chunks=[stub_chunk]))

    content = await ekrs_search(retriever, "any query")

    assert isinstance(content, list) and len(content) == 1
    item = content[0]
    assert isinstance(item, TextContent), f"expected TextContent, got {type(item)}"
    assert item.type == "text"
    payload = json.loads(item.text)
    assert "chunks" in payload
    assert len(payload["chunks"]) == 1
    first = payload["chunks"][0]
    assert first["chunk_id"] == "abc12345-0000"
    assert first["doc_hash"] == "abc12345"
    assert "Q345" in first["text"]


# ---------------------------------------------------------------------------
# Test 5: ekrs_search catches retriever exceptions — no crash, returns MCP error
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_search_isolates_retriever_exceptions(stub_retriever: _StubRetriever) -> None:
    from ekrs_rag.mcp.server import ekrs_search

    stub_retriever._exc = RuntimeError("qdrant unreachable")

    content = await ekrs_search(stub_retriever, "any query")

    # Parent §204: business path must be resilient; retriever error must NOT
    # propagate. Return MCP content with error field instead.
    assert isinstance(content, list) and len(content) == 1
    payload = json.loads(content[0].text)
    assert "error" in payload, f"missing 'error' field in: {payload}"
    assert "qdrant unreachable" in payload["error"]


# ---------------------------------------------------------------------------
# Test 6: ekrs_search with zero chunks returns empty list, no 5xx-style error
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_search_empty_chunks_returns_empty_list(stub_retriever: _StubRetriever) -> None:
    from ekrs_rag.mcp.server import ekrs_search

    stub_retriever._result = _StubResult(chunks=[])

    content = await ekrs_search(stub_retriever, "no hits")

    payload = json.loads(content[0].text)
    assert payload["chunks"] == []


# ---------------------------------------------------------------------------
# Test 7: ekrs_status returns JSON TextContent of dependencies dict
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_status_returns_dependency_payload() -> None:
    from ekrs_rag.mcp.server import ekrs_status

    deps = {"status": "ok", "retriever": "ready", "pipeline": "ready"}

    content = await ekrs_status(deps)

    assert isinstance(content, list) and len(content) == 1
    payload = json.loads(content[0].text)
    assert payload == deps


# ---------------------------------------------------------------------------
# Test 8: ekrs_status doesn't touch retriever — server start safe even when
# retriever is a null object (safety net for late init / degraded mode).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ekrs_status_independent_of_retriever() -> None:
    from ekrs_rag.mcp.server import ekrs_status

    deps = {"status": "starting", "retriever": "uninitialized"}

    # Should NOT raise even though we never passed a retriever.
    content = await ekrs_status(deps)

    payload = json.loads(content[0].text)
    assert payload["retriever"] == "uninitialized"


# ---------------------------------------------------------------------------
# Test helpers (test-local; keep implementation detail hidden)
# ---------------------------------------------------------------------------
def _list_tool_names(server: FastMCP) -> set[str]:
    """Return the wire-protocol names of all tools registered on a FastMCP server.

    Best-effort: FastMCP may store tools in different attributes across
    versions. We probe ``_tool_manager`` (current mcp==1.27 layout) and
    fall back to a public-ish attribute if needed.
    """
    # mcp==1.27 path: tool_manager._tools dict keyed by tool name.
    tool_manager = getattr(server, "_tool_manager", None)
    if tool_manager is not None:
        tools_dict = getattr(tool_manager, "_tools", None)
        if isinstance(tools_dict, dict):
            return {name for name in tools_dict.keys()}
    # Fallback: scan known attributes.
    for attr in ("tools", "_tools"):
        tools = getattr(server, attr, None)
        if isinstance(tools, dict):
            return {name for name in tools.keys()}
        if isinstance(tools, list):
            names: set[str] = set()
            for t in tools:
                name = getattr(t, "name", None)
                if name:
                    names.add(name)
            if names:
                return names
    pytest.fail("could not locate tool registry on FastMCP server")
