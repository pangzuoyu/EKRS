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


async def ekrs_query(
    solver: Any,
    *,
    query: str,
    context: Optional[Dict[str, Any]] = None,
    scope: Optional[List[str]] = None,
    policy: Optional[str] = None,
    overlay_hints: Optional[List[Any]] = None,
    strict: bool = False,
    top_k: int = 40,
) -> List[TextContent]:
    """Full R3 three-gate constraint solve.

    Direct internal call to ``solver.evaluate_constraints`` — no HTTP
    round-trip, no double rate-limit, no double audit. Mirrors
    ``POST /v1/constraints`` semantics.

    The solver contract is the ``evaluate_constraints`` helper exposed
    by ``ekrs_rag.api.routes.constraints`` — the same helper the HTTP
    route delegates to, so R3/R4/R6/R7 are honored transparently.

    Returns ``[TextContent]`` with JSON:
        Success → ``{"branches": {...}, "primary_branch": ..., "mode":
        ..., "conflicts": [...]}``
        Error   → ``{"error": {"type": ..., "status_code": int,
                  "message": ..., "detail": ...}}``
    """
    ctx: Dict[str, Any] = context or {}
    try:
        envelope = await solver.evaluate_constraints(
            query,
            context=ctx,
            scope=scope,
            policy=policy,
            overlay_hints=overlay_hints,
            strict=strict,
            top_k=top_k,
        )
    except Exception as exc:  # noqa: BLE001 — top-level isolation
        payload: Dict[str, Any] = {
            "error": {
                "message": f"solver raised: {exc}",
                "type": "solver_exception",
            }
        }
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    if envelope.get("status") == "error":
        err = dict(envelope["error"])
        err["message"] = err.get("detail", "solver returned error envelope")
        payload = {"error": err}
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    response = envelope["response"]
    # ConstraintQueryResponse is a Pydantic BaseModel — use model_dump.
    if hasattr(response, "model_dump"):
        payload = response.model_dump()
    else:  # pragma: no cover — defensive for dataclass fallbacks
        payload = dict(response.__dict__)
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def ekrs_get_block(
    qdrant: Any,
    *,
    block_id: str,
) -> List[TextContent]:
    """Document deep-read by ``block_id`` (UUID).

    Direct internal call to ``qdrant.get_payload_by_block_id`` — no HTTP
    round-trip. Returns the full Qdrant payload (text NOT truncated;
    this is a deep-read, not a search preview).

    Returns ``[TextContent]`` with JSON:
        Success → ``{"block_id": ..., "doc_hash": ..., "text": ...,
        "scope_path": [...], ...}``
        Not-found → ``{"error": "block_id not found", "block_id": ...}``
        Qdrant error → ``{"error": {"message": ..., "type":
        "qdrant_exception", "block_id": ...}}``

    The ``block_id`` naming is consistent with the FTS5 PK, Qdrant
    payload, and audit event field name.
    """
    try:
        payload = qdrant.get_payload_by_block_id(block_id)
    except Exception as exc:  # noqa: BLE001 — top-level isolation
        err_payload: Dict[str, Any] = {
            "error": {
                "message": f"qdrant raised: {exc}",
                "type": "qdrant_exception",
                "block_id": block_id,
            }
        }
        return [TextContent(type="text", text=json.dumps(err_payload, ensure_ascii=False))]

    if payload is None:
        not_found_payload: Dict[str, Any] = {
            "error": "block_id not found",
            "block_id": block_id,
        }
        return [TextContent(type="text", text=json.dumps(not_found_payload, ensure_ascii=False))]

    # Project numeric_hints to count-only for the wire payload (full list
    # could blow past MCP message-size limits on dense docs).
    if "numeric_hints" in payload and isinstance(payload["numeric_hints"], list):
        payload = {**payload, "numeric_hints": len(payload["numeric_hints"])}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def build_server(
    retriever: Any,
    qdrant: Any,
    solver: Any,
    dependencies: Dict[str, str],
) -> FastMCP:
    """Construct a FastMCP server with the 4 Td.1+Td.2 tools wired up.

    Closure capture — ``retriever``, ``qdrant``, ``solver``, and
    ``dependencies`` are frozen at construction time. Re-call
    ``build_server`` if any dependency changes. The MCP tool names are
    wire-protocol names: must match exactly with what clients (Claude
    Code, MCP inspector, etc.) call.

    Production wiring is the caller's responsibility — the CLI
    entrypoint below passes ``None`` for all deps (zero-config PoC).
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

    @server.tool(
        name="ekrs_query",
        description=(
            "Full constraint solve via the R3 three-gate pipeline "
            "(recall → extract → solve). Returns branches, mode, and "
            "conflicts as JSON. Same semantics as POST /v1/constraints."
        ),
    )
    async def _query(
        query: str,
        context: Optional[Dict[str, Any]] = None,
        scope: Optional[List[str]] = None,
        policy: Optional[str] = None,
        overlay_hints: Optional[List[Any]] = None,
        strict: bool = False,
        top_k: int = 40,
    ) -> List[TextContent]:
        return await ekrs_query(
            solver,
            query=query,
            context=context,
            scope=scope,
            policy=policy,
            overlay_hints=overlay_hints,
            strict=strict,
            top_k=top_k,
        )

    @server.tool(
        name="ekrs_get_block",
        description=(
            "Document deep-read by block_id (UUID). Returns the full "
            "block payload (text NOT truncated, numeric_hints as count "
            "only). Returns {\"error\": \"block_id not found\", ...} when "
            "the block_id is unknown."
        ),
    )
    async def _get_block(block_id: str) -> List[TextContent]:
        return await ekrs_get_block(qdrant, block_id=block_id)

    return server


if __name__ == "__main__":  # pragma: no cover
    # CLI entrypoint: ``python -m ekrs_rag.mcp.server`` starts stdio
    # transport. Real deployments wire retriever+qdrant+solver+dependencies
    # through build_server() before calling run(). CLI default is
    # zero-config (all deps None) — every tool call surfaces the
    # exception-isolation path through real stdio transport.
    server = build_server(
        retriever=None,
        qdrant=None,
        solver=None,
        dependencies={"status": "starting"},
    )
    server.run(transport="stdio")
