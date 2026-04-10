"""Ingestion API routes.

POST /v1/ingestion/notify — accept parser notification, queue ingestion
GET /v1/ingestion/status/{doc_hash} — query ingestion status
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from ekrs_shared.models import IngestionNotification, IngestionStatus

from ...core.config import settings
from ...ingestion.pipeline import IngestionPipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])

# Set by main.py lifespan
_pipeline: IngestionPipeline | None = None


def set_pipeline(pipeline: IngestionPipeline) -> None:
    """Inject the ingestion pipeline instance (called at startup)."""
    global _pipeline
    _pipeline = pipeline


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

    Returns 202 on success, 403 on bad token, 422 on bad payload.
    """
    _validate_token(x_parser_token)

    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    logger.info("Received notify: doc=%s v=%d", notification.doc_hash, notification.version)

    background_tasks.add_task(_pipeline.ingest, notification)

    return {"status": "queued", "doc_hash": notification.doc_hash, "version": notification.version}


@router.get("/status/{doc_hash}", response_model=IngestionStatus)
async def get_status(doc_hash: str):
    """Query ingestion status for a document."""
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    status = _pipeline._qdrant.get_ingestion_status(doc_hash)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No ingestion record for {doc_hash}")

    return status
