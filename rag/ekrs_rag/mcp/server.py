"""Phase 10 T10d Td.1 — minimal MCP server exposing EKRS retrieval to AI agents.

Exposes 2 tools via the official Python MCP SDK (``mcp>=1.0``):

* ``ekrs_search(query, top_k=40, active_scope=None)`` — broad-spectrum
  retrieval (vector + FTS + RRF). Direct reuse of
  :class:`ekrs_rag.retrieval.retriever.EKRSRetriever` — no internal HTTP,
  no double rate-limit, no double audit.
* ``ekrs_status()`` — healthz dependency payload, no retriever needed.

Both functions return ``list[TextContent]`` (MCP's wire format). The
JSON shape inside ``TextContent.text`` is the contract documented in
``tests/unit/test_mcp_server_td1.py``.

Why this module is a thin wrapper:

* Iron Rules — MCP layer doesn't touch solver or retrieval logic, so
  R1-R8 stay ✅ by composition.
* Audit — the wrapper does **not** emit a new audit event; the
  retriever's existing ``fts_searched`` / ``qdrant_search_failed``
  events cover the search path. Adding a wrapper-level audit would
  double-write.
* Resilience — retriever exceptions are caught and returned as MCP
  content with ``{"error": "..."}`` so the server never crashes
  mid-session (parent plan §204).

Run from CLI::

    python -m ekrs_rag.mcp.server  # stdio transport (default)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

# Chunk.text is truncated for the MCP payload to keep responses small.
# 200 chars is well past typical NumericHint phrases (50-80 chars) and
# small enough that 40-chunk responses stay under ~8 KB.
CHUNK_TEXT_PREVIEW_CHARS = 200


def _serialize_chunks(chunks: List[Any]) -> List[Dict[str, str]]:
    """Project a list of Chunk objects to the MCP payload schema.

    Only 3 fields exposed per chunk: ``chunk_id``, ``text`` (truncated),
    ``doc_hash``. Solvers/consumers needing full text fall back to the
    HTTP API (``/v1/blocks/{id}``) — not in Td.1 scope.
    """
    payload: List[Dict[str, str]] = []
    for c in chunks:
        text = getattr(c, "text", "") or ""
        if len(text) > CHUNK_TEXT_PREVIEW_CHARS:
            text = text[:CHUNK_TEXT_PREVIEW_CHARS]
        payload.append({
            "chunk_id": getattr(c, "chunk_id", "") or "",
            "text": text,
            "doc_hash": getattr(c, "doc_hash", "") or "",
        })
    return payload


async def ekrs_search(
    retriever: Any,
    query: str,
    top_k: int = 40,
    active_scope: Optional[List[str]] = None,
) -> List[TextContent]:
    """Broad-spectrum retrieval. Delegates to ``retriever.retrieve``.

    Catches retriever exceptions and returns them as MCP content with
    an ``error`` field — never lets a query exception crash the server.
    """
    try:
        result = await retriever.retrieve(
            query, top_k=top_k, active_scope=active_scope
        )
    except Exception as exc:  # noqa: BLE001 — top-level isolation
        payload: Dict[str, Any] = {"error": f"retrieval failed: {exc}"}
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    chunks = getattr(result, "chunks", []) or []
    payload = {"chunks": _serialize_chunks(chunks)}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def ekrs_status(dependencies: Dict[str, str]) -> List[TextContent]:
    """Return the dependency healthcheck payload as MCP content."""
    return [TextContent(type="text", text=json.dumps(dependencies, ensure_ascii=False))]


def build_server(retriever: Any, dependencies: Dict[str, str]) -> FastMCP:
    """Construct a FastMCP server with the 2 Td.1 tools wired up.

    Closure capture — ``retriever`` and ``dependencies`` are frozen at
    construction time. Re-call ``build_server`` if the retriever changes.
    The MCP tool names are wire-protocol names: must match exactly with
    what clients (Claude Code, MCP inspector, etc.) call.
    """
    server = FastMCP("ekrs")

    @server.tool(
        name="ekrs_search",
        description=(
            "Broad-spectrum retrieval over the EKRS index: vector + "
            "BM25 + RRF fusion. Returns up to top_k chunks as JSON "
            "with fields chunk_id, text (truncated to 200 chars), doc_hash."
        ),
    )
    async def _search(query: str, top_k: int = 40, active_scope: Optional[List[str]] = None) -> List[TextContent]:
        return await ekrs_search(retriever, query, top_k=top_k, active_scope=active_scope)

    @server.tool(
        name="ekrs_status",
        description=(
            "Healthz payload: status + per-dependency readiness flags "
            "(retriever, pipeline, audit_index, ...)."
        ),
    )
    async def _status() -> List[TextContent]:
        return await ekrs_status(dependencies)

    return server


if __name__ == "__main__":  # pragma: no cover
    # CLI entrypoint: ``python -m ekrs_rag.mcp.server`` starts stdio
    # transport. Real deployments wire retriever+dependencies through
    # build_server() before calling run().
    server = build_server(retriever=None, dependencies={"status": "starting"})
    server.run(transport="stdio")
