"""Admin endpoint to flush the EmbeddingService LRU cache.

Phase 7 T7 (Decision §4): /v1/admin/embedding-cache/flush drops every
cached entry so the next encode() re-computes from the ONNX export.

Use cases:
- Operator swapped model.onnx out-of-band and restarted the service
  with a fresh process (cache is empty by then — flush is a no-op).
- Cache state is suspected to be corrupted (e.g., monotonic-clock
  drift in a long-lived process made TTL checks unreliable).
- Bulk operator action during a planned maintenance window to free
  memory before swap.

Auth: X-Admin-Key (same pattern as /v1/admin/audit/rebuild-index).
The endpoint is intentionally separate from /v1/admin/audit/* so
the routers can be enabled/disabled independently if a deployment
chooses to scope one admin path but not the other.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ekrs_rag.security import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/embedding-cache", tags=["admin"])


@router.post("/flush", dependencies=[Depends(require_admin_key)])
async def flush_embedding_cache(request: Request) -> dict:
    """Drop every cached embedding entry; return how many were cleared.

    Returns 503 if EmbeddingService is not initialized (service started
    in a mode where the embedder is unavailable). Otherwise returns
    ``{status, cleared, model_version, cache_size_after}`` so operators
    can confirm both the action and the resulting empty cache.
    """
    embedding_service = getattr(request.app.state, "embedding_service", None)
    if embedding_service is None:
        raise HTTPException(
            status_code=503,
            detail="EmbeddingService not initialized (embedder unavailable)",
        )

    cleared = embedding_service.flush_cache()
    logger.info(
        "Embedding cache flushed by admin: cleared=%d, model_version=%s",
        cleared,
        embedding_service.model_version,
    )
    return {
        "status": "ok",
        "cleared": cleared,
        "model_version": embedding_service.model_version,
        "cache_size_after": embedding_service.cache_size(),
    }