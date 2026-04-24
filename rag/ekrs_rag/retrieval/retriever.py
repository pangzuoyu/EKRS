"""Dense-only retriever for EKRS RAG.

Phase 2b: Embeds queries with BGESmall and searches Qdrant with dense vectors.
Phase 3: Scope-aware ranking (national > industry > enterprise > project).
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


# Scope priority: national > industry > enterprise > project > reference
_SCOPE_PRIORITY_MAP = {
    "national": 100,
    "industry": 80,
    "enterprise": 60,
    "project": 40,
    "reference": 20,
}


@dataclass
class RetrievalResult:
    """Result of a retrieval query.

    Attributes:
        chunks: List of retrieved Chunk objects, filtered by scope if applicable.
        vector_scores: Parallel list of cosine similarity scores (0.0-1.0).
        scope_scores: Parallel list of scope priority scores (0.0-1.0).
        final_scores: Composite scores = vector * (1 + scope/100).
    """

    chunks: List[Chunk]
    vector_scores: List[float]
    scope_scores: List[float]
    final_scores: List[float]


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
            return RetrievalResult(chunks=[], vector_scores=[], scope_scores=[], final_scores=[])

        query_vector = query_vectors[0]

        # Step 2: Dense search in Qdrant
        hits = self._qdrant.search(query_vector=query_vector, top_k=top_k)

        if not hits:
            return RetrievalResult(chunks=[], vector_scores=[], scope_scores=[], final_scores=[])

        payloads, raw_scores = zip(*hits)

        # Step 3: Build Chunk objects from payloads
        chunks: List[Chunk] = []
        vector_scores: List[float] = []

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
            vector_scores.append(score)

        # Step 6: Scope-aware ranking (composite score)
        chunks, vector_scores, scope_scores, final_scores = self._rank_by_scope(
            chunks, vector_scores
        )

        logger.debug(
            "Retrieved %d chunks (of %d hits), scope=%s",
            len(chunks),
            len(hits),
            active_scope,
        )

        return RetrievalResult(
            chunks=chunks,
            vector_scores=vector_scores,
            scope_scores=scope_scores,
            final_scores=final_scores,
        )

    @staticmethod
    def _scope_priority(chunk: Chunk) -> float:
        """Get scope priority score for a chunk.

        Returns normalized priority (0.0-1.0) based on first element of scope_path.
        """
        if not chunk.scope_path:
            return 0.0
        first = chunk.scope_path[0].lower()
        return _SCOPE_PRIORITY_MAP.get(first, 40) / 100.0

    def _rank_by_scope(
        self, chunks: List[Chunk], vector_scores: List[float]
    ) -> tuple[List[Chunk], List[float], List[float], List[float]]:
        """Rank chunks by composite score: vector_similarity * (1 + scope_priority).

        Returns (sorted_chunks, sorted_vector_scores, scope_scores, final_scores)
        sorted by final_scores descending.
        """
        if not chunks:
            return [], [], [], []

        scope_scores = [self._scope_priority(c) for c in chunks]
        # final = vec * (1 + scope/100)
        final_scores = [
            vec * (1 + scope) for vec, scope in zip(vector_scores, scope_scores)
        ]

        # Sort descending by final_scores, keeping parallel lists aligned
        combined = list(zip(chunks, vector_scores, scope_scores, final_scores))
        combined.sort(key=lambda x: x[3], reverse=True)

        sorted_chunks, sorted_vec, sorted_scope, sorted_final = zip(*combined)
        return list(sorted_chunks), list(sorted_vec), list(sorted_scope), list(sorted_final)

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
