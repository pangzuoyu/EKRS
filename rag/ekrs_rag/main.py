"""EKRS RAG FastAPI application.

Entry point for the RAG service.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from .api.middleware.observability import ObservabilityMiddleware
from .api.routes import constraints, ingestion, metrics
from .concurrency.compensation import CompensationScanner
from .concurrency.redis_lock import RedisLock
from .core.config import settings
from .core.logging import setup_logging
from .ingestion.pipeline import IngestionPipeline
from .observability.audit import AuditWriter, attach_index, set_writer
from .observability.audit_index import AuditIndex
from .retrieval.embedder import BGESmallEmbedder
from .retrieval.qdrant_client import QdrantManager
from .retrieval.retriever import EKRSRetriever
from .storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)

# Phase 4.5: real handler will be wired to IngestionPipeline.ingest via
# callback_url (per Task 7). Until then, the stub handler in lifespan() does
# no work, so the scanner must NOT claim-and-bump attempts for orphan rows;
# it must mark them unwired-skipped so attempts stays at 0 and the audit
# trail reflects that the work never ran.
COMPENSATION_HANDLER_IMPLEMENTED = False


async def _stub_compensation_handler(task: dict) -> None:
    """重试入队: 重新触发 ingest (需 pipeline 支持重试入口)."""
    # TODO: wire to IngestionPipeline.ingest via callback_url (Task 7)
    logger.warning("Compensation handler not yet wired for %s", task["request_id"])


def _get_compensation_handler():
    """Lookup indirection so tests can monkeypatch this function to inject
    a real handler without rewriting main.py."""
    return _stub_compensation_handler

# Shared across app via module-level state
_qdrant: QdrantManager | None = None
_pipeline: IngestionPipeline | None = None
_embedder: BGESmallEmbedder | None = None
_retriever: EKRSRetriever | None = None
_audit_writer: AuditWriter | None = None
_audit_index: AuditIndex | None = None
_task_repo: TaskRepo | None = None

# All 15 audit event schemas required by spec §Audit (registered at startup).
_EVENT_SCHEMAS = {
    "endpoint_started": {"trace_id", "endpoint", "method"},
    "endpoint_completed": {"trace_id", "status_code", "duration_ms"},
    "constraint_solve_started": {"trace_id", "query"},
    "constraint_solved": {"trace_id", "branches_count"},
    "constraint_solve_failed": {"trace_id", "error_type"},
    "query_replay_executed": {"replayed_trace_id", "deterministic_match"},
    "ingestion_received": {"request_id", "doc_id"},
    "ingestion_completed": {"request_id", "doc_id"},
    "ingestion_failed": {"request_id", "doc_id"},
    "ingestion_replay_started": {"request_id"},
    "ingestion_replay_completed": {"request_id"},
    "ingestion_replay_sha256_mismatch": {"request_id"},
    "compensation_retry": {"request_id"},
    "qdrant_write_failed": {"collection"},
    "lock_acquire_failed": {"lock_key"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init logging, Qdrant, embedder, retriever, ingestion pipeline,
    redis, task_repo, compensation scanner, AuditWriter, AuditIndex."""
    global _qdrant, _pipeline, _embedder, _retriever
    global _audit_writer, _audit_index, _task_repo

    setup_logging(debug=settings.EKRS_DEBUG, debug_log_path=settings.DEBUG_LOG_PATH)
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

    # handler lookup is dynamic so tests can patch compensation_handler.
    handler = _get_compensation_handler()
    _scanner = CompensationScanner(
        task_repo=_task_repo,
        handler=handler,
        max_attempts=settings.MAX_ATTEMPTS,
        threshold_sec=60.0,
        handler_is_wired=COMPENSATION_HANDLER_IMPLEMENTED,
    )
    app.state.compensation_scanner = _scanner
    retried = await _scanner.scan()
    logger.info("Compensation scan completed: retried=%d", retried)

    # Phase 5: observability wiring
    audit_path = settings.AUDIT_LOG_PATH
    Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
    _audit_writer = AuditWriter(audit_path)
    for event_type, required in _EVENT_SCHEMAS.items():
        _audit_writer.register_event_schema(event_type, required)
    set_writer(_audit_writer)
    app.state.audit_writer = _audit_writer

    # Build audit index async (don't block readiness on multi-GB scan)
    _audit_index = AuditIndex(audit_path)
    await asyncio.to_thread(_audit_index.build)
    constraints.set_audit_index(_audit_index)
    attach_index(_audit_index)
    app.state.audit_index = _audit_index

    yield

    logger.info("Shutting down EKRS RAG service")


def create_app() -> FastAPI:
    """Build the FastAPI app. Exposed as a factory so tests can construct
    fresh app instances with isolated env vars."""
    app = FastAPI(
        title="EKRS RAG Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(ingestion.router)
    app.include_router(metrics.router)
    app.include_router(constraints.router)

    @app.get("/health", response_class=PlainTextResponse)
    async def health():
        """Plain liveness probe (kept for backwards compatibility)."""
        return "ok"

    @app.get("/healthz")
    async def healthz():
        """Readiness probe: reports audit log + index health."""
        audit_path = Path(settings.AUDIT_LOG_PATH)
        writable = audit_path.exists() and os.access(audit_path, os.W_OK)
        index_loaded = _audit_index is not None
        return JSONResponse(
            status_code=200 if (writable and index_loaded) else 503,
            content={
                "audit_log_writable": writable,
                "audit_index_loaded": index_loaded,
                "audit_index_size": _audit_index.size if _audit_index else 0,
                "audit_index_load_seconds": _audit_index.load_seconds if _audit_index else 0.0,
                "task_repo_initialized": _task_repo is not None,
            },
        )

    return app


app = create_app()


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