"""EKRS RAG FastAPI application.

Entry point for the RAG service.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from .api.routes import constraints, ingestion, metrics
from .concurrency.compensation import CompensationScanner
from .concurrency.redis_lock import RedisLock
from .core.config import settings
from .core.logging import setup_logging
from .ingestion.pipeline import IngestionPipeline
from .retrieval.embedder import BGESmallEmbedder
from .retrieval.qdrant_client import QdrantManager
from .retrieval.retriever import EKRSRetriever
from .storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)

# Shared across app via module-level state
_qdrant: QdrantManager | None = None
_pipeline: IngestionPipeline | None = None
_embedder: BGESmallEmbedder | None = None
_retriever: EKRSRetriever | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init logging, Qdrant collection, embedder, retriever, ingestion pipeline."""
    global _qdrant, _pipeline, _embedder, _retriever

    setup_logging(debug=settings.EKRS_DEBUG)
    logger.info("Starting EKRS RAG service (debug=%s)", settings.EKRS_DEBUG)

    _qdrant = QdrantManager(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
        collection_name=settings.COLLECTION_NAME,
        vector_size=384,  # bge-small is 384d
    )

    try:
        _qdrant.ensure_collection(vector_size=384)
        logger.info("Qdrant collection ready: %s", settings.COLLECTION_NAME)
    except Exception as e:
        logger.error("Qdrant connection failed: %s", e)
        # Don't crash — endpoints will return 503

    # Phase 2b: init embedder + retriever
    _embedder = BGESmallEmbedder()
    _retriever = EKRSRetriever(qdrant=_qdrant, embedder=_embedder)

    # Wire up constraints router
    constraints.set_retriever(_retriever)

    # Store on app.state for route access
    app.state.embedder = _embedder
    app.state.retriever = _retriever

    _pipeline = IngestionPipeline(_qdrant, settings.SHARED_STORAGE_PATH)
    ingestion.set_pipeline(_pipeline)

    # Phase 4: redis, task_repo, lock, compensation
    _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    _redis_lock = RedisLock(_redis)
    _task_repo = TaskRepo(db_path=settings.TASK_DB_PATH)
    _task_repo.init()
    app.state.redis = _redis
    app.state.redis_lock = _redis_lock
    app.state.task_repo = _task_repo
    ingestion.set_redis_lock(_redis_lock)
    ingestion.set_task_repo(_task_repo)

    async def _compensation_handler(task: dict) -> None:
        """重试入队: 重新触发 ingest (需 pipeline 支持重试入口)."""
        # TODO: wire to IngestionPipeline.ingest via callback_url (Task 7)
        logger.warning("Compensation handler not yet wired for %s", task["request_id"])

    _scanner = CompensationScanner(
        task_repo=_task_repo,
        handler=_compensation_handler,
        max_attempts=settings.MAX_ATTEMPTS,
        threshold_sec=60.0,
    )
    retried = await _scanner.scan()
    logger.info("Compensation scan completed: retried=%d", retried)

    yield

    logger.info("Shutting down EKRS RAG service")


app = FastAPI(
    title="EKRS RAG Service",
    version="0.1.0",
    lifespan=lifespan,
)

# Routes
app.include_router(ingestion.router)
app.include_router(metrics.router)
app.include_router(constraints.router)


@app.get("/health", response_class=PlainTextResponse)
async def health():
    """Health check endpoint."""
    return "ok"


def run():
    """Run with uvicorn when called as python -m ekrs_rag.main."""
    import uvicorn
    uvicorn.run(
        "ekrs_rag.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.EKRS_DEBUG,
    )


if __name__ == "__main__":
    run()
