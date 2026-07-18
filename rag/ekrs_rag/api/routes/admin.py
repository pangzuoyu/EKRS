"""Admin routes for operational recovery (X-Admin-Key required).

Currently scoped to audit index recovery. Future admin endpoints
(restart hooks, manual compensations, etc.) should be added here.

Spec §16 / Phase 5.5 F: after audit.log rotation, the in-memory
`AuditIndex` is rebuilt automatically via the writer's `on_rollover`
callback. The endpoint below is for manual rebuild — used by on-call
when an operator truncates audit.log outside the rotation path, or
when the index is suspected to be stale.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ekrs_rag.security import require_admin_key

router = APIRouter(prefix="/v1/admin/audit", tags=["admin"])


@router.post("/rebuild-index", dependencies=[Depends(require_admin_key)])
async def rebuild_audit_index(request: Request) -> dict:
    """Re-scan audit.log from scratch and rebuild the in-memory index.

    Returns 503 if the AuditIndex was not initialized at startup
    (e.g., audit.log missing on a fresh deployment). Otherwise returns
    the post-rebuild entry count and size in bytes.
    """
    audit_index = getattr(request.app.state, "audit_index", None)
    if audit_index is None:
        raise HTTPException(
            status_code=503,
            detail="AuditIndex not initialized (audit.log missing or unreadable)",
        )

    entries = audit_index.rebuild()
    return {
        "status": "ok",
        "entries_indexed": entries,
        "index_size_bytes": audit_index.size,
    }
