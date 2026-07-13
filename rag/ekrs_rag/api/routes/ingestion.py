"""Ingestion API routes.

POST /v1/ingestion/notify — accept parser notification, queue ingestion
GET /v1/ingestion/status/{doc_hash} — query ingestion status
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ekrs_shared.idempotency import request_id_from_trace
from ekrs_shared.models import IngestionStatus, IngestionNotification

from ..auth import require_parser_token

from ...concurrency.redis_lock import RedisLock
from ...core.config import settings
from ...ingestion.pipeline import IngestionPipeline
from ...storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])

# Set by main.py lifespan
_pipeline: IngestionPipeline | None = None
_lock: RedisLock | None = None
_repo: TaskRepo | None = None


def set_pipeline(pipeline: IngestionPipeline) -> None:
    """Inject the ingestion pipeline instance (called at startup)."""
    global _pipeline
    _pipeline = pipeline


def set_redis_lock(lock: RedisLock) -> None:
    """Inject the distributed lock instance (called at startup)."""
    global _lock
    _lock = lock


def set_task_repo(repo: TaskRepo) -> None:
    """Inject the task repository instance (called at startup)."""
    global _repo
    _repo = repo


# ---------------------------------------------------------------------------
# Dependency functions
# ---------------------------------------------------------------------------


def get_pipeline(request: Request) -> IngestionPipeline:
    p = getattr(request.app.state, "pipeline", None)
    if p is None:
        raise HTTPException(status_code=503, detail="ingestion pipeline not initialized")
    return p


def get_redis_lock(request: Request) -> RedisLock:
    lock = getattr(request.app.state, "redis_lock", None)
    if lock is None:
        raise HTTPException(status_code=503, detail="redis lock not initialized")
    return lock


def get_task_repo(request: Request) -> TaskRepo:
    repo = getattr(request.app.state, "task_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="task repo not initialized")
    return repo


def _validate_token(token: str | None) -> None:
    """Timing-safe token validation."""
    if not token:
        raise HTTPException(status_code=403, detail="Missing X-Parser-Token")
    if not hmac.compare_digest(token, settings.PARSER_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


@router.post("/notify", status_code=202)
async def notify(
    notification: IngestionNotification,
    background_tasks: BackgroundTasks,
    x_parser_token: str | None = Header(None),
):
    """Accept parser notification and queue async ingestion.

    Idempotency: same (trace_id, doc_hash, version) → 202 duplicate.
    Distributed lock: same doc_hash in-flight elsewhere → 202 in_flight.
    """
    _validate_token(x_parser_token)

    if _pipeline is None or _lock is None or _repo is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    doc_hash = notification.doc_hash
    version = notification.version
    request_id = request_id_from_trace(
        notification.trace_id or "", doc_hash, version
    )

    # Distributed lock FIRST: if another pod is processing this doc_hash,
    # return "in_flight" without touching the tasks table (no PENDING row
    # left stranded for the local compensation scanner to clean up).
    lock_key = f"lock:ingest:{doc_hash}"
    token = await _lock.acquire(lock_key, ttl_sec=settings.LOCK_TTL_SEC)
    if token is None:
        logger.info("Lock held for %s; another pod is processing", doc_hash)
        return {"status": "in_flight", "doc_hash": doc_hash, "version": version}

    try:
        # Idempotency: UNIQUE constraint → already processed. Released lock
        # because we hold the lock but won't be doing background work.
        if not _repo.try_insert(request_id, doc_hash):
            logger.info("Duplicate notify (idempotent): %s", request_id)
            await _lock.release(lock_key, token)
            return {"status": "duplicate", "doc_hash": doc_hash, "version": version}
    except Exception:
        await _lock.release(lock_key, token)
        raise

    async def _locked_ingest() -> None:
        try:
            await _pipeline.ingest(notification)
            _repo.mark_status(request_id, "COMPLETED")
        except Exception as e:
            _repo.mark_status(request_id, "FAILED", error=str(e))
            raise
        finally:
            await _lock.release(lock_key, token)

    background_tasks.add_task(_locked_ingest)
    return {"status": "queued", "doc_hash": doc_hash, "version": version}


@router.get("/status/{doc_hash}", response_model=IngestionStatus)
async def get_status(doc_hash: str):
    """Query ingestion status for a document."""
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    status = _pipeline._qdrant.get_ingestion_status(doc_hash)
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
    request: Request,
    _auth: None = Depends(require_parser_token),
):
    """Replay a completed ingestion by request_id.

    Re-runs parse+chunk+upsert for an already-indexed document. Does NOT
    trigger parser callback. Rejects in-flight, failed, and pre-Phase-5
    (NULL source_path) tasks with 409.
    """
    # Lazy imports for audit (writers may not be initialized in tests).
    from ekrs_rag.observability.audit import get_writer

    task_repo = getattr(request.app.state, "task_repo", None) or _repo
    pipeline = _pipeline
    if task_repo is None:
        raise HTTPException(status_code=503, detail="task_repo not initialized")
    if pipeline is None:
        raise HTTPException(status_code=503, detail="pipeline not initialized")

    row = task_repo.get(req.request_id)
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
