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
from prometheus_client import CollectorRegistry, multiprocess, start_http_server

from .api.middleware.observability import ObservabilityMiddleware
from .api.routes import constraints, ingestion, trace
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
from .storage.documents import DocumentRepo

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
_doc_repo: DocumentRepo | None = None

# All audit event schemas required by spec §Audit (registered at startup).
# Phase 6A (D6): the 2 optional fields (lineage_snapshot + conflict_details)
# are added ONLY to events that carry them at write time. The other events
# still register without the new fields. Phase 6A also registered
# `document_metadata_failed` (T2 soft-fail) — bringing total from 15→16.
_PHASE6A_FIELDS = frozenset({"lineage_snapshot", "conflict_details"})
_EVENT_SCHEMAS = {
    # 7 events that carry Phase 6A fields (write-site can include them):
    "constraint_solve_started": {"trace_id", "query"} | _PHASE6A_FIELDS,
    "constraint_solved": {"trace_id", "branches_count"} | _PHASE6A_FIELDS,
    "constraint_solve_failed": {"trace_id", "error_type"} | _PHASE6A_FIELDS,
    "endpoint_started": {"trace_id", "endpoint", "method"} | _PHASE6A_FIELDS,
    "endpoint_completed": {"trace_id", "status_code", "duration_ms"} | _PHASE6A_FIELDS,
    "ingestion_received": {"request_id", "doc_id"} | _PHASE6A_FIELDS,
    "ingestion_completed": {"request_id", "doc_id"} | _PHASE6A_FIELDS,
    # 8 events unchanged from pre-6A (no fields added; emit-only or unrelated):
    "query_replay_executed": {"replayed_trace_id", "deterministic_match"},
    "ingestion_failed": {"request_id", "doc_id"},
    "ingestion_replay_started": {"request_id"},
    "ingestion_replay_completed": {"request_id"},
    "ingestion_replay_sha256_mismatch": {"request_id"},
    "compensation_retry": {"request_id"},
    "qdrant_write_failed": {"collection"},
    "lock_acquire_failed": {"lock_key"},
    # Phase 6A (T2 soft-fail audit): registered after Task 2 write-site was
    # added (c439d50) so audit-event invariant count becomes 16.
    "document_metadata_failed": {"request_id", "doc_id", "error"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init logging, Qdrant, embedder, retriever, ingestion pipeline,
    redis, task_repo, compensation scanner, AuditWriter, AuditIndex."""
    global _qdrant, _pipeline, _embedder, _retriever
    global _audit_writer, _audit_index, _task_repo, _doc_repo

    # ---- Phase 5.5 D: sidecar metrics exporter ----
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    # 0.0.0.0 allows cross-container scraping in docker-compose. For local
    # development without Docker, set METRICS_HOST=127.0.0.1 to limit exposure.
    metrics_host = os.environ.get("METRICS_HOST", "0.0.0.0")
    metrics_port = int(os.environ.get("METRICS_PORT", "9090"))

    setup_logging(debug=settings.EKRS_DEBUG, debug_log_path=settings.DEBUG_LOG_PATH)

    exporter_registry = None
    if multiproc_dir:
        # PROMETHEUS_MULTIPROC_DIR must be created and emptied by deployment
        # before Python starts: MmapedValue opens its files at import time, so a
        # missing directory fails before this lifespan runs. It must also be
        # wiped between restarts because stale files inflate merged metrics.
        # In multi-worker deployments, only one process may bind METRICS_PORT;
        # workers otherwise only write files for the shared collector.
        p = Path(multiproc_dir)
        if not p.is_dir():
            raise RuntimeError(
                f"PROMETHEUS_MULTIPROC_DIR={p} does not exist. "
                "Create the directory before starting the process "
                "(MmapedValue opens .db files at import-time)."
            )
        p.mkdir(parents=True, exist_ok=True)
        exporter_registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(exporter_registry)

    try:
        if exporter_registry is not None:
            httpd, _ = start_http_server(
                metrics_port, addr=metrics_host, registry=exporter_registry
            )
        else:
            httpd, _ = start_http_server(metrics_port, addr=metrics_host)
        app.state.metrics_httpd = httpd
        logger.info(
            "Metrics exporter listening on %s:%d", metrics_host, metrics_port
        )
    except OSError as e:
        # Another worker or process may already own the exporter port. Only one
        # process binds it; other workers continue writing multiprocess files.
        logger.warning(
            "Metrics exporter bind failed on %s:%d (%s) — assuming another "
            "process owns the port; continuing without local exporter",
            metrics_host,
            metrics_port,
            e,
        )
        app.state.metrics_httpd = None

    try:
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

        # retriever wired to app.state below; get_retriever dep reads it
        app.state.embedder = _embedder
        app.state.retriever = _retriever

        _pipeline = IngestionPipeline(_qdrant, settings.SHARED_STORAGE_PATH)
        app.state.pipeline = _pipeline

        # Phase 4: redis, task_repo, lock, compensation
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        _redis_lock = RedisLock(_redis)
        _task_repo = TaskRepo(db_path=settings.TASK_DB_PATH)
        _task_repo.init()
        app.state.redis = _redis
        app.state.redis_lock = _redis_lock
        app.state.task_repo = _task_repo

        # Phase 6A: DocumentRepo for spec §4 metadata tables
        _doc_repo = DocumentRepo(db_path=settings.DOCUMENTS_DB_PATH)
        _doc_repo.init()
        app.state.document_repo = _doc_repo

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

        def _on_audit_rollover() -> None:
            """Rebuild AuditIndex byte offsets after audit.log rotates.

            Reads the module-level `_audit_index` lazily — the index is
            initialized further down, but rotation only fires after the
            file exceeds 100 MB, well after startup completes.
            """
            global _audit_index
            if _audit_index is not None:
                try:
                    _audit_index.build()
                    logger.info("audit_index rebuilt after rollover")
                except Exception:
                    logger.exception("audit_index rebuild failed after rollover")

        _audit_writer = AuditWriter(audit_path, on_rollover=_on_audit_rollover)
        for event_type, required in _EVENT_SCHEMAS.items():
            _audit_writer.register_event_schema(event_type, required)
        set_writer(_audit_writer)
        app.state.audit_writer = _audit_writer

        # Build audit index async (don't block readiness on multi-GB scan)
        _audit_index = AuditIndex(audit_path)
        try:
            await asyncio.to_thread(_audit_index.build)
        except Exception as e:
            logger.warning(
                "AuditIndex build failed (replay will be unavailable): %s", e
            )
            _audit_index = None
        if _audit_index is not None:
            attach_index(_audit_index)
        app.state.audit_index = _audit_index

        yield
    finally:
        # Always release the exporter, even when later startup work fails.
        httpd = getattr(app.state, "metrics_httpd", None)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        logger.info("Metrics exporter stopped")
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
    app.include_router(constraints.router)
    app.include_router(trace.router)

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