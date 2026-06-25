"""Ingestion API routes.

POST /v1/ingestion/notify — accept parser notification, queue ingestion
GET /v1/ingestion/status/{doc_hash} — query ingestion status
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from ekrs_shared.idempotency import request_id_from_trace
from ekrs_shared.models import IngestionStatus, IngestionNotification

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

    # Idempotency: UNIQUE constraint → already processed
    if not _repo.try_insert(request_id, doc_hash):
        logger.info("Duplicate notify (idempotent): %s", request_id)
        return {"status": "duplicate", "doc_hash": doc_hash, "version": version}

    # Distributed lock: prevent concurrent ingestion of same doc
    lock_key = f"lock:ingest:{doc_hash}"
    token = await _lock.acquire(lock_key, ttl_sec=settings.LOCK_TTL_SEC)
    if token is None:
        logger.warning("Lock held for %s, mark PENDING for compensation", doc_hash)
        return {"status": "in_flight", "doc_hash": doc_hash, "version": version}

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