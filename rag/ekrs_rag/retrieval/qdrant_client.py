"""Qdrant client wrapper for EKRS RAG (Phase 6B rewrite).

Phase 6B fixes 3 production bugs from 6A final review:
- B1: search() uses query_points() (qdrant-client 1.17.1)
- B2: ensure_collection reads config.params.vectors (1.17.1)
- B3: upsert_chunks uses EmbeddingService for real dense+sparse

EmbeddingService is injected at construction. D1: upsert raises
EmbeddingUnavailableError when service is in dummy mode.
D4: ensure_collection runs in lifespan; AUTO_REINDEX env controls
whether dim mismatch triggers automatic delete+recreate.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from qdrant_client import QdrantClient, models
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ekrs_shared.models import Chunk, IngestionStatus
from ekrs_rag.observability.audit import get_writer
from ekrs_rag.retrieval.embedding_service import (
    EmbeddingService,
    EmbeddingUnavailableError,
)


def _emit_qdrant_failure(operation: str, collection: str, exc: BaseException) -> None:
    """Best-effort audit emit for Qdrant operation failures (Phase 6C T8 fix).

    Never raises — writer.write() already swallows its own errors. Calling
    sites re-raise the original exception so retry/caller behavior is
    unchanged.
    """
    writer = get_writer()
    if writer is None:
        return
    writer.write(
        "qdrant_write_failed",
        collection=collection,
        operation=operation,
        error=type(exc).__name__,
        message=str(exc)[:200],
    )

logger = logging.getLogger(__name__)

DEFAULT_VECTOR_SIZE = 1024  # bge-m3 dense dimension


class QdrantManager:
    """Manages Qdrant collection lifecycle and document operations."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6333,
        collection_name: str = "rag_documents",
        embedding_service: Optional[EmbeddingService] = None,
        auto_reindex: bool = True,
    ) -> None:
        if embedding_service is None:
            raise ValueError(
                "embedding_service is required (Phase 6B B3 fix). "
                "Pass EmbeddingService() instance."
            )
        self._client = QdrantClient(host=host, port=port)
        self._collection_name = collection_name
        self._embedding_service = embedding_service
        self._auto_reindex = auto_reindex

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
    )
    def ensure_collection(self, vector_size: int = DEFAULT_VECTOR_SIZE) -> None:
        """Create collection if not exists. B2 fix: real 1.17.1 API path.

        If existing collection dim mismatches, behavior depends on auto_reindex:
        - True (default): delete and recreate (D4)
        - False: raise RuntimeError (production safety)
        """
        try:
            existing_size = None
            try:
                existing = self._client.get_collection(self._collection_name)
                # B2 fix: 1.17.1 path is config.params.vectors["dense"].size
                existing_size = existing.config.params.vectors["dense"].size
            except Exception:
                existing_size = None

            if existing_size is not None and existing_size != vector_size:
                if not self._auto_reindex:
                    raise RuntimeError(
                        f"Collection {self._collection_name} dim={existing_size} "
                        f"does not match expected {vector_size}. "
                        f"Recovery: set AUTO_REINDEX=true in .env to automatically "
                        f"rebuild, OR manually delete and recreate via Qdrant UI/API."
                    )
                logger.warning(
                    "Collection %s has dim=%d, need %d — recreating",
                    self._collection_name, existing_size, vector_size,
                )
                self._client.delete_collection(self._collection_name)
                existing_size = None

            if existing_size is None:
                self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config={
                        "dense": models.VectorParams(
                            size=vector_size,
                            distance=models.Distance.COSINE,
                        ),
                    },
                    sparse_vectors_config={
                        "sparse": models.SparseVectorParams(
                            index=models.SparseIndexParams(on_disk=False),
                        ),
                    },
                )
                logger.info(
                    "Created collection %s (dense=%dd + sparse)",
                    self._collection_name, vector_size,
                )
        except Exception as exc:
            _emit_qdrant_failure("write", self._collection_name, exc)
            raise

    @retry(
        reraise=True,
        retry=retry_if_not_exception_type(EmbeddingUnavailableError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
    )
    def upsert_chunks(self, chunks: list[Chunk]) -> int:
        """Batch upsert chunks with real bge-m3 embeddings (B3 fix).

        D1: Raises EmbeddingUnavailableError if embedding service is dummy.
        Returns number of points upserted.
        """
        try:
            if not chunks:
                return 0

            if self._embedding_service.is_dummy:
                raise EmbeddingUnavailableError(
                    "Cannot upsert: EmbeddingService is in dummy mode. "
                    "Model files missing or failed to load. "
                    "Check rag/models/bge-m3/ and audit log."
                )

            texts = [c.text for c in chunks]
            encoded = self._embedding_service.encode(texts)

            points = []
            for chunk, vec in zip(chunks, encoded):
                point_id = str(uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"{chunk.doc_hash}:{chunk.version}:{chunk.source_block_ids}",
                ))
                sparse_qdrant = self._embedding_service.to_qdrant_sparse(vec.sparse)
                payload = {
                    "text": chunk.text,
                    "scope_path": chunk.scope_path,
                    "source_block_ids": chunk.source_block_ids,
                    "token_count": chunk.token_count,
                    "doc_hash": chunk.doc_hash,
                    "version": chunk.version,
                    "page_numbers": chunk.page_numbers,
                }
                points.append(models.PointStruct(
                    id=point_id,
                    vector={
                        "dense": vec.dense,
                        "sparse": sparse_qdrant,
                    },
                    payload=payload,
                ))

            batch_size = 100
            for i in range(0, len(points), batch_size):
                batch = points[i:i + batch_size]
                self._client.upsert(
                    collection_name=self._collection_name,
                    points=batch,
                )

            logger.info(
                "Upserted %d chunks for doc %s v%d (bge-m3 dense+sparse)",
                len(points), chunks[0].doc_hash, chunks[0].version,
            )
            return len(points)
        except EmbeddingUnavailableError:
            # Phase 6A D1 contract: dummy-mode is a config error, not a
            # Qdrant write failure — emit nothing, let caller handle.
            raise
        except Exception as exc:
            _emit_qdrant_failure("write", self._collection_name, exc)
            raise

    def get_ingestion_status(self, doc_hash: str) -> Optional[IngestionStatus]:
        """Query Qdrant for ingestion status of a document."""
        try:
            results, _ = self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="doc_hash",
                            match=models.MatchValue(value=doc_hash),
                        ),
                    ],
                ),
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
            if not results:
                return None
            count_result = self._client.count(
                collection_name=self._collection_name,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="doc_hash",
                            match=models.MatchValue(value=doc_hash),
                        ),
                    ],
                ),
            )
            version = results[0].payload.get("version", 0)
            return IngestionStatus(
                status="success",
                chunks_indexed=count_result.count,
                version=version,
            )
        except Exception as e:
            logger.error("Failed to query ingestion status for %s: %s", doc_hash, e)
            return IngestionStatus(
                status="failed",
                chunks_indexed=0,
                error=str(e),
            )

    def search(
        self,
        query_text: str,
        top_k: int = 40,
        score_threshold: Optional[float] = None,
    ) -> list[tuple[dict, float]]:
        """Hybrid search by query text. B1 fix: uses query_points (1.17.1).

        Encodes query via EmbeddingService, then query_points with
        Prefetch (dense + sparse) + FusionQuery(RRF). Preserves 6A's
        SearchParams(hnsw_ef=128) optimization for HNSW recall quality.
        """
        if self._embedding_service.is_dummy:
            # Critical gap fix: log WARN so operator sees silent empty results
            # in dev/CI without confusing them with production empty queries.
            logger.warning(
                "search() returning []: EmbeddingService is in dummy mode. "
                "Model files missing or failed to load. "
                "Check rag/models/bge-m3/ and audit log."
            )
            return []  # Safe degradation; no match possible

        try:
            encoded = self._embedding_service.encode([query_text])[0]
            sparse_qdrant = self._embedding_service.to_qdrant_sparse(encoded.sparse)

            results = self._client.query_points(
                collection_name=self._collection_name,
                prefetch=[
                    models.Prefetch(
                        query=encoded.dense,
                        using="dense",
                        limit=top_k,
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=sparse_qdrant["indices"],
                            values=sparse_qdrant["values"],
                        ),
                        using="sparse",
                        limit=top_k,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=top_k,
                with_payload=True,
                with_vectors=False,
                score_threshold=score_threshold,
                # Preserve 6A Task 8 commit 033a8a3 HNSW quality optimization.
                # hnsw_ef=128 raises HNSW search beam width for better recall
                # at small perf cost. Inherited by both prefetches.
                search_params=models.SearchParams(hnsw_ef=128),
            )
            return [(hit.payload, hit.score) for hit in results.points]
        except Exception as exc:
            _emit_qdrant_failure("read", self._collection_name, exc)
            raise

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
    )
    def delete_old_versions(self, doc_hash: str, keep_version: int) -> int:
        """Delete Qdrant points for old versions of a document."""
        try:
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="doc_hash",
                                match=models.MatchValue(value=doc_hash),
                            ),
                            models.FieldCondition(
                                key="version",
                                match=models.MatchValue(value=keep_version),
                            ),
                        ],
                        must_not=[],
                    ),
                ),
            )
            logger.info("Deleted old versions of %s keeping v%d", doc_hash, keep_version)
            return 0
        except Exception as exc:
            _emit_qdrant_failure("delete", self._collection_name, exc)
            raise
