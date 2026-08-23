"""Step5 helpers — pure functions extracted from pipeline.ingest.

Phase 13a Pre-Task A (eng-review Issue 1): single source of truth for the
ingestion Step 5 segment. pipeline.ingest 老路径仍消费 helper;new
Step5Worker (T3) 直接调 helper via asyncio.run wrapper。

- _prepare_step5: 读 JSONL + parse + classify + chunk + 幂等 skip;不触 encode/qdrant.upsert
- _run_step5: qdrant.upsert + fts.replace_doc + delete_old_versions;无 I/O 副作用

All dependencies are DI'd (Protocol contracts), no module globals, no
asyncio — keep these as plain sync functions so they can be unit-tested
without an event loop and reused by both sync and async call sites.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ekrs_shared.models import Chunk, IngestionNotification

from ..core.config import settings
from ..ingestion.chunker import chunk_blocks
from ..ingestion.doc_classifier import (
    DocClassifierRules,
    classify,
    load_index_file_name,
    load_rules,
)
from ..ingestion.ir_parser import IRParseError, parse_jsonl_file
from ..ingestion.outcome import IngestionOutcome

logger = logging.getLogger(__name__)


class AuditEmitter(Protocol):
    """Minimal audit writer contract (Phase 13a Pre-Task A relocation).

    Originally defined in ingestion/pipeline.py. Moved here because
    step5_helpers is the lowest layer that needs it; pipeline.py now
    re-imports from this module to preserve the
    `from ekrs_rag.ingestion.pipeline import AuditEmitter` public path
    (existing tests rely on it).
    """

    def write(self, event_type: str, **kwargs: object) -> bool: ...


# Phase 12 Task C: filename → doc_type classifier rules. Lazy-loaded
# singleton (loaded once per process). Rules themselves are immutable;
# the cache exists only to avoid re-parsing the JSON config on every
# ingest call.
_DOC_CLASSIFIER_RULES: DocClassifierRules | None = None


class QdrantLike(Protocol):
    """Minimal Qdrant contract used by Step5 helpers.

    Keeps helpers free of QdrantManager import; tests pass MagicMock
    with `spec=QdrantManager` and Python's structural typing handles
    the rest. Same pattern as AuditEmitter above.
    """

    def get_ingestion_status(self, doc_hash: str) -> Any | None: ...
    def upsert_chunks(self, chunks: list[Chunk]) -> int: ...
    def delete_old_versions(self, doc_hash: str, *, keep_version: int) -> int: ...


class FTSLike(Protocol):
    """Minimal FTS contract used by Step5 helpers.

    Mirrors FTSManager.replace_doc (returns int — number of rows upserted).
    """

    def replace_doc(self, doc_hash: str, chunks: list[Chunk], *, version: int) -> int: ...


@dataclass(frozen=True)
class Step5Preparation:
    """Result of _prepare_step5.

    Three valid states:
    - chunks populated, outcome=None, skip_reason=None → proceed to _run_step5
    - chunks=None, outcome=IngestionOutcome(success), skip_reason="duplicate"
      → idempotent skip (existing pipeline behavior at pipeline.py:148-153)
    - chunks=None, outcome=IngestionOutcome(failed), skip_reason=str
      → early-exit on missing/empty/parse-error
    """

    chunks: list[Chunk] | None
    outcome: IngestionOutcome | None
    skip_reason: str | None


def _prepare_step5(
    notification: IngestionNotification,
    qdrant: QdrantLike,
    storage_root: Path,
    audit_writer: AuditEmitter | None,  # noqa: ARG001 — kept for future expansion (audit emits at prepare boundaries); unused today
) -> Step5Preparation:
    """Pure function: read JSONL → parse → classify → chunk.

    Returns Step5Preparation with either:
    - chunks populated, outcome=None, skip_reason=None → proceed to _run_step5
    - chunks=None, outcome=IngestionOutcome(...), skip_reason=str → early-exit

    No encode, no qdrant write, no FTS write. All side effects delegated
    to the qdrant.get_ingestion_status call (idempotency check only).
    """
    doc_hash = notification.doc_hash
    version = notification.version
    output_path = Path(notification.output_path)
    resolved_storage_root = Path(storage_root).resolve()

    # P0.2: defense-in-depth output_path check (route already enforces this;
    # pipeline re-checks). Replicated from pipeline.py:124-139.
    try:
        output_path.resolve(strict=False).relative_to(resolved_storage_root)
    except (ValueError, OSError) as e:
        logger.error(
            "output_path_out_of_scope: doc=%s v=%d path=%s root=%s",
            doc_hash, version, output_path, resolved_storage_root,
        )
        return Step5Preparation(
            chunks=None,
            outcome=IngestionOutcome(
                rag_status="failed",
                error=f"output_path outside SHARED_STORAGE_PATH: {output_path}",
                error_code="output_path_out_of_scope",
            ),
            skip_reason="output_path_out_of_scope",
        )

    logger.info(
        "Starting ingestion: doc=%s v=%d path=%s",
        doc_hash, version, output_path,
    )

    # Step 1: Idempotency check — already indexed at same version?
    existing = qdrant.get_ingestion_status(doc_hash)
    if existing and existing.status == "success" and existing.version == version:
        logger.info(
            "Already indexed: doc=%s v=%d (%d chunks), skipping",
            doc_hash, version, existing.chunks_indexed,
        )
        return Step5Preparation(
            chunks=None,
            outcome=IngestionOutcome(
                rag_status="success",
                chunks_indexed=existing.chunks_indexed,
            ),
            skip_reason="duplicate",
        )

    # Step 2: Read JSONL
    jsonl_path = output_path / "data.jsonl"
    if not jsonl_path.exists():
        logger.error("JSONL not found: %s", jsonl_path)
        return Step5Preparation(
            chunks=None,
            outcome=IngestionOutcome(
                rag_status="failed",
                error=f"File not found: {jsonl_path}",
                error_code="jsonl_missing",
            ),
            skip_reason="jsonl_missing",
        )

    # Step 3-4: Parse + chunk
    try:
        blocks = parse_jsonl_file(str(jsonl_path))
        if not blocks:
            logger.warning("Empty JSONL: %s", jsonl_path)
            return Step5Preparation(
                chunks=None,
                outcome=IngestionOutcome(
                    rag_status="failed",
                    error="Empty JSONL file",
                    error_code="jsonl_empty",
                ),
                skip_reason="jsonl_empty",
            )

        # Phase 12 Task C: read index.json → classify filename → doc_type.
        # Classifier exceptions are isolated so they NEVER fail ingestion;
        # we log WARNING + default doc_type to 'unknown'.
        global _DOC_CLASSIFIER_RULES
        try:
            file_name = load_index_file_name(output_path)
            if _DOC_CLASSIFIER_RULES is None:
                _DOC_CLASSIFIER_RULES = load_rules()
            rules: DocClassifierRules = _DOC_CLASSIFIER_RULES
            if file_name is not None:
                doc_type = classify(file_name, rules).doc_type
            else:
                doc_type = "unknown"
        except Exception as e:
            logger.warning(
                "doc_classifier_failed: %s — defaulting doc_type to 'unknown'",
                e,
            )
            doc_type = "unknown"

        chunks = chunk_blocks(
            blocks, doc_hash, version,
            max_tokens=settings.MAX_CHUNK_TOKENS,
            payload_version=2,
            doc_type=doc_type,
        )
        if not chunks:
            logger.warning("No chunks produced from %d blocks", len(blocks))
            return Step5Preparation(
                chunks=None,
                outcome=IngestionOutcome(
                    rag_status="failed",
                    error="No chunks produced",
                    error_code="no_chunks",
                ),
                skip_reason="no_chunks",
            )

        return Step5Preparation(chunks=chunks, outcome=None, skip_reason=None)

    except IRParseError as e:
        logger.error("JSONL parse error for %s: %s", doc_hash, e)
        return Step5Preparation(
            chunks=None,
            outcome=IngestionOutcome(
                rag_status="failed",
                error=str(e),
                error_code="ir_parse_error",
            ),
            skip_reason="ir_parse_error",
        )


def _run_step5(
    chunks: list[Chunk],
    qdrant: QdrantLike,
    fts: FTSLike | None,
    audit_writer: AuditEmitter | None,
    doc_hash: str,
    version: int,
) -> IngestionOutcome:
    """Pure function: qdrant.upsert + delete_old_versions + fts.replace_doc.

    All side effects delegated to DI'd managers. Returns IngestionOutcome.

    Failure semantics:
    - qdrant.upsert_chunks failure → rag_status="failed", error_code="qdrant_upsert_failed"
      (qdrant is truth-of-record; if it fails, fts MUST NOT be called)
    - delete_old_versions failure → silently logged, outcome still success
      (best-effort cleanup; not blocking)
    - fts.replace_doc failure → silently logged, outcome still success
      (paired write — drift detection in T10a-2 catches later)
    - fts=None → no FTS write, no fts_synced audit emit (Phase 9 baseline path)
    """
    # Step 5: Upsert to Qdrant (truth-of-record)
    try:
        count = qdrant.upsert_chunks(chunks)
        logger.info(
            "Ingested %d chunks for doc=%s v=%d",
            count, doc_hash, version,
        )
    except Exception as e:
        logger.error("Qdrant upsert failed for %s: %s", doc_hash, e)
        return IngestionOutcome(
            rag_status="failed",
            error=str(e),
            error_code="qdrant_upsert_failed",
        )

    # Step 5.5: P2 — old-version cleanup (only after successful upsert)
    if settings.OLD_VERSION_DELETE_ENABLED:
        try:
            qdrant.delete_old_versions(doc_hash, keep_version=version)
        except Exception as e:
            logger.warning(
                "delete_old_versions_failed: doc=%s v=%d err=%s",
                doc_hash, version, e,
            )

    # Step 5.6: Phase 10 T10a-2 — FTS sync write (paired with Qdrant).
    # Qdrant is truth-of-record; FTS failure does NOT fail ingestion.
    # Drift (if any) is detected by ConsistencyChecker (T10a-2).
    if fts is not None:
        try:
            fts.replace_doc(doc_hash, chunks, version=version)
            if audit_writer is not None:
                # fts_synced schema registered in T10a-7; before that,
                # write() returns False silently. Emit call stays stable.
                audit_writer.write(
                    "fts_synced",
                    doc_hash=doc_hash, version=version,
                    chunks_written=count,
                )
        except Exception as fts_err:
            logger.warning(
                "fts_sync_failed_after_qdrant: doc=%s v=%d err=%s",
                doc_hash, version, fts_err,
            )

    return IngestionOutcome(rag_status="success", chunks_indexed=count)