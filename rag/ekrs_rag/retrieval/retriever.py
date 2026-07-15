"""Scope-aware retriever (Phase 6B).

Embeds queries via QdrantManager.search(query_text=...) which now
internally uses EmbeddingService. Retriever no longer holds embedder
directly (D5 simplification).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ekrs_shared.models import Chunk, NumericHint

from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
from ekrs_rag.retrieval.qdrant_client import QdrantManager

logger = logging.getLogger(__name__)

_SCOPE_PRIORITY_MAP = {
    "national": 100, "industry": 80, "enterprise": 60, "project": 40, "reference": 20,
}


@dataclass
class RetrievalResult:
    chunks: List[Chunk]
    vector_scores: List[float]
    scope_scores: List[float]
    final_scores: List[float]

    @property
    def scores(self) -> List[float]:
        return self.vector_scores


class EKRSRetriever:
    def __init__(self, qdrant: QdrantManager) -> None:
        self._qdrant = qdrant

    def retrieve(
        self,
        query: str,
        top_k: int = 40,
        active_scope: Optional[List[str]] = None,
    ) -> RetrievalResult:
        # Phase 6B: qdrant.search handles embedding internally (D5)
        hits = self._qdrant.search(query_text=query, top_k=top_k)
        if not hits:
            return RetrievalResult(chunks=[], vector_scores=[], scope_scores=[], final_scores=[])

        payloads, raw_scores = zip(*hits)
        chunks: List[Chunk] = []
        vector_scores: List[float] = []

        for payload, score in zip(payloads, raw_scores):
            chunk = Chunk(
                text=payload.get("text", ""),
                scope_path=payload.get("scope_path", []),
                source_block_ids=payload.get("source_block_ids", []),
                token_count=payload.get("token_count", 0),
                doc_hash=payload.get("doc_hash", ""),
                version=payload.get("version", 0),
                page_numbers=payload.get("page_numbers", []),
                numeric_hints=[],
            )
            if active_scope is not None:
                if not chunk.scope_path:
                    continue
                if not self._scope_matches(chunk.scope_path, active_scope):
                    continue
            hints: List[NumericHint] = extract_hints(chunk)
            chunk.numeric_hints = hints
            chunks.append(chunk)
            vector_scores.append(score)

        chunks, vector_scores, scope_scores, final_scores = self._rank_by_scope(chunks, vector_scores)
        logger.debug("Retrieved %d chunks, scope=%s", len(chunks), active_scope)
        return RetrievalResult(
            chunks=chunks, vector_scores=vector_scores,
            scope_scores=scope_scores, final_scores=final_scores,
        )

    @staticmethod
    def _scope_priority(chunk: Chunk) -> float:
        if not chunk.scope_path:
            return 0.0
        first = chunk.scope_path[0].lower()
        return _SCOPE_PRIORITY_MAP.get(first, 40) / 100.0

    def _rank_by_scope(self, chunks, vector_scores):
        if not chunks:
            return [], [], [], []
        scope_scores = [self._scope_priority(c) for c in chunks]
        final_scores = [vec * (1 + scope) for vec, scope in zip(vector_scores, scope_scores)]
        combined = list(zip(chunks, vector_scores, scope_scores, final_scores))
        combined.sort(key=lambda x: x[3], reverse=True)
        sorted_chunks, sorted_vec, sorted_scope, sorted_final = zip(*combined)
        return list(sorted_chunks), list(sorted_vec), list(sorted_scope), list(sorted_final)

    @staticmethod
    def _scope_matches(chunk_scope, active_scope):
        if len(chunk_scope) < len(active_scope):
            return False
        return chunk_scope[: len(active_scope)] == active_scope
