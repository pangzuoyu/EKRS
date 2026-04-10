"""Ingestion pipeline — orchestrates JSONL → parse → chunk → Qdrant.

Handles the full ingestion flow triggered by parser notifications.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ekrs_shared.models import IngestionNotification

from ..retrieval.qdrant_client import QdrantManager
from .chunker import chunk_blocks
from .ir_parser import IRParseError, parse_jsonl_file

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates: read JSONL → parse → chunk → write Qdrant → callback."""

    def __init__(self, qdrant: QdrantManager, storage_path: Path):
        self._qdrant = qdrant
        self._storage_path = storage_path

    async def ingest(self, notification: IngestionNotification) -> None:
        """Run full ingestion pipeline for a parser notification.

        Steps:
        1. Check idempotency (already indexed → skip)
        2. Read JSONL from shared volume
        3. Parse DocumentBlock IR
        4. Chunk blocks
        5. Upsert to Qdrant
        6. Send callback to parser
        """
        doc_hash = notification.doc_hash
        version = notification.version
        output_path = Path(notification.output_path)

        logger.info("Starting ingestion: doc=%s v=%d path=%s",
                     doc_hash, version, output_path)

        # Step 1: Idempotency check
        existing = self._qdrant.get_ingestion_status(doc_hash)
        if existing and existing.status == "success" and existing.version == version:
            logger.info("Already indexed: doc=%s v=%d (%d chunks), skipping",
                         doc_hash, version, existing.chunks_indexed)
            await self._send_callback(notification, "success")
            return

        # Step 2: Read JSONL
        jsonl_path = output_path / "data.jsonl"
        if not jsonl_path.exists():
            logger.error("JSONL not found: %s", jsonl_path)
            await self._send_callback(notification, "failed",
                                       error=f"File not found: {jsonl_path}")
            return

        # Step 3-4: Parse and chunk
        try:
            blocks = parse_jsonl_file(str(jsonl_path))
            if not blocks:
                logger.warning("Empty JSONL: %s", jsonl_path)
                await self._send_callback(notification, "failed",
                                           error="Empty JSONL file")
                return

            chunks = chunk_blocks(blocks, doc_hash, version)
            if not chunks:
                logger.warning("No chunks produced from %d blocks", len(blocks))
                await self._send_callback(notification, "failed",
                                           error="No chunks produced")
                return

        except IRParseError as e:
            logger.error("JSONL parse error for %s: %s", doc_hash, e)
            await self._send_callback(notification, "failed", error=str(e))
            return

        # Step 5: Upsert to Qdrant
        try:
            count = self._qdrant.upsert_chunks(chunks)
            logger.info("Ingested %d chunks for doc=%s v=%d", count, doc_hash, version)
        except Exception as e:
            logger.error("Qdrant upsert failed for %s: %s", doc_hash, e)
            await self._send_callback(notification, "failed", error=str(e))
            return

        # Step 6: Callback success
        await self._send_callback(notification, "success")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def _send_callback(
        self,
        notification: IngestionNotification,
        rag_status: str,
        error: str | None = None,
    ) -> None:
        """Send callback to parser with ingestion result."""
        if not notification.callback_url:
            logger.warning("No callback_url, skipping callback for %s",
                           notification.doc_hash)
            return

        payload = {
            "doc_hash": notification.doc_hash,
            "version": notification.version,
            "rag_status": rag_status,
            "trace_id": notification.trace_id,
        }
        if error:
            payload["error"] = error

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    notification.callback_url,
                    json=payload,
                )
                resp.raise_for_status()
                logger.info("Callback sent: doc=%s status=%s",
                            notification.doc_hash, rag_status)
        except Exception as e:
            logger.error("Callback failed for %s: %s", notification.doc_hash, e)
            # Re-raise to trigger tenacity retry
            raise
