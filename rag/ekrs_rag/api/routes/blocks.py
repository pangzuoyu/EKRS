"""Blocks API route (Phase 10 T10d Td.2).

GET /v1/blocks/{block_id} — document deep-read by ``block_id`` (UUID
from ir_parser). Returns the full Qdrant payload (text NOT truncated;
this is a deep-read endpoint, not a search preview).

Backs the ``ekrs_get_block`` MCP tool (Td.2.3) and any HTTP consumer
needing the full block payload.

Iron Rules honored:
- R1 (source_span / block_id): the path param is the canonical block_id.
- R4 (scope priority): not relevant here (this is a read proxy, no
  ranking or scope comparison).
- R7 (scope_path): the response includes scope_path when present in the
  payload so downstream consumers can filter / display.

No R6 strict-mode gating here either — this endpoint returns the raw
chunk, not a solved constraint set.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ekrs_rag.api.auth import require_parser_token
from ekrs_rag.retrieval.qdrant_client import QdrantManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["blocks"])


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def get_qdrant(request: Request) -> QdrantManager:
    """Strict dep: read QdrantManager from app.state. 503 if uninitialized."""
    q = getattr(request.app.state, "qdrant", None)
    if q is None:
        raise HTTPException(status_code=503, detail="qdrant not initialized")
    return q


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class BlockResponse(BaseModel):
    """Block payload returned by GET /v1/blocks/{block_id}.

    ``numeric_hints`` is exposed as an INT COUNT (not the full hint list)
    — the full list lives at /v1/constraints or via the MCP ``ekrs_query``
    tool. This projection keeps the response bounded for large blocks.
    """

    block_id: str
    doc_hash: str
    text: str
    scope_path: List[str] = []
    page_numbers: List[int] = []
    token_count: int = 0
    version: int = 0
    source_block_ids: List[str] = []
    numeric_hints: int = 0  # count only


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get(
    "/blocks/{block_id}",
    response_model=BlockResponse,
    responses={
        404: {"description": "block_id not found in Qdrant"},
        503: {"description": "qdrant dependency not initialized"},
    },
)
async def get_block(
    block_id: str,
    qdrant: QdrantManager = Depends(get_qdrant),
    _auth: None = Depends(require_parser_token),
) -> BlockResponse:
    """Read a single block payload by ``block_id``.

    200 — full BlockResponse.
    404 — block_id unknown (Qdrant scroll returned no points).
    503 — ``app.state.qdrant`` unset (lifespan not initialized).
    500 — Qdrant transport error (isolated; proxy never crashes).
    """
    try:
        payload: Optional[Dict[str, Any]] = qdrant.get_payload_by_block_id(block_id)
    except Exception as exc:  # noqa: BLE001 — top-level isolation
        logger.error("blocks route: qdrant raised for %s: %s", block_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"qdrant error: {exc}",
        )

    if payload is None:
        raise HTTPException(status_code=404, detail="block_id not found")

    # Project numeric_hints (list) → count (int) for the response shape.
    raw_hints = payload.get("numeric_hints", [])
    if isinstance(raw_hints, list):
        numeric_hints_count = len(raw_hints)
    elif isinstance(raw_hints, int):
        numeric_hints_count = raw_hints
    else:
        numeric_hints_count = 0

    return BlockResponse(
        block_id=str(payload.get("block_id", block_id)),
        doc_hash=str(payload.get("doc_hash", "")),
        text=str(payload.get("text", "")),
        scope_path=list(payload.get("scope_path", []) or []),
        page_numbers=list(payload.get("page_numbers", []) or []),
        token_count=int(payload.get("token_count", 0) or 0),
        version=int(payload.get("version", 0) or 0),
        source_block_ids=list(payload.get("source_block_ids", []) or []),
        numeric_hints=numeric_hints_count,
    )