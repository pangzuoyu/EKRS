"""Qdrant client wrapper for EKRS RAG.

Handles collection creation, point upsert, status queries.
Phase 1 uses dummy vectors (zeros). Phase 2 replaces with real embeddings.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from qdrant_client import QdrantClient, models
from tenacity import retry, stop_after_attempt, wait_exponential

from ekrs_shared.models import Chunk, IngestionStatus

logger = logging.getLogger(__name__)

DENSE_VECTOR_SIZE = 1024  # bge-m3 output dimension (Phase 1 default)


class QdrantManager:
    """Manages Qdrant collection lifecycle and document operations."""

    def __init__(self, host: str = "localhost", port: int = 6333,
                 collection_name: str = "rag_documents", vector_size: int = 384):
        self._client = QdrantClient(host=host, port=port)
        self._collection_name = collection_name
        self._vector_size = vector_size

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def ensure_collection(self, vector_size: int = 384) -> None:
        """Create collection if it doesn't exist or vector size mismatches.

        If collection exists with wrong vector size, delete and recreate.
        Phase 1 used 1024d (bge-m3). Phase 2 uses 384d (bge-small).
        """
        try:
            existing = self._client.get_collection(self._collection_name)
            existing_size = existing.vectors_config["dense"].size
        except Exception:
            existing_size = None

        if existing_size is not None and existing_size != vector_size:
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
            logger.info("Created collection %s (dense=%dd)", self._collection_name, vector_size)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def upsert_chunks(self, chunks: list[Chunk]) -> int:
        """Batch upsert chunks with dummy dense vectors (Phase 1).

        Phase 2 replaces dummy vectors with real bge-m3 embeddings.
        Returns number of points upserted.
        """
        if not chunks:
            return 0

        points = []
        for chunk in chunks:
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chunk.doc_hash}:{chunk.version}:{chunk.source_block_ids}"))

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
                    "dense": [0.0] * self._vector_size,  # Phase 1: dummy
                },
                payload=payload,
            ))

        # Batch upsert in groups of 100
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            self._client.upsert(
                collection_name=self._collection_name,
                points=batch,
            )

        logger.info("Upserted %d chunks for doc %s v%d",
                     len(points), chunks[0].doc_hash, chunks[0].version)
        return len(points)

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

            # Get count
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
        query_vector: list[float],
        top_k: int = 40,
        score_threshold: Optional[float] = None,
    ) -> list[tuple[dict, float]]:
        """Search collection by dense vector.

        Args:
            query_vector: Dense vector to search with.
            top_k: Number of results to return.
            score_threshold: Minimum cosine similarity score.

        Returns:
            List of (payload_dict, score) tuples.
        """
        from qdrant_client import models

        search_params = models.SearchParams(
            hnsw_algorithm=models.HNSWParams(m=16, ef=128)
        )
        results = self._client.search(
            collection_name=self._collection_name,
            query_vector=("dense", query_vector),
            limit=top_k,
            score_threshold=score_threshold,
            search_params=search_params,
            with_payload=True,
            with_vectors=False,
        )
        return [(hit.payload, hit.score) for hit in results]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def delete_old_versions(self, doc_hash: str, keep_version: int) -> int:
        """Delete Qdrant points for old versions of a document.

        Returns number of deleted points.
        """
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
        return 0  # Qdrant delete doesn't return count directly
