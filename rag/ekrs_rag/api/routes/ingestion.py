"""Ingestion API routes.

POST /v1/ingestion/notify — accept parser notification, queue ingestion
GET /v1/ingestion/status/{doc_hash} — query ingestion status
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from ekrs_shared.idempotency import request_id_from_trace
from ekrs_shared.models import IngestionStatus, IngestionNotification

from ..auth import require_parser_token

from ...concurrency.redis_lock import RedisLock
from ...core.config import settings
from ...ingestion.outcome import IngestionOutcome
from ...ingestion.pipeline import IngestionPipeline
from ...storage.task_repo import TaskRepo
from ...storage.documents import Document

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


async def _run_locked_ingest(
    pipeline: IngestionPipeline,
    repo: TaskRepo,
    lock: RedisLock,
    lock_key: str,
    lock_token: str,
    notification: IngestionNotification,
    request_id: str,
) -> None:
    """Run ingestion under the per-doc Redis lock and map outcome → TaskRepo.

    - outcome.rag_status == "success"  → repo.mark_status(request_id, "COMPLETED")
    - outcome.rag_status == "failed"   → repo.mark_failed_with_error(...)
    - unhandled system exception       → repo.mark_failed_with_error + re-raise
    The lock is always released in the finally block.

    Audit (Phase 7 T2): emit ingestion_received on entry, then
    ingestion_completed or ingestion_failed on each terminal branch. The
    writer is best-effort: missing writer in test fixtures is silently
    skipped (mirrors callback_url_blocked pattern).
    """
    from ekrs_rag.observability.audit import get_writer

    writer = get_writer()
    if writer is not None:
        writer.write(
            "ingestion_received",
            request_id=request_id,
            doc_id=notification.doc_hash,
        )

    try:
        outcome = await pipeline.ingest(notification)
        if isinstance(outcome, IngestionOutcome):
            if outcome.rag_status == "success":
                repo.mark_status(request_id, "COMPLETED")
                if writer is not None:
                    writer.write(
                        "ingestion_completed",
                        request_id=request_id,
                        doc_id=notification.doc_hash,
                        chunks_indexed=outcome.chunks_indexed,
                    )
            else:
                repo.mark_failed_with_error(request_id, outcome.error or "unknown")
                if writer is not None:
                    writer.write(
                        "ingestion_failed",
                        request_id=request_id,
                        doc_id=notification.doc_hash,
                        error_code=outcome.error_code or "unknown",
                        error=outcome.error or "unknown",
                    )
        else:  # back-compat: legacy code path returning None
            repo.mark_status(request_id, "COMPLETED")
            if writer is not None:
                writer.write(
                    "ingestion_completed",
                    request_id=request_id,
                    doc_id=notification.doc_hash,
                    chunks_indexed=0,
                )
    except Exception as e:
        repo.mark_failed_with_error(request_id, f"unhandled: {e}")
        if writer is not None:
            writer.write(
                "ingestion_failed",
                request_id=request_id,
                doc_id=notification.doc_hash,
                error_code="unhandled_exception",
                error=str(e),
            )
        raise
    finally:
        await lock.release(lock_key, lock_token)


# ---------------------------------------------------------------------------
# Dependency functions
# ---------------------------------------------------------------------------


def get_pipeline(request: Request) -> IngestionPipeline:
    """Strict dep: read pipeline from app.state. 503 if uninitialized."""
    p = getattr(request.app.state, "pipeline", None)
    if p is None:
        raise HTTPException(status_code=503, detail="ingestion pipeline not initialized")
    return p


def get_redis_lock(request: Request) -> RedisLock:
    """Strict dep: read redis lock from app.state. 503 if uninitialized."""
    lock = getattr(request.app.state, "redis_lock", None)
    if lock is None:
        raise HTTPException(status_code=503, detail="redis lock not initialized")
    return lock


def get_task_repo(request: Request) -> TaskRepo:
    """Strict dep: read task repo from app.state. 503 if uninitialized."""
    repo = getattr(request.app.state, "task_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="task repo not initialized")
    return repo


@router.post("/notify", status_code=202)
async def notify(
    notification: IngestionNotification,
    background_tasks: BackgroundTasks,
    request: Request,
    pipeline: IngestionPipeline = Depends(get_pipeline),
    lock: RedisLock = Depends(get_redis_lock),
    repo: TaskRepo = Depends(get_task_repo),
    _auth: None = Depends(require_parser_token),
):
    """Accept parser notification and queue async ingestion.

    Idempotency: same (trace_id, doc_hash, version) → 202 duplicate.
    Distributed lock: same doc_hash in-flight elsewhere → 202 in_flight.
    """
    doc_hash = notification.doc_hash
    version = notification.version
    request_id = request_id_from_trace(
        notification.trace_id or "", doc_hash, version
    )

    # P0.2: reject output_path that escapes SHARED_STORAGE_PATH
    storage_root: Path = request.app.state.shared_storage_root
    try:
        candidate = Path(notification.output_path).resolve(strict=False)
        candidate.relative_to(storage_root)
    except (ValueError, OSError):
        raise HTTPException(
            status_code=400,
            detail="output_path must be an absolute subdirectory of SHARED_STORAGE_PATH",
        )

    # Distributed lock FIRST: if another pod is processing this doc_hash,
    # return "in_flight" without touching the tasks table (no PENDING row
    # left stranded for the local compensation scanner to clean up).
    lock_key = f"lock:ingest:{doc_hash}"
    token = await lock.acquire(lock_key, ttl_sec=settings.LOCK_TTL_SEC)
    if token is None:
        logger.info("Lock held for %s; another pod is processing", doc_hash)
        from ekrs_rag.observability.audit import get_writer

        writer = get_writer()
        if writer is not None:
            writer.write(
                "lock_acquire_failed",
                lock_key=lock_key,
                request_id=request_id,
                doc_id=doc_hash,
            )
        return {"status": "in_flight", "doc_hash": doc_hash, "version": version}

    try:
        # Idempotency: UNIQUE constraint → already processed. Released lock
        # because we hold the lock but won't be doing background work.
        if not repo.try_insert(request_id, doc_hash):
            logger.info("Duplicate notify (idempotent): %s", request_id)
            await lock.release(lock_key, token)
            return {"status": "duplicate", "doc_hash": doc_hash, "version": version}
    except Exception:
        await lock.release(lock_key, token)
        raise

    # Phase 6A (A1) / Q1: extract doc_metadata from notification and persist
    # via DocumentRepo. Parser populates notification.metadata with
    # {doc_id, type, scope_path, status}. If absent, skip silently (back-compat
    # with pre-A1 payloads). On write failure, soft-fail with audit warning —
    # never block ingestion.
    _doc_meta = (notification.metadata or {}).get("doc_metadata")
    _repo_doc = getattr(request.app.state, "document_repo", None)
    if _doc_meta is not None and _repo_doc is not None:
        try:
            _repo_doc.insert(Document(
                doc_id=_doc_meta["doc_id"],
                doc_type=_doc_meta.get("type", "unknown"),
                scope_path=_doc_meta.get("scope_path", ""),
                status=_doc_meta.get("status", "active"),
                created_at=time.time(),
            ))
        except Exception as _e:
            logger.warning("document_metadata_extraction_failed: %s", _e)
            try:
                from ekrs_rag.observability.audit import get_writer as _gw
                _writer = _gw()
                if _writer is not None:
                    _writer.write(
                        "document_metadata_failed",
                        request_id=getattr(request.state, "request_id", "unknown"),
                        doc_id=str(_doc_meta.get("doc_id", "?")),
                        error=str(_e),
                    )
            except Exception:
                pass  # audit best-effort

    async def _locked_ingest() -> None:
        await _run_locked_ingest(
            pipeline=pipeline,
            repo=repo,
            lock=lock,
            lock_key=lock_key,
            lock_token=token,
            notification=notification,
            request_id=request_id,
        )

    background_tasks.add_task(_locked_ingest)
    return {"status": "queued", "doc_hash": doc_hash, "version": version}


@router.get("/status/{doc_hash}", response_model=IngestionStatus)
async def get_status(
    doc_hash: str,
    pipeline: IngestionPipeline = Depends(get_pipeline),
):
    """Query ingestion status for a document."""
    status = pipeline._qdrant.get_ingestion_status(doc_hash)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No ingestion record for {doc_hash}")

    return status


class IngestionReplayRequest(BaseModel):
    """POST /v1/ingestion/replay body."""
    request_id: str
    replayed_by: str  # ops user / trace id


@router.post("/replay")
async def replay_ingestion(
    req: IngestionReplayRequest,
    repo: TaskRepo = Depends(get_task_repo),
    pipeline: IngestionPipeline = Depends(get_pipeline),
    _auth: None = Depends(require_parser_token),
):
    """Replay a completed ingestion by request_id.

    Re-runs parse+chunk+upsert for an already-indexed document. Does NOT
    trigger parser callback. Rejects in-flight, failed, and pre-Phase-5
    (NULL source_path) tasks with 409.
    """
    # Lazy imports for audit (writers may not be initialized in tests).
    from ekrs_rag.observability.audit import get_writer

    row = repo.get(req.request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request_id not found")
    if row["status"] in ("PENDING", "RUNNING"):
        raise HTTPException(status_code=409, detail={"reason": "in_flight"})
    if row["status"] != "COMPLETED":
        raise HTTPException(status_code=409, detail={"reason": "not_completed"})

    source_path = row.get("source_path")
    if not source_path:
        raise HTTPException(status_code=409, detail={"reason": "pre_phase5"})

    expected_sha = row.get("payload_sha256")
    jsonl_path = Path(source_path)
    if not jsonl_path.exists():
        raise HTTPException(status_code=409, detail={"reason": "file_missing"})

    actual_sha = hashlib.sha256(jsonl_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        writer = get_writer()
        if writer:
            writer.write(
                "ingestion_replay_sha256_mismatch",
                request_id=req.request_id,
                expected_sha256=expected_sha or "",
                actual_sha256=actual_sha,
            )
        raise HTTPException(status_code=409, detail={"reason": "sha256_mismatch"})

    # Audit started (best-effort: writer may be None in tests).
    writer = get_writer()
    if writer:
        writer.write(
            "ingestion_replay_started",
            request_id=req.request_id,
            replayed_by=req.replayed_by,
            source_path=source_path,
        )

    # Re-run ingestion (no callback, no idempotency skip).
    start = time.monotonic()
    try:
        chunks_written = await pipeline.replay(
            jsonl_path=jsonl_path,
            doc_hash=row["doc_id"],
            version=row.get("version", 1),
        )
        duration_ms = int((time.monotonic() - start) * 1000)
    except Exception as e:
        logger.error("Replay failed for %s: %s", req.request_id, e)
        raise HTTPException(status_code=500, detail=f"replay failed: {e}")

    if writer:
        writer.write(
            "ingestion_replay_completed",
            request_id=req.request_id,
            sha256_match=True,
            duration_ms=duration_ms,
            chunks_written=chunks_written,
        )

    return {
        "request_id": req.request_id,
        "status": "completed",
        "chunks_written": chunks_written,
        "duration_ms": duration_ms,
    }
