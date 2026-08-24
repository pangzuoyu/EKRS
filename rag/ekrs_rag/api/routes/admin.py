"""Admin routes for operational recovery (X-Admin-Key required).

Currently scoped to:
- Audit index recovery (``POST /v1/admin/audit/rebuild-index``).
- GPU channel operations (Phase 13b T5.3 / T5.1):
  - ``POST /v1/admin/gpu/invalidate`` — forces the next probe cycle's
    self-check to fail, triggering a GPU→CPU transition within one
    probe interval (default 30s; CI override 5s). Replaces the fragile
    host-level ``chmod 000`` approach which depended on POSIX mount
    semantics (parent §204 + eng-review fix #3).
  - ``POST /v1/admin/gpu/memory-stats`` — exact ``torch.cuda`` peak
    allocation read. Avoids Prometheus multiproc 5s scrape lag when
    Phase B benchmarks need a precise memory peak (T5.1 acceptance
    line #8: peak ≤ 6 GB).

Spec §16 / Phase 5.5 F: audit index rebuild after rotation. Spec §13 /
Phase 13b T5: GPU channel observability for ops + CI failover tests.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from ekrs_rag.security import require_admin_key

logger = logging.getLogger(__name__)


# ----- /v1/admin/audit/* (existing) -----

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


# ----- /v1/admin/gpu/* (Phase 13b T5.1 / T5.3) -----


gpu_router = APIRouter(prefix="/v1/admin/gpu", tags=["admin"])


@gpu_router.post("/invalidate", dependencies=[Depends(require_admin_key)])
async def gpu_invalidate() -> dict:
    """Force the router to drop the GPU channel until the next probe re-validates.

    The encoding router's 30s health-probe daemon (T3.4) calls
    ``force_re_register_gpu()`` periodically. We forcibly set the
    router's ``current_channel="cpu"`` under the lock, which transitions
    the state machine from "gpu" → "cpu" and emits a
    ``channel_switched{gpu→cpu, reason="admin_invalidate"}`` audit event
    immediately. The next probe cycle will then call ``force_re_register_gpu``
    (which clears the registration flag) and re-run ``_self_check()``;
    if the GPU is healthy, the state will transition back to "gpu" and
    emit a ``cpu→gpu`` audit event (recovery detected).

    This endpoint is the test-automation trigger for T5.3 (failover test)
    and an ops escape hatch for "I just updated the GPU driver and want
    to take RAG off GPU until I've verified it." No file-system writes
    or host-mount assumptions; safe in any deployment topology.

    Returns:
        ``{"status": "invalidated", "from_channel": "...", "to_channel": "cpu"}``

        503 if the router wasn't initialized (worker subprocess boot
        race — operators should retry in a few seconds).
    """
    from ekrs_rag.services import encoding_router

    router_obj = encoding_router.get_router()
    if router_obj is None:
        raise HTTPException(
            status_code=503,
            detail="EncodingRouter not initialized",
        )

    # Force gpu→cpu under the lock and emit the audit event.
    with router_obj._lock:  # type: ignore[attr-defined]
        old_channel = router_obj._state.current_channel  # type: ignore[attr-defined]
        router_obj._state.current_channel = "cpu"  # type: ignore[attr-defined]
        router_obj._state.last_switch_ts = datetime.now(timezone.utc).isoformat()
        router_obj._state.switch_count_by_reason["admin_invalidate"] = (
            router_obj._state.switch_count_by_reason.get("admin_invalidate", 0) + 1
        )

    # Audit emit — release the lock first (the audit writer may take its
    # own locks; see parent §204 on defensive isolation).
    if old_channel != "cpu":
        router_obj._emit_channel_switched(
            from_channel=old_channel,
            to_channel="cpu",
            reason="admin_invalidate",
        )

    logger.info(
        "gpu_invalidate: state %s -> cpu; next probe will re-evaluate",
        old_channel,
    )
    return {
        "status": "invalidated",
        "from_channel": old_channel,
        "to_channel": "cpu",
    }


@gpu_router.post("/memory-stats", dependencies=[Depends(require_admin_key)])
async def gpu_memory_stats() -> dict:
    """Return exact GPU memory stats via ``torch.cuda``.

    Provides ``peak_bytes`` (``max_memory_allocated``) and
    ``allocated_bytes`` (``memory_allocated``) for a given device —
    bypassing Prometheus multiproc scrape lag (5s default) for
    benchmark scripts that need a precise peak right after Phase B
    completes.

    Returns:
        ``{"peak_bytes": int, "allocated_bytes": int, "device": "cuda:0"}``

        503 if torch / CUDA is unavailable (CPU-only install).
        500 if torch.cuda raises (driver fault, MIG config, etc.).
    """
    try:
        import torch
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="torch not installed (CPU-only deployment)",
        )

    if not torch.cuda.is_available():
        raise HTTPException(
            status_code=503,
            detail="CUDA not available",
        )

    # Use device 0 by default; Phase 13b T1 only supports single-GPU.
    # Multi-GPU support deferred to Phase 14.
    from ekrs_rag.core.config import settings

    device_id = getattr(settings, "BGE_M3_GPU_DEVICE_ID", 0)
    device_str = f"cuda:{device_id}"

    try:
        peak = int(torch.cuda.max_memory_allocated(device_id))
        allocated = int(torch.cuda.memory_allocated(device_id))
    except Exception as e:
        # Defensive: catch any torch-side fault (driver reset, MIG
        # misconfig, etc.) and surface as 500 — parent §204 says we
        # never let an admin read cascade into the worker pool.
        logger.warning("gpu_memory_stats: torch.cuda failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"torch.cuda failed: {e}",
        )

    return {
        "peak_bytes": peak,
        "allocated_bytes": allocated,
        "device": device_str,
    }