"""Ingestion pipeline — orchestrates JSONL → parse → chunk → Qdrant.

Handles the full ingestion flow triggered by parser notifications.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import httpx
from prometheus_client import Counter
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ekrs_shared.models import IngestionNotification

from ..core.config import settings
from ..retrieval.fts_manager import FTSManager
from ..retrieval.qdrant_client import QdrantManager
from ..security import (
    CallbackAuthMissingError,
    CallbackURLBlockedError,
    build_callback_headers,
    validate_callback_url,
)
from .chunker import chunk_blocks
from .ir_parser import IRParseError, parse_jsonl_file
from .outcome import IngestionOutcome
from ..services.step5_helpers import (
    AuditEmitter,
    _prepare_step5,
    _run_step5,
)

# Re-export for backward compat: existing tests / callers use
# `from ekrs_rag.ingestion.pipeline import AuditEmitter`.
__all__ = [
    "AuditEmitter",
    "CallbackNonRetryableError",
    "CallbackRetryableError",
    "IngestionPipeline",
]

logger = logging.getLogger(__name__)


# T6: callback outcome counters for observability. Matches the pattern used
# by ekrs_rag.observability.metrics; uses prometheus_client directly so the
# pipeline can be imported without spinning up the full metrics registry.
# Outcome values: sent, url_blocked, auth_missing, nonretryable_4xx, retried.
CALLBACK_OUTCOMES = Counter(
    "rag_callback_total",
    "RAG callback outcomes (one per terminal branch of _send_callback)",
    ["outcome"],
)


class CallbackRetryableError(Exception):
    """Network or 5xx error — should be retried."""


class CallbackNonRetryableError(Exception):
    """4xx error — should NOT be retried."""


class IngestionPipeline:
    """Orchestrates: read JSONL → parse → chunk → write Qdrant → callback."""

    def __init__(
        self,
        qdrant: QdrantManager,
        storage_path: Path,
        parser_token: str,
        audit_writer: AuditEmitter | None = None,
        fts: FTSManager | None = None,
        fts_schema_version: int = 2,
    ) -> None:
        self._qdrant = qdrant
        self._shared_storage_root = Path(storage_path).resolve()
        self._parser_token = parser_token
        # D5: optional injection; if None, audit emits are skipped (test fixtures).
        self._audit_writer = audit_writer
        # Phase 10 T10a-2: FTS sync. None = Phase 9 baseline (no FTS write).
        self._fts = fts
        # Phase 12 F1: FTSManager target schema version. Default=2 means new
        # ingest lands in v2 (form_fields_text + column_headers_text columns).
        # v1 reserved for legacy DBs pending one-time migration. Pipeline does
        # NOT migrate in-place; migration is the responsibility of an offline
        # rebuild script (see F3 + ccd5726 4-step P2 fix).
        self._fts_schema_version = fts_schema_version

    async def ingest(self, notification: IngestionNotification) -> IngestionOutcome:
        """Run full ingestion pipeline for a parser notification.

        Steps (Phase 13a Pre-Task A: Steps 1-5.6 extracted to
        services/step5_helpers.py as pure functions, single source of
        truth):
        1. Check idempotency (already indexed → skip)
        2. Read JSONL from shared volume
        3. Parse DocumentBlock IR
        4. Chunk blocks
        5. Upsert to Qdrant
        6. Send callback to parser

        Returns IngestionOutcome. Callback transport failures are
        swallowed by _send_callback_safely so the outcome reflects only
        the ingestion state (success/business-failure), not callback
        delivery status.
        """
        # Phase 13a Pre-Task A: helper extraction. _prepare_step5 handles
        # Steps 0-4 (defense-in-depth path check + idempotency + JSONL +
        # parse + chunk + classifier). Early-exit paths return an
        # IngestionOutcome directly.
        prep = _prepare_step5(
            notification=notification,
            qdrant=self._qdrant,
            storage_root=self._shared_storage_root,
            audit_writer=self._audit_writer,
        )

        # Early-exit: prep has outcome but no chunks (skip / error path)
        if prep.chunks is None:
            assert prep.outcome is not None, "Step5Preparation invariant violated"
            await self._send_callback_safely(notification, prep.outcome)
            return prep.outcome

        # Step 5+5.5+5.6: qdrant.upsert + delete_old_versions + fts.replace_doc
        outcome = _run_step5(
            chunks=prep.chunks,
            qdrant=self._qdrant,
            fts=self._fts,
            audit_writer=self._audit_writer,
            doc_hash=notification.doc_hash,
            version=notification.version,
        )

        await self._send_callback_safely(notification, outcome)
        return outcome

    @staticmethod
    def _failed_outcome(error_code: str, error_msg: str) -> IngestionOutcome:
        """Build a failed IngestionOutcome with consistent shape."""
        return IngestionOutcome(
            rag_status="failed",
            error=error_msg,
            error_code=error_code,
        )

    async def _send_callback_safely(
        self,
        notification: IngestionNotification,
        outcome: IngestionOutcome,
    ) -> None:
        """Send callback; swallow transport failures.

        By the time we reach this method the Qdrant write is already
        committed (success) or there's no recoverable state worth
        surfacing (failure). Best-effort by design.
        """
        doc_hash = notification.doc_hash
        version = notification.version
        try:
            await self._send_callback(
                notification, outcome.rag_status, error=outcome.error,
            )
        except (CallbackRetryableError, CallbackNonRetryableError) as cb_err:
            if self._audit_writer is not None:
                self._audit_writer.write(
                    "callback_best_effort_failed",
                    doc_hash=doc_hash, version=version,
                    rag_status=outcome.rag_status,
                    error=str(cb_err),
                )
            logger.warning(
                "callback_best_effort_failed: doc=%s v=%d status=%s err=%s",
                doc_hash, version, outcome.rag_status, cb_err,
            )

    async def replay(
        self,
        jsonl_path: Path,
        doc_hash: str,
        version: int,
    ) -> int:
        """Re-run parse+chunk+upsert for an already-indexed document.

        Used by /v1/ingestion/replay. Shares parse/chunk/upsert primitives
        with ingest() but skips the parser callback and the idempotency
        check (caller has already verified the source_path + sha256).

        Returns the number of chunks written to Qdrant.
        """
        logger.info("Replaying ingestion: doc=%s v=%d path=%s",
                     doc_hash, version, jsonl_path)

        try:
            blocks = parse_jsonl_file(str(jsonl_path))
            if not blocks:
                raise ValueError(f"Empty JSONL: {jsonl_path}")

            chunks = chunk_blocks(
                blocks, doc_hash, version,
                max_tokens=settings.MAX_CHUNK_TOKENS,
                payload_version=2,
            )
            if not chunks:
                raise ValueError("No chunks produced")
        except IRParseError as e:
            raise ValueError(f"JSONL parse error: {e}") from e

        count = self._qdrant.upsert_chunks(chunks)
        logger.info("Replayed %d chunks for doc=%s v=%d", count, doc_hash, version)
        return count

    async def reparse(
        self,
        source_path: str,
        doc_hash: str,
        version: int,
        callback_url: str | None,
        force: bool = False,
    ) -> IngestionOutcome:
        """Universal re-ingest entry point for compensation handlers + CLI.

        Phase 7 T3 (Decision §1). Reads JSONL from ``source_path`` and
        re-runs the parse → chunk → Qdrant upsert → callback flow. When
        ``force=False`` (default) and the on-disk file's SHA256 already
        matches the version indexed in Qdrant, returns early with
        ``rag_status='duplicate'`` — preserving idempotency for routine
        compensation retries. When ``force=True``, bypasses the hash
        check and unconditionally re-upserts (operator escape hatch).

        Args:
            source_path: absolute path to the parser-written JSONL file.
            doc_hash:    ``doc_id`` / ``content_hash`` from the task row.
            version:     monotonic document version.
            callback_url: optional parser callback URL (no-op if None).
            force:       bypass SHA256 idempotency check.

        Returns:
            IngestionOutcome with ``rag_status`` ∈ {success, duplicate,
            business_failure}.
        """
        from ..models.ingestion import IngestionNotification  # local import (avoid cycle)

        jsonl_path = Path(source_path)
        if not jsonl_path.exists():
            logger.error(
                "reparse: source_path missing: doc=%s v=%d path=%s",
                doc_hash, version, source_path,
            )
            return IngestionOutcome(
                rag_status="business_failure",
                error=f"source_path missing: {source_path}",
            )

        # Idempotency: skip re-upsert when hashes already match.
        if not force:
            existing = self._qdrant.get_ingestion_status(doc_hash)
            if existing and existing.status == "success" and existing.version == version:
                logger.info(
                    "reparse: hash matches existing v=%d, skipping upsert "
                    "(force=False); doc=%s",
                    version, doc_hash,
                )
                outcome = IngestionOutcome(
                    rag_status="duplicate",
                    chunks_indexed=existing.chunks_indexed,
                )
                if callback_url:
                    # Best-effort callback for the duplicate path.
                    notification = IngestionNotification(
                        doc_hash=doc_hash,
                        version=version,
                        output_path=str(jsonl_path.parent),
                        callback_url=callback_url,
                    )
                    await self._send_callback_safely(notification, outcome)
                return outcome

        logger.info(
            "reparse: doc=%s v=%d path=%s force=%s",
            doc_hash, version, source_path, force,
        )
        # Delegate to replay() (shares parse/chunk/upsert primitives).
        try:
            count = await self.replay(jsonl_path, doc_hash, version)
        except ValueError as e:
            return IngestionOutcome(
                rag_status="business_failure", error=str(e),
            )

        outcome = IngestionOutcome(
            rag_status="success", chunks_indexed=count,
        )
        if callback_url:
            notification = IngestionNotification(
                doc_hash=doc_hash,
                version=version,
                output_path=str(jsonl_path.parent),
                callback_url=callback_url,
            )
            await self._send_callback_safely(notification, outcome)
        return outcome

    @retry(
        reraise=True,
        retry=retry_if_exception_type(CallbackRetryableError),
        stop=stop_after_attempt(settings.PIPELINE_CALLBACK_MAX_ATTEMPTS),
        wait=wait_exponential(
            min=settings.PIPELINE_RETRY_MIN_SEC,
            max=settings.PIPELINE_RETRY_MAX_SEC,
        ),
    )
    async def _send_callback(
        self,
        notification: IngestionNotification,
        rag_status: str,
        error: str | None = None,
    ) -> None:
        """Send callback to parser with ingestion result.

        URL is allowlisted (T4); headers carry X-Parser-Token (T6); 4xx
        responses are non-retryable (T7); 5xx and network errors are
        retried up to PIPELINE_CALLBACK_MAX_ATTEMPTS attempts.
        """
        if not notification.callback_url:
            logger.warning("No callback_url, skipping callback for %s",
                           notification.doc_hash)
            return

        # T4: validate URL against allowlist (SSRF mitigation)
        try:
            parsed = validate_callback_url(notification.callback_url)
        except CallbackURLBlockedError as e:
            CALLBACK_OUTCOMES.labels(outcome="url_blocked").inc()
            if self._audit_writer is not None:
                self._audit_writer.write(
                    "callback_url_blocked",
                    doc_hash=notification.doc_hash,
                    version=notification.version,
                    reason=str(e),
                )
            logger.warning(
                "callback_url_blocked: doc=%s reason=%s",
                notification.doc_hash, e,
            )
            return  # best-effort; don't block ingestion

        # T6: build headers with X-Parser-Token
        try:
            headers = build_callback_headers()
        except CallbackAuthMissingError as e:
            CALLBACK_OUTCOMES.labels(outcome="auth_missing").inc()
            if self._audit_writer is not None:
                self._audit_writer.write(
                    "callback_auth_missing",
                    doc_hash=notification.doc_hash,
                    version=notification.version,
                )
            logger.error("callback_auth_missing: %s", e)
            return

        payload = {
            "doc_hash": notification.doc_hash,
            "version": notification.version,
            "rag_status": rag_status,
            "trace_id": notification.trace_id,
        }
        if error:
            # Defensive cap: prevents oversized callback body and DB errors
            # if the parser's parse_tasks.error column is bounded.
            payload["error"] = error[: settings.CALLBACK_ERROR_MAX_CHARS]

        try:
            async with httpx.AsyncClient(
                timeout=settings.PIPELINE_CALLBACK_TIMEOUT_SEC,
            ) as client:
                resp = await client.post(
                    parsed.raw, json=payload, headers=headers,
                )
                # T7: 4xx is non-retryable
                if 400 <= resp.status_code < 500:
                    CALLBACK_OUTCOMES.labels(outcome="nonretryable_4xx").inc()
                    raise CallbackNonRetryableError(
                        f"callback {resp.status_code} (non-retryable)",
                    )
                resp.raise_for_status()
                CALLBACK_OUTCOMES.labels(outcome="sent").inc()
                logger.info(
                    "Callback sent: doc=%s status=%s",
                    notification.doc_hash, rag_status,
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            raise CallbackRetryableError(str(e)) from e
        except httpx.HTTPStatusError as e:
            if 400 <= e.response.status_code < 500:
                CALLBACK_OUTCOMES.labels(outcome="nonretryable_4xx").inc()
                raise CallbackNonRetryableError(
                    f"callback {e.response.status_code} (non-retryable)",
                ) from e
            raise CallbackRetryableError(str(e)) from e
