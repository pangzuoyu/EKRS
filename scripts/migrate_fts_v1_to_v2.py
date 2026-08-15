#!/usr/bin/env python3
"""FTS5 v1 → v2 migration script — Phase 12 F3.

One-time migration for the 745 historical docs that landed in FTS5 v1
(pre-Phase 12 T3). Reads all Qdrant points grouped by doc_hash, builds
Chunk objects, and writes to a fresh v2 FTS5 table via FTSManager.replace_doc.

Designed for low-traffic window (Q5 trigger conditions):
- verify_reingest.py 4-step P2 fix shipped (ccd5726)
- Phase 12 T1-T5 ships (090d74f + 6b726bd)
- 7-day soak period
- User approval

Operational properties:
- DRY-RUN by default (--apply to actually write).
- Idempotent: replace_doc atomically wipes + writes per doc_hash, so
  re-running after partial failure converges to the same end state.
- ConsistencyChecker suppression: is_migration_in_progress()=True during
  rebuild so 5-min drift checks don't emit false-positive audits.
- D3 retry: replace_doc retries on sqlite busy with exponential backoff.
- Per-doc progress logging to stdout; CHANGELOG-worthy summary at end.

Usage:
    python scripts/migrate_fts_v1_to_v2.py --dry-run           # default
    python scripts/migrate_fts_v1_to_v2.py --apply             # real write
    python scripts/migrate_fts_v1_to_v2.py --apply --limit 10  # first 10 docs only

Exit codes:
    0 = success (all docs migrated or dry-run only)
    1 = unrecoverable error (Qdrant unreachable, FTS5 init failure)
    2 = partial success (some docs failed; rerun to retry)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterator

# Allow running from project root without install
_RAG_DIR = Path(__file__).resolve().parent.parent / "rag"
sys.path.insert(0, str(_RAG_DIR))

from ekrs_rag.concurrency.migration_state import (  # noqa: E402
    is_migration_in_progress,
    reset_migration_in_progress,
    set_migration_in_progress,
)
from ekrs_rag.retrieval.fts_manager import FTSManager  # noqa: E402
from ekrs_shared.models import Chunk  # noqa: E402

logger = logging.getLogger("fts_migration")


# --- D3 retry decorator (sqlite busy) ---------------------------------------

def retry_on_sqlite_busy(max_attempts: int = 3, backoff_ms: int = 100):
    """Retry decorator for sqlite3.OperationalError 'database is locked'.

    D3 plan: SQLite FTS5 + concurrent reads from Qdrant during rebuild can
    surface SQLITE_BUSY. Exponential backoff: 100ms, 200ms, 400ms.
    """
    import functools
    import sqlite3

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    last_exc = exc
                    if attempt + 1 >= max_attempts:
                        # Final attempt: don't sleep, just raise below.
                        break
                    wait_ms = backoff_ms * (2 ** attempt)
                    logger.warning(
                        "sqlite_busy_attempt_%d: %s (retry in %dms)",
                        attempt + 1, exc, wait_ms,
                    )
                    time.sleep(wait_ms / 1000.0)
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator


# --- Qdrant helpers ---------------------------------------------------------

def list_doc_hashes(qdrant) -> list[str]:
    """List all distinct doc_hash values in the Qdrant collection.

    Scrolls the entire collection. For 745 docs at ~50 chunks/doc = ~37k points,
    should complete in <30s. For larger corpora, replace with iteration on
    indexed doc_hash values (Qdrant payload index).
    """
    from qdrant_client import models

    seen: set[str] = set()
    offset = None
    while True:
        results, next_offset = qdrant._client.scroll(  # noqa: SLF001
            collection_name=qdrant._collection_name,  # noqa: SLF001
            scroll_filter=None,
            limit=1000,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for point in results:
            doc_hash = (point.payload or {}).get("doc_hash", "")
            if doc_hash:
                seen.add(doc_hash)
        if not next_offset:
            break
        offset = next_offset
    return sorted(seen)


def fetch_chunks_for_doc(qdrant, doc_hash: str) -> list[Chunk]:
    """Fetch all chunks for a single doc_hash from Qdrant.

    Returns Chunk objects reconstructed from Qdrant payload — same shape
    as FTSManager._payload_to_chunk (without the unused score arg).
    """
    from qdrant_client import models

    results, _ = qdrant._client.scroll(  # noqa: SLF001
        collection_name=qdrant._collection_name,  # noqa: SLF001
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="doc_hash",
                    match=models.MatchValue(value=doc_hash),
                ),
            ],
        ),
        limit=10_000,
        with_payload=True,
        with_vectors=False,
    )

    chunks: list[Chunk] = []
    for point in results:
        payload = point.payload or {}
        # Reconstruct Chunk from payload (matches _payload_to_chunk shape).
        chunks.append(Chunk(
            text=payload.get("text", ""),
            scope_path=payload.get("scope_path", []),
            source_block_ids=payload.get("source_block_ids", []),
            token_count=payload.get("token_count", 0),
            doc_hash=payload.get("doc_hash", ""),
            version=payload.get("version", 0),
            page_numbers=payload.get("page_numbers", []),
            numeric_hints=[],
            chunk_id=payload.get("chunk_id"),
            form_fields=payload.get("form_fields", []),
            column_headers=payload.get("column_headers", []),
        ))
    return chunks


# --- Migration loop ---------------------------------------------------------

@retry_on_sqlite_busy(max_attempts=3, backoff_ms=100)
def _replace_doc_with_retry(fts: FTSManager, doc_hash: str, chunks: list[Chunk]) -> int:
    return fts.replace_doc(doc_hash, chunks, version=chunks[0].version if chunks else 0)


def migrate(
    fts: FTSManager,
    qdrant,
    *,
    apply: bool,
    limit: int | None,
) -> tuple[int, int]:
    """Run migration. Returns (success_count, failure_count).

    Args:
        fts: FTSManager with schema_version=2 (caller's responsibility).
        qdrant: QdrantManager to read payloads from.
        apply: If False, dry-run (no writes); if True, actually writes.
        limit: If set, only process the first N docs.
    """
    assert fts._schema_version == 2, "FTSManager must be initialized with schema_version=2"
    assert apply or limit is None, "limit only meaningful with --apply"

    doc_hashes = list_doc_hashes(qdrant)
    if limit is not None:
        doc_hashes = doc_hashes[:limit]
    logger.info(
        "migration_start: docs=%d apply=%s limit=%s schema_version=%d",
        len(doc_hashes), apply, limit, fts._schema_version,
    )

    success = 0
    failure = 0
    token = set_migration_in_progress(apply)  # only suppress checks during real run
    try:
        for i, doc_hash in enumerate(doc_hashes, start=1):
            try:
                chunks = fetch_chunks_for_doc(qdrant, doc_hash)
                if not chunks:
                    logger.warning("skip_empty_doc: %s", doc_hash)
                    continue
                if apply:
                    written = _replace_doc_with_retry(fts, doc_hash, chunks)
                    logger.info(
                        "doc_migrated: %s (%d/%d) chunks_written=%d",
                        doc_hash, i, len(doc_hashes), written,
                    )
                else:
                    logger.info(
                        "doc_dry_run: %s (%d/%d) chunks=%d",
                        doc_hash, i, len(doc_hashes), len(chunks),
                    )
                success += 1
            except Exception as exc:
                logger.error("doc_failed: %s err=%s", doc_hash, exc)
                failure += 1
    finally:
        reset_migration_in_progress(token)

    logger.info(
        "migration_done: success=%d failure=%d apply=%s",
        success, failure, apply,
    )
    return success, failure


# --- Entry point ------------------------------------------------------------

def _build_qdrant() -> object:
    """Build QdrantManager from env vars. Standalone (no FastAPI app)."""
    from ekrs_rag.retrieval.qdrant_client import QdrantManager

    host = os.environ.get("QDRANT_HOST", "localhost")
    port = int(os.environ.get("QDRANT_GRPC_PORT", "6333"))
    # EmbeddingService is needed by QdrantManager __init__ but NOT used by
    # the migration's read path (we only call .scroll + .count). Use a stub.
    from ekrs_rag.retrieval.embedding_service import EmbeddingService

    emb = EmbeddingService(model_dir=Path("/nonexistent"))  # is_dummy=True
    return QdrantManager(host=host, port=port, embedding_service=emb)


def _build_fts(db_path: Path) -> FTSManager:
    """Build FTSManager with schema_version=2."""
    return FTSManager(db_path, schema_version=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write to FTS5. Default is dry-run.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N docs (for staged rollout).",
    )
    parser.add_argument(
        "--fts-db", type=Path, default=Path("/app/rag/fts.sqlite"),
        help="Path to the FTS5 SQLite file (default: /app/rag/fts.sqlite).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    try:
        qdrant = _build_qdrant()
        fts = _build_fts(args.fts_db)
    except Exception as exc:
        logger.error("init_failed: %s", exc)
        return 1

    success, failure = migrate(
        fts, qdrant,
        apply=args.apply,
        limit=args.limit,
    )

    if failure == 0:
        return 0
    if success > 0:
        return 2  # partial
    return 1  # unrecoverable (no successes)


if __name__ == "__main__":
    sys.exit(main())