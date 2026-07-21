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


class AuditEmitter(Protocol):
    """Minimal contract for the injected audit writer.

    Documents the method the pipeline actually calls (`write`), so a missing
    or renamed method is caught by the type checker rather than at runtime.
    Kept as a Protocol to preserve loose coupling (no import of AuditWriter).
    """

    def write(self, event_type: str, **kwargs: object) -> bool: ...


class IngestionPipeline:
    """Orchestrates: read JSONL → parse → chunk → write Qdrant → callback."""

    def __init__(
        self,
        qdrant: QdrantManager,
        storage_path: Path,
        parser_token: str,
        audit_writer: AuditEmitter | None = None,
    ) -> None:
        self._qdrant = qdrant
        self._shared_storage_root = Path(storage_path).resolve()
        self._parser_token = parser_token
        # D5: optional injection; if None, audit emits are skipped (test fixtures).
        self._audit_writer = audit_writer

    async def ingest(self, notification: IngestionNotification) -> IngestionOutcome:
        """Run full ingestion pipeline for a parser notification.

        Steps:
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
        doc_hash = notification.doc_hash
        version = notification.version
        output_path = Path(notification.output_path)

        # P0.2: defense-in-depth check (route already enforces this; pipeline re-checks)
        try:
            output_path.resolve(strict=False).relative_to(self._shared_storage_root)
        except (ValueError, OSError) as e:
            logger.error(
                "output_path_out_of_scope: doc=%s v=%d path=%s root=%s",
                doc_hash,
                version,
                output_path,
                self._shared_storage_root,
            )
            outcome = self._failed_outcome(
                "output_path_out_of_scope",
                f"output_path outside SHARED_STORAGE_PATH: {output_path}",
            )
            await self._send_callback_safely(notification, outcome)
            return outcome

        logger.info("Starting ingestion: doc=%s v=%d path=%s",
                     doc_hash, version, output_path)

        # Step 1: Idempotency check
        existing = self._qdrant.get_ingestion_status(doc_hash)
        if existing and existing.status == "success" and existing.version == version:
            logger.info("Already indexed: doc=%s v=%d (%d chunks), skipping",
                         doc_hash, version, existing.chunks_indexed)
            outcome = IngestionOutcome(
                rag_status="success",
                chunks_indexed=existing.chunks_indexed,
            )
            await self._send_callback_safely(notification, outcome)
            return outcome

        # Step 2: Read JSONL
        jsonl_path = output_path / "data.jsonl"
        if not jsonl_path.exists():
            logger.error("JSONL not found: %s", jsonl_path)
            outcome = self._failed_outcome(
                "jsonl_missing", f"File not found: {jsonl_path}",
            )
            await self._send_callback_safely(notification, outcome)
            return outcome

        # Step 3-4: Parse and chunk
        try:
            blocks = parse_jsonl_file(str(jsonl_path))
            if not blocks:
                logger.warning("Empty JSONL: %s", jsonl_path)
                outcome = self._failed_outcome("jsonl_empty", "Empty JSONL file")
                await self._send_callback_safely(notification, outcome)
                return outcome

            chunks = chunk_blocks(blocks, doc_hash, version)
            if not chunks:
                logger.warning("No chunks produced from %d blocks", len(blocks))
                outcome = self._failed_outcome("no_chunks", "No chunks produced")
                await self._send_callback_safely(notification, outcome)
                return outcome

        except IRParseError as e:
            logger.error("JSONL parse error for %s: %s", doc_hash, e)
            outcome = self._failed_outcome("ir_parse_error", str(e))
            await self._send_callback_safely(notification, outcome)
            return outcome

        # Step 5: Upsert to Qdrant
        try:
            count = self._qdrant.upsert_chunks(chunks)
            logger.info("Ingested %d chunks for doc=%s v=%d", count, doc_hash, version)
        except Exception as e:
            logger.error("Qdrant upsert failed for %s: %s", doc_hash, e)
            outcome = self._failed_outcome("qdrant_upsert_failed", str(e))
            await self._send_callback_safely(notification, outcome)
            return outcome

        # Step 5.5: P2 — old-version cleanup (only after successful upsert)
        if settings.OLD_VERSION_DELETE_ENABLED:
            try:
                self._qdrant.delete_old_versions(doc_hash, keep_version=version)
            except Exception as e:
                logger.warning(
                    "delete_old_versions_failed: doc=%s v=%d err=%s",
                    doc_hash, version, e,
                )

        # Step 6: success
        outcome = IngestionOutcome(rag_status="success", chunks_indexed=count)
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

            chunks = chunk_blocks(blocks, doc_hash, version)
            if not chunks:
                raise ValueError("No chunks produced")
        except IRParseError as e:
            raise ValueError(f"JSONL parse error: {e}") from e

        count = self._qdrant.upsert_chunks(chunks)
        logger.info("Replayed %d chunks for doc=%s v=%d", count, doc_hash, version)
        return count

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
