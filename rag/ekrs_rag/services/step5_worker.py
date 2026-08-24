"""Phase 13a T3 — picklable Step5 worker fn for pebble subprocess dispatch.

pebble.ProcessPool serializes args + target fn via pickle. Two
constraints:
1. Target fn must be module-level (top-level callable, no closures).
2. Args must be picklable dataclasses (no live connections / async
   objects crossing the process boundary).

This module exports:
- ``Step5Payload`` (frozen dataclass) — picklable input
- ``run_step5(payload)`` — sync top-level fn; ``asyncio.run`` wrapper
- ``_step5_async(payload)`` — async coroutine with the actual work

Subprocess private state (no cross-process sharing):
- QdrantManager (constructed from Settings in subprocess)
- FTSManager (constructed from Settings in subprocess)
- aioredis client + RedisLock wrapper

The subprocess startup is heavyweight (~1-2s bge-m3 ONNX load). To
amortize, T4 EncodingPool calls ``_init_child`` once per worker which
pre-warms EmbeddingService. This module still constructs its own
QdrantManager per call (Qdrant clients are cheap; bge-m3 is the
expensive bit).

Single source of truth: ``_prepare_step5`` + ``_run_step5`` from
``services/step5_helpers.py`` (Pre-Task A). This module is a thin
subprocess-shaped adapter — no duplicate parse/chunk/upsert logic.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from ekrs_shared.models import IngestionNotification

from ..core.config import settings
from ..ingestion.outcome import IngestionOutcome
from .admission import chunk_gate
from .step5_helpers import _prepare_step5, _run_step5

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Encode backend seam — Phase 13c GPU channel hook (T9, eng-review Issue 5)
# ---------------------------------------------------------------------------


@runtime_checkable
class _EncodeBackend(Protocol):
    """Encode backend contract for Phase 13b/c GPU channel.

    Today, dense encoding happens inside ``QdrantManager.upsert_chunks``
    (CPU bge-m3 ONNX path). Phase 13b (separate plan
    ``docs/superpowers/plans/2026-08-23-phase13b-gpu-container.md``)
    replaces the default implementation with a torch FP16 GPU encoder;
    Phase 13c then wires the GPU container into the request path.

    Contract locks return shape as ``list[list[float]]`` so any 13b
    shape change (e.g. ``list[torch.Tensor]``) fails this Protocol's
    TypeGuard at runtime — caught by tests, not production.

    Subclassing a runtime_checkable Protocol with the matching
    ``__call__`` is enough to satisfy ``isinstance(x, _EncodeBackend)``.
    """

    def __call__(self, texts: list[str]) -> list[list[float]]: ...


def _encode_backend(texts: list[str]) -> list[list[float]]:
    """Default CPU bge-m3 ONNX encode → dense vectors only.

    Phase 13a T9: seam for Phase 13b GPU channel replacement. The
    function is the swap point — 13b replaces the body (or rebinds the
    module attribute) with a torch FP16 implementation. Today's caller
    path goes through ``QdrantManager.upsert_chunks``; introducing this
    seam at module level lets future wiring point here without changing
    the contract test surface.

    Sparse vectors are out of scope for this seam — Qdrant hybrid
    indexing still needs sparse, which lives in ``QdrantManager``
    (``to_qdrant_sparse``). The GPU channel contract covers the dense
    path only; sparse stays CPU until proven otherwise.
    """
    if not texts:
        return []
    from ..retrieval.embedding_service import EmbeddingService

    service = EmbeddingService()
    encoded = service.encode(texts)
    return [vec.dense for vec in encoded]


@dataclass(frozen=True)
class Step5Payload:
    """Picklable input for pebble subprocess dispatch.

    ``callback_url`` is intentionally absent — the worker does NOT call
    back to the parser; T5 notify handler is responsible for that (with
    proper 4xx/5xx retry semantics from pipeline._send_callback).
    """

    trace_id: str
    doc_hash: str
    version: int
    output_path: str


# ---------------------------------------------------------------------------
# Subprocess private factories (overridable for tests via monkeypatch)
# ---------------------------------------------------------------------------


def _build_qdrant() -> Any:
    """Construct QdrantManager in subprocess from Settings."""
    from ..retrieval.embedding_service import EmbeddingService
    from ..retrieval.qdrant_client import QdrantManager

    embedding_service = EmbeddingService()
    return QdrantManager(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
        collection_name=settings.COLLECTION_NAME,
        embedding_service=embedding_service,
        auto_reindex=settings.AUTO_REINDEX,
    )


def _build_fts() -> Any:
    """Construct FTSManager in subprocess from Settings."""
    from ..retrieval.fts_manager import FTSManager

    return FTSManager(
        db_path=Path(settings.FTS_DB_PATH),
        schema_version=2,
    )


def _build_redis_lock() -> Any:
    """Construct aioredis client + RedisLock wrapper in subprocess."""
    import redis.asyncio as aioredis
    from ..concurrency.redis_lock import RedisLock

    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return RedisLock(client)


# ---------------------------------------------------------------------------
# Top-level sync entry point (pebble picklable)
# ---------------------------------------------------------------------------


def run_step5(payload: Step5Payload) -> dict[str, Any]:
    """Sync top-level fn — pebble picks this up and runs in subprocess.

    Returns a picklable dict (IngestionOutcome is a dataclass with
    non-trivial fields; dict is the safest cross-version shape).
    """
    try:
        outcome: IngestionOutcome = asyncio.run(_step5_async(payload))
    except Exception as e:
        logger.error(
            "step5_worker_unhandled: %s\n%s",
            e, traceback.format_exc(),
        )
        return _outcome_to_dict(IngestionOutcome(
            rag_status="failed",
            error=str(e),
            error_code="worker_unhandled",
        ))
    return _outcome_to_dict(outcome)


# ---------------------------------------------------------------------------
# Async work — parse → chunk → chunk_gate → idempotent → RedisLock → encode
# ---------------------------------------------------------------------------


async def _step5_async(payload: Step5Payload) -> IngestionOutcome:
    """Async coroutine: do the work.

    Order matters:
    1. Build subprocess-private clients (no shared state across boundary)
    2. _prepare_step5 does parse + chunk + idempotent skip + classifier
    3. chunk_gate is a defense-in-depth check inside the worker
       (T5 notify handler runs coarse_gate; this is belt-and-suspenders)
    4. RedisLock wraps encode/upsert/fts (concurrent skip semantics)
    5. _run_step5 does the actual encode + qdrant upsert + fts write

    Audit emits are intentionally NOT done here — the worker is a thin
    adapter. T5 notify handler emits audit events (admission_rejected,
    callback outcome) where they belong.
    """
    qdrant = _build_qdrant()
    fts = _build_fts()
    redis_lock = _build_redis_lock()

    notification = IngestionNotification(
        trace_id=payload.trace_id,
        doc_hash=payload.doc_hash,
        version=payload.version,
        output_path=payload.output_path,
        callback_url="",  # worker does not call back; T5 handler does
    )

    # Step 1+2: prepare (parse + chunk + idempotent skip + classifier)
    prep = _prepare_step5(
        notification=notification,
        qdrant=qdrant,
        storage_root=Path(settings.SHARED_STORAGE_PATH).resolve(),
        audit_writer=None,
    )

    # Idempotent skip OR error short-circuit (helper already returned outcome)
    if prep.chunks is None:
        assert prep.outcome is not None, "Step5Preparation invariant violated"
        return prep.outcome

    # Step 3: chunk_gate (defense-in-depth — coarse_gate lives in T5)
    gate = chunk_gate(len(prep.chunks))
    if not gate["ok"]:
        return IngestionOutcome(
            rag_status="failed",
            error=f"chunk_gate: {gate.get('reason', 'chunks_over_limit')}",
            error_code="chunks_over_limit",
        )

    # Step 4: RedisLock — concurrent_skip semantics (idempotent, NOT a failure)
    lock_key = f"step5:{payload.doc_hash}:{payload.version}"
    token = await redis_lock.acquire(
        lock_key, ttl_sec=settings.LOCK_TTL_SEC,
    )
    if token is None:
        return IngestionOutcome(
            rag_status="success",
            chunks_indexed=0,
            error="concurrent_skip: another worker holds the lock",
            error_code="concurrent_skip",
        )

    # Step 5: encode + upsert + fts (Pre-Task A helper)
    try:
        return _run_step5(
            chunks=prep.chunks,
            qdrant=qdrant,
            fts=fts,
            audit_writer=None,
            doc_hash=payload.doc_hash,
            version=payload.version,
        )
    finally:
        # Best-effort release — if release raises, the lock TTL will
        # reap it (LOCK_TTL_SEC default = 300s, well under typical
        # encode time).
        try:
            await redis_lock.release(lock_key, token)
        except Exception as e:
            logger.warning(
                "step5_worker: lock release failed (TTL will reap): %s", e,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outcome_to_dict(outcome: IngestionOutcome) -> dict[str, Any]:
    """Flatten IngestionOutcome to a picklable dict for pebble return."""
    return {
        "rag_status": outcome.rag_status,
        "error": outcome.error,
        "error_code": outcome.error_code,
        "chunks_indexed": outcome.chunks_indexed,
    }