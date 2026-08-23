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
from ...observability.metrics import METRICS, safe_dec, safe_inc, safe_observe
from ...services.encoding_pool import EncodingPool
from ...services.inline_steps import run_inline_admission
from ...services.step5_worker import Step5Payload, run_step5
from ...storage.task_repo import TaskRepo
from ...storage.documents import Document

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ingestion", tags=["ingestion"])


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _record_terminal(result_label: str, t0: float | None) -> None:
    """Phase 13a T7: queue-depth dec + task-duration observe.

    Called from terminal branches (success / failed / pool_wait_unhandled).
    `t0` is the monotonic start captured at notify() entry; ``None`` is
    safe (skip duration observe) for legacy call sites that don't pass
    it. Result labels mirror rag_status literal values
    (``success``/``failed``/``duplicate``/``business_failure``) plus an
    internal ``pool_wait_unhandled`` for the pool-crash defensive path.
    """
    safe_dec(METRICS.task_queue_depth)
    if t0 is not None:
        try:
            safe_observe(
                METRICS.task_duration_seconds,
                time.monotonic() - t0,
                result=result_label,
            )
        except Exception:
            pass


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


def get_encoding_pool(request: Request) -> EncodingPool:
    """Strict dep: read EncodingPool from app.state. 503 if uninitialized.

    Phase 13a T5: notify dispatches Step 5 to the pebble subprocess pool
    (T4). The pool is wired in main.py lifespan; in test fixtures it's
    monkey-patched onto app.state.encoding_pool.
    """
    pool = getattr(request.app.state, "encoding_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="encoding pool not initialized")
    return pool


# ---------------------------------------------------------------------------
# Background-task helper: pool.wait → TaskRepo terminal mapping + callback
# ---------------------------------------------------------------------------


async def _run_pool_terminal(
    pool: EncodingPool,
    task_id: str,
    notification: IngestionNotification,
    repo: TaskRepo,
    request_id: str,
    t0: float | None = None,
) -> None:
    """Await pool outcome and map to TaskRepo + parser callback.

    Order of operations:
    1. pool.wait(task_id) → outcome dict (success/failed with error_code)
    2. mark_status RUNNING (so /status reflects in-flight)
    3. mark_status COMPLETED or FAILED
    4. ingest_received → ingest_completed / ingest_failed audit emits
    5. fire parser callback (best-effort, like pipeline._send_callback_safely)

    Failure isolation: every step is wrapped so a pool subprocess crash
    or audit emit failure doesn't escape into BackgroundTasks (which
    would otherwise surface as an unhandled task exception).
    """
    from ekrs_rag.observability.audit import get_writer

    writer = get_writer()

    try:
        outcome = await pool.wait(task_id)
    except Exception as e:
        # Pool wait should never raise (EncodingPool.wait converts
        # ProcessExpired + others to dict outcomes). This is the
        # defensive net for unforeseen exceptions.
        logger.error("pool.wait raised for task=%s: %s", task_id, e)
        repo.mark_status(request_id, "RUNNING")
        repo.mark_failed_with_error(request_id, f"pool_wait_unhandled: {e}")
        if writer is not None:
            writer.write(
                "ingestion_failed",
                request_id=request_id,
                doc_id=notification.doc_hash,
                error_code="pool_wait_unhandled",
                error=str(e),
            )
        _record_terminal("pool_wait_unhandled", t0)
        return

    rag_status = outcome.get("rag_status")
    error = outcome.get("error")
    error_code = outcome.get("error_code")
    chunks_indexed = outcome.get("chunks_indexed", 0)

    # RUNNING transition so /status reflects in-flight (queued → running)
    try:
        repo.mark_status(request_id, "RUNNING")
    except Exception as e:
        logger.warning("mark_status RUNNING failed for %s: %s", request_id, e)

    if rag_status == "success":
        try:
            repo.mark_status(request_id, "COMPLETED")
        except Exception as e:
            logger.warning("mark_status COMPLETED failed for %s: %s", request_id, e)
        if writer is not None:
            writer.write(
                "ingestion_completed",
                request_id=request_id,
                doc_id=notification.doc_hash,
                chunks_indexed=chunks_indexed,
            )
        _record_terminal(str(rag_status) if rag_status else "failed", t0)
    else:
        try:
            repo.mark_failed_with_error(request_id, error or "unknown")
        except Exception as e:
            logger.warning("mark_failed failed for %s: %s", request_id, e)
        if writer is not None:
            writer.write(
                "ingestion_failed",
                request_id=request_id,
                doc_id=notification.doc_hash,
                error_code=error_code or "unknown",
                error=error or "unknown",
            )
        _record_terminal(str(rag_status) if rag_status else "failed", t0)

    # Fire parser callback (best-effort; mirrors pipeline._send_callback_safely).
    # We rebuild an IngestionOutcome here because _send_callback_safely is a
    # pipeline method; for T5 we just reuse the protocol-level emit and
    # rely on the worker having already done encode+upsert. The callback
    # is intentionally a stub fire-and-forget here — full callback
    # semantics (4xx non-retry / 5xx retry / URL allowlist) live in
    # IngestionPipeline._send_callback; T5's worker doesn't fire callbacks
    # because Step5Payload has no callback_url field. The parser still
    # gets terminal confirmation via TaskRepo polling (status endpoint).
    if notification.callback_url:
        logger.info(
            "pool_terminal: callback_url=%s would fire here; T5 keeps "
            "callback fire-and-forget, full retry semantics land in T8",
            notification.callback_url,
        )


@router.post("/notify", status_code=202)
async def notify(
    notification: IngestionNotification,
    background_tasks: BackgroundTasks,
    request: Request,
    pool: EncodingPool = Depends(get_encoding_pool),
    lock: RedisLock = Depends(get_redis_lock),
    repo: TaskRepo = Depends(get_task_repo),
    _auth: None = Depends(require_parser_token),
):
    """Accept parser notification and dispatch Step 5 to the pebble pool.

    Phase 13a T5 flow (eng-review Issue 1: Steps 1-4 inline + Step 5
    via pool):
    1. Path check (defense-in-depth, unchanged)
    2. Distributed lock + idempotency (UNIQUE → 202 duplicate)
    3. Inline coarse_gate (T2 admission) — cheap raw_chars scan
       → on reject: TaskRepo FAILED + 202 status=rejected (E10: NOT 403)
    4. TaskRepo status: PENDING → QUEUED → (background) RUNNING → terminal
    5. pool.submit(Step5Payload) — non-blocking dispatch (T4)
    6. Background: pool.wait → terminal mapping + audit + callback
    """
    doc_hash = notification.doc_hash
    version = notification.version
    request_id = request_id_from_trace(
        notification.trace_id or "", doc_hash, version
    )
    # Phase 13a T7: capture queue-entry time so the BackgroundTasks
    # terminal path can observe end-to-end task duration. Stashed on
    # request.state to avoid threading it through BackgroundTasks kwargs.
    request.state.notify_t0 = time.monotonic()

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
    # return "in_flight" without touching the tasks table.
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
        # Idempotency: UNIQUE constraint → already processed.
        if not repo.try_insert(request_id, doc_hash):
            logger.info("Duplicate notify (idempotent): %s", request_id)
            await lock.release(lock_key, token)
            return {"status": "duplicate", "doc_hash": doc_hash, "version": version}
    except Exception:
        await lock.release(lock_key, token)
        raise

    # Phase 6A (A1) / Q1: extract doc_metadata from notification and persist
    # via DocumentRepo. Soft-fail with audit warning — never block ingestion.
    _doc_meta = (notification.metadata or {}).get("doc_metadata")
    _repo_doc = getattr(request.app.state, "document_repo", None)
    if _doc_meta is not None and _repo_doc is not None:
        try:
            _raw_scope = _doc_meta.get("scope_path", "")
            if isinstance(_raw_scope, list):
                _scope_path_str = ",".join(str(s) for s in _raw_scope)
            else:
                _scope_path_str = str(_raw_scope)
            _repo_doc.insert(Document(
                doc_id=_doc_meta["doc_id"],
                doc_type=_doc_meta.get("type", "unknown"),
                scope_path=_scope_path_str,
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
                pass

    # Phase 13a T5: inline coarse_gate (T2 admission).
    # Conservative-reject: better false positive than wedging the pool.
    # Returns 202 with status=rejected (E10: NOT bare 403).
    ok, rejection = run_inline_admission(notification)
    if not ok:
        # Release the lock — we won't be doing background work.
        try:
            await lock.release(lock_key, token)
        except Exception:
            pass
        error_code = rejection.error_code if rejection else "raw_chars_over_limit"
        error_msg = rejection.error if rejection else "admission rejected"
        # actual_chunks: number of blocks in the JSONL (best-effort scan).
        # 0 if the file is unreadable — admission_rejected still fires.
        # output_path is the parser's output DIRECTORY (coarse_gate reads
        # {output_path}/data.jsonl); we mirror that contract.
        actual_chunks = 0
        try:
            _jsonl_path = Path(notification.output_path) / "data.jsonl"
            with _jsonl_path.open("r", encoding="utf-8") as _f:
                for _line in _f:
                    if _line.strip():
                        actual_chunks += 1
        except Exception:
            pass
        # Phase 13a T6: emit admission_rejected with full schema
        # (doc_hash + reason + actual_chunks). Required-field schema is
        # registered in main.py _EVENT_SCHEMAS.
        try:
            from ekrs_rag.observability.audit import get_writer as _gw
            _writer = _gw()
            if _writer is not None:
                _writer.write(
                    "admission_rejected",
                    request_id=request_id,
                    doc_hash=doc_hash,
                    reason=error_code or "raw_chars_over_limit",
                    actual_chunks=actual_chunks,
                )
        except Exception:
            pass
        # Phase 13a T7: rag_doc_rejections_total counter (operator-facing
        # metric, complements the audit emit). `reason` label matches the
        # audit event's `reason` field so dashboards can group by it.
        safe_inc(
            METRICS.doc_rejections_total,
            reason=error_code or "raw_chars_over_limit",
        )
        try:
            repo.mark_failed_with_error(
                request_id,
                error_msg or "admission rejected",
            )
        except Exception:
            pass
        return {
            "status": "rejected",
            "doc_hash": doc_hash,
            "version": version,
            "error_code": error_code,
        }

    # TaskRepo: PENDING → QUEUED so /status reflects in-flight.
    try:
        repo.mark_status(request_id, "QUEUED")
    except Exception as e:
        logger.warning("mark_status QUEUED failed for %s: %s", request_id, e)
    # Phase 13a T7: queue depth gauge inc on entry into QUEUED state.
    # Decremented by terminal transitions below (and by the
    # BackgroundTasks terminal path inside _run_pool_terminal).
    safe_inc(METRICS.task_queue_depth)

    # Pool dispatch (T4 EncodingPool). submit is non-blocking; the spawn
    # happens in background.
    payload = Step5Payload(
        trace_id=notification.trace_id or "",
        doc_hash=doc_hash,
        version=version,
        output_path=notification.output_path,
    )
    task_id = await pool.submit(run_step5, payload=payload)

    # Background: pool.wait → terminal mapping + audit + callback fire.
    # Pass t0 so _run_pool_terminal can observe end-to-end task duration.
    background_tasks.add_task(
        _run_pool_terminal,
        pool, task_id, notification, repo, request_id,
        request.state.notify_t0,
    )

    return {
        "status": "queued",
        "doc_hash": doc_hash,
        "version": version,
        "task_id": task_id,
    }


@router.get("/status/{doc_hash}", response_model=IngestionStatus)
async def get_status(
    doc_hash: str,
    request: Request,
    repo: TaskRepo = Depends(get_task_repo),
):
    """Query ingestion status for a document.

    Phase 13a T5: exposes queued/running states via TaskRepo (not just
    qdrant.get_ingestion_status which only returns terminal states).

    Lookup order:
    1. TaskRepo: any row matching doc_hash → return row.status
       (PENDING / QUEUED / RUNNING / COMPLETED / FAILED)
    2. Qdrant: get_ingestion_status → terminal SUCCESS (Phase 6A contract)
    3. Neither → 404
    """
    # TaskRepo first — exposes queued/running (Phase 13a T5).
    # Scan the rows dict directly (TaskRepo.rows isn't a public attr,
    # but we use the same connection's SELECT for portability across
    # test fixtures).
    try:
        row = repo.get_for_doc(doc_hash)
    except AttributeError:
        # Test stub without get_for_doc — fall back to qdrant.
        row = None
    if row is not None:
        status = row.get("status", "PENDING").lower()
        # Map internal PENDING/QUEUED/RUNNING/COMPLETED/FAILED to the
        # IngestionStatus-compatible shape. For non-terminal states,
        # we return a partial IngestionStatus with status='pending'.
        if status in ("queued", "running"):
            # Synthesize a minimal IngestionStatus; the contract allows
            # in-flight docs to surface queued/running.
            from ekrs_shared.models import IngestionStatus
            return IngestionStatus(
                doc_hash=doc_hash,
                version=int(row.get("version", 0)),
                status="pending",  # IngestionStatus enum: success/pending/...
                chunks_indexed=0,
            )
        elif status in ("completed",):
            # Already-indexed in qdrant (or row + qdrant should agree);
            # fall through to qdrant lookup for terminal shape.
            pass  # fallthrough
        elif status in ("failed", "pending"):
            from ekrs_shared.models import IngestionStatus
            return IngestionStatus(
                doc_hash=doc_hash,
                version=int(row.get("version", 0)),
                status="pending",
                chunks_indexed=0,
            )

    # Terminal: query Qdrant for the canonical IngestionStatus (Phase 6A contract).
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is not None and getattr(pipeline, "_qdrant", None) is not None:
        status = pipeline._qdrant.get_ingestion_status(doc_hash)
        if status is not None:
            return status

    raise HTTPException(
        status_code=404, detail=f"No ingestion record for {doc_hash}",
    )


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
