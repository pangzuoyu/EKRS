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
from .api.routes import admin, calculate, constraints, ingestion, trace
from .concurrency.compensation import CompensationScanner
from .concurrency.redis_lock import RedisLock
from .core.config import settings
from .core.logging import setup_logging
from .ingestion.pipeline import IngestionPipeline
from .observability.audit import AuditWriter, attach_index, set_writer
from .observability.audit_index import AuditIndex
from .retrieval.embedding_service import EmbeddingService
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


async def _stub_compensation_handler(task: dict) -> bool:
    """Stub handler — returns False to signal no work done.

    Phase 7 T3 (Decision §5): handler now returns ``bool``. The stub
    returns False because no real re-ingest happened; the scanner marks
    the task FAILED with descriptive last_error.
    """
    logger.warning("Compensation handler not yet wired for %s", task["request_id"])
    return False


def _get_compensation_handler():
    """Lookup indirection so tests can monkeypatch this function to inject
    a real handler without rewriting main.py."""
    return _stub_compensation_handler

# Shared across app via module-level state
_qdrant: QdrantManager | None = None
_pipeline: IngestionPipeline | None = None
_retriever: EKRSRetriever | None = None
_audit_writer: AuditWriter | None = None
_audit_index: AuditIndex | None = None
_task_repo: TaskRepo | None = None
_doc_repo: DocumentRepo | None = None

# All audit event schemas required by spec §Audit (registered at startup).
# Phase 6A (D5 retro): the 2 optional fields (lineage_snapshot,
# conflict_details) are NOT in any event's required schema. They pass
# through `log_event`'s defensive spread via `_PHASE6A_OPTIONAL` in the
# shared audit base, so write-sites that pass them (D6, D7, calculate.py)
# don't need to re-register the schema. Phase 6A also registered
# `document_metadata_failed` (T2 soft-fail) — bringing total from 15→16.
_PHASE6A_FIELDS = frozenset({"lineage_snapshot", "conflict_details"})
_EVENT_SCHEMAS = {
    # 7 events whose write-sites may include the 2 optional Phase 6A fields:
    "constraint_solve_started": {"trace_id", "query"},
    "constraint_solved": {"trace_id", "branches_count"},
    "constraint_solve_failed": {"trace_id", "error_type"},
    "endpoint_started": {"trace_id", "endpoint", "method"},
    "endpoint_completed": {"trace_id", "status_code", "duration_ms"},
    "ingestion_received": {"request_id", "doc_id"},
    "ingestion_completed": {"request_id", "doc_id"},
    # 8 events unchanged from pre-6A (no fields added; emit-only or unrelated):
    "query_replay_executed": {"replayed_trace_id", "deterministic_match"},
    "ingestion_failed": {"request_id", "doc_id"},
    "ingestion_replay_started": {"request_id"},
    "ingestion_replay_completed": {"request_id"},
    "ingestion_replay_sha256_mismatch": {"request_id"},
    "compensation_retry": {"request_id", "reingest_outcome", "reingest_duration_ms"},
    "qdrant_write_failed": {"collection"},
    "lock_acquire_failed": {"lock_key"},
    # Phase 6A (T2 soft-fail audit): registered after Task 2 write-site was
    # added (c439d50) so audit-event invariant count becomes 16.
    "document_metadata_failed": {"request_id", "doc_id", "error"},
    # Doc-to-MD integration (T6/T9): callback best-effort audit events.
    # Emitted by IngestionPipeline when a callback branch fails; count → 19.
    "callback_url_blocked": {"doc_hash", "version", "reason"},
    "callback_auth_missing": {"doc_hash", "version"},
    "callback_best_effort_failed": {"doc_hash", "version", "rag_status", "error"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init logging, Qdrant, embedding service, retriever, ingestion pipeline,
    redis, task_repo, compensation scanner, AuditWriter, AuditIndex."""
    global _qdrant, _pipeline, _retriever
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

        storage_root = settings.SHARED_STORAGE_PATH
        if not storage_root.is_dir():
            raise RuntimeError(
                f"SHARED_STORAGE_PATH={storage_root} does not exist; "
                "create the directory or fix the config before starting."
            )
        app.state.shared_storage_root = storage_root.resolve()

        # T3 defense-in-depth: refuse to boot if PARSER_TOKEN is missing
        # or shorter than 32 chars. Validator in core/config.py already
        # rejects the placeholder default at Settings() time, but a second
        # gate here catches operator overrides (e.g., empty string, 8-char
        # rotation candidate) that bypass the validator (rotations happen
        # via PARSER_TOKEN rotation endpoint, not module re-import).
        if not settings.PARSER_TOKEN or len(settings.PARSER_TOKEN) < 32:
            raise RuntimeError(
                "PARSER_TOKEN is missing or shorter than 32 chars; "
                "set PARSER_TOKEN in .env before starting."
            )

        embedding_service = None
        try:
            # Phase 6B D5: EmbeddingService facade wraps bge-m3 ONNX;
            # QdrantManager consumes it (no embedder arg on retriever).
            embedding_service = EmbeddingService()
            _qdrant = QdrantManager(
                host=settings.QDRANT_HOST,
                port=settings.QDRANT_PORT,
                collection_name=settings.COLLECTION_NAME,
                embedding_service=embedding_service,
                auto_reindex=settings.AUTO_REINDEX,
            )
            # D4: rebuild collection (if dim mismatch) in lifespan, before serve.
            # asyncio.to_thread: ensure_collection is sync; offload from event loop.
            await asyncio.to_thread(_qdrant.ensure_collection)
            logger.info("Qdrant collection ready: %s", settings.COLLECTION_NAME)
        except Exception as e:
            # Phase 6C T8 fix: make Qdrant/Embedding init non-fatal. The
            # metrics exporter sidecar above is independent of Qdrant and
            # must keep serving so Prometheus can scrape even when Qdrant
            # is unreachable. Routes depending on qdrant/retriever/pipeline
            # already 503 via get_retriever() (constraints.py:41).
            logger.warning(
                "Qdrant/Embedding init failed (%s) — service starts in "
                "degraded mode. Routes depending on Qdrant/retriever/pipeline "
                "will return 503 until Qdrant becomes reachable. Restart the "
                "service after fixing Qdrant to fully restore.",
                e,
            )
            _qdrant = None
            _retriever = None
            _pipeline = None
            embedding_service = None

        app.state.embedding_service = embedding_service
        app.state.qdrant_manager = _qdrant

        # Phase 2b: retriever (no longer takes embedder — qdrant.search
        # handles embedding internally via injected EmbeddingService).
        if _qdrant is not None:
            _retriever = EKRSRetriever(qdrant=_qdrant)
            app.state.retriever = _retriever
            _pipeline = IngestionPipeline(
                _qdrant,
                storage_path=app.state.shared_storage_root,
                parser_token=settings.PARSER_TOKEN,
                audit_writer=_audit_writer,  # D5: pass the lifespan-managed writer
            )
            app.state.pipeline = _pipeline
        else:
            app.state.retriever = None
            app.state.pipeline = None

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

        # Run compensation scan AFTER AuditWriter is set. The scanner emits
        # `compensation_retry` events for every retry path (handler_not_wired,
        # claim_race_lost, retry_invoked, handler_failed); previously this ran
        # before set_writer() so all events from the cold-start scan were
        # silently dropped. Moving it here ensures the events survive.
        retried = await _scanner.scan()
        logger.info("Compensation scan completed: retried=%d", retried)

        yield
    finally:
        # Always release the exporter, even when later startup work fails.
        # Use a new local name (not `httpd`) so mypy doesn't infer
        # WSGIServer from the earlier `start_http_server(...)` return.
        metrics_httpd = getattr(app.state, "metrics_httpd", None)
        if metrics_httpd is not None:
            metrics_httpd.shutdown()
            metrics_httpd.server_close()
        logger.info("Metrics exporter stopped")
        logger.info("Shutting down EKRS RAG service")


def create_app() -> FastAPI:
    """Build the FastAPI app. Exposed as a factory so tests can construct
    fresh app instances with isolated env vars."""
    app = FastAPI(
        title="EKRS RAG Service",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {"name": "ingestion", "description": "Parser callback + replay"},
            {"name": "constraints", "description": "Constraint solving API"},
            {"name": "calculate", "description": "Numerical calc endpoint"},
            {"name": "trace", "description": "Trace replay endpoint"},
            {"name": "admin", "description": "Operator recovery (X-Admin-Key)"},
        ],
    )
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(ingestion.router)
    app.include_router(constraints.router)
    app.include_router(trace.router)
    app.include_router(calculate.router)
    app.include_router(admin.router)

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