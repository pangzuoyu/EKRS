"""Dense-only retriever for EKRS RAG.

Phase 2b: Embeds queries with BGESmall and searches Qdrant with dense vectors.
Scope filtering is applied post-retrieval.
Numeric hints are extracted from retrieved chunks for downstream solving.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ekrs_shared.models import Chunk, NumericHint

from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
from ekrs_rag.retrieval.embedder import BGESmallEmbedder
from ekrs_rag.retrieval.qdrant_client import QdrantManager

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Result of a retrieval query.

    Attributes:
        chunks: List of retrieved Chunk objects, filtered by scope if applicable.
        scores: Parallel list of cosine similarity scores (0.0-1.0).
    """

    chunks: List[Chunk]
    scores: List[float]


class EKRSRetriever:
    """Dense-only retriever using BGESmall embeddings.

    Phase 2b replaces Phase 1 dummy-vector retrieval with real bge-small-en-v1.5
    384-dimensional embeddings.
    """

    def __init__(self, qdrant: QdrantManager, embedder: BGESmallEmbedder):
        """Initialize retriever.

        Args:
            qdrant: QdrantManager instance for vector search.
            embedder: BGESmallEmbedder instance for query encoding.
        """
        self._qdrant = qdrant
        self._embedder = embedder

    def retrieve(
        self,
        query: str,
        top_k: int = 40,
        active_scope: Optional[List[str]] = None,
    ) -> RetrievalResult:
        """Retrieve chunks by semantic similarity to query.

        1. Embed query with BGESmall → 384d dense vector.
        2. Search Qdrant with dense vector.
        3. Optionally filter by scope_path prefix.
        4. Extract NumericHints from each chunk text.

        Args:
            query: Free-text query string.
            top_k: Maximum number of chunks to retrieve (before scope filtering).
            active_scope: Optional scope path prefix to filter results.
                          e.g. ["national", "GB"] matches scope_path starting with that prefix.

        Returns:
            RetrievalResult with filtered chunks and parallel scores.
        """
        # Step 1: Encode query
        query_vectors = self._embedder.encode([query])
        if not query_vectors:
            logger.warning("Embedder returned no vectors for query")
            return RetrievalResult(chunks=[], scores=[])

        query_vector = query_vectors[0]

        # Step 2: Dense search in Qdrant
        hits = self._qdrant.search(query_vector=query_vector, top_k=top_k)

        if not hits:
            return RetrievalResult(chunks=[], scores=[])

        payloads, raw_scores = zip(*hits)

        # Step 3: Build Chunk objects from payloads
        chunks: List[Chunk] = []
        scores: List[float] = []

        for payload, score in zip(payloads, raw_scores):
            # Reconstruct Chunk from Qdrant payload
            chunk = Chunk(
                text=payload.get("text", ""),
                scope_path=payload.get("scope_path", []),
                source_block_ids=payload.get("source_block_ids", []),
                token_count=payload.get("token_count", 0),
                doc_hash=payload.get("doc_hash", ""),
                version=payload.get("version", 0),
                page_numbers=payload.get("page_numbers", []),
                numeric_hints=[],  # populated below
            )

            # Step 4: Scope filtering (post-retrieval)
            if active_scope is not None:
                if not chunk.scope_path:
                    continue
                # Check if chunk.scope_path starts with the active_scope prefix
                if not self._scope_matches(chunk.scope_path, active_scope):
                    continue

            # Step 5: Extract numeric hints from chunk text
            hints: List[NumericHint] = extract_hints(chunk)
            chunk.numeric_hints = hints

            chunks.append(chunk)
            scores.append(score)

        logger.debug(
            "Retrieved %d chunks (of %d hits), scope=%s",
            len(chunks),
            len(hits),
            active_scope,
        )

        return RetrievalResult(chunks=chunks, scores=scores)

    @staticmethod
    def _scope_matches(chunk_scope: List[str], active_scope: List[str]) -> bool:
        """Check if chunk_scope starts with active_scope prefix.

        Args:
            chunk_scope: The chunk's scope_path.
            active_scope: The query's active_scope prefix.

        Returns:
            True if chunk_scope starts with active_scope, False otherwise.
        """
        if len(chunk_scope) < len(active_scope):
            return False
        return chunk_scope[: len(active_scope)] == active_scope
