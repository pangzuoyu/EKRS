"""Scope-aware retriever (Phase 6B) + RRF integration (Phase 10 T10a-4).

Embeds queries via QdrantManager.search(query_text=...) which internally
uses EmbeddingService. Retriever no longer holds embedder directly
(D5 simplification).

T10a-4: parallel vector + FTS retrieval via ``asyncio.gather``, fused
through :func:`~ekrs_rag.retrieval.rank_fusion.reciprocal_rank_fusion`.
``fts=None`` (default) preserves the Phase 9 byte-level baseline.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ekrs_shared.models import Chunk, NumericHint

from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
from ekrs_rag.retrieval.qdrant_client import QdrantManager
from ekrs_rag.retrieval.rank_fusion import FusionStats, reciprocal_rank_fusion

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
    # T10a-4: ``None`` when retriever runs in ``fts=None`` degradation
    # mode (Phase 9 byte-level baseline). Set to a ``FusionStats``
    # instance when FTS is configured — :meth:`retrieve` populates even
    # when FTS results are empty (vector-only round), so consumers can
    # distinguish "FTS disabled" (None) vs "FTS ran but found nothing"
    # (FusionStats(vector=N, fts=0, both=0)).
    fusion_stats: Optional[FusionStats] = None

    @property
    def scores(self) -> List[float]:
        return self.vector_scores


class EKRSRetriever:
    def __init__(
        self,
        qdrant: QdrantManager,
        fts: Optional["object"] = None,  # type: ignore[type-arg]  # FTSManager | None
    ) -> None:
        # ``fts`` is duck-typed (needs ``search_with_payload(query)``). Using a
        # forward-reference avoids a circular import with FTSManager.
        self._qdrant = qdrant
        self._fts = fts

    async def retrieve(
        self,
        query: str,
        top_k: int = 40,
        active_scope: Optional[List[str]] = None,
    ) -> RetrievalResult:
        # Parallel retrieval: vector + FTS in two threads. FTS exception
        # is isolated via ``gather(return_exceptions=True)``; FTS failure
        # degrades to vector-only results (logged at WARNING).
        if self._fts is not None:
            vector_hits, fts_hits = await asyncio.gather(
                asyncio.to_thread(self._qdrant.search, query_text=query, top_k=top_k),
                asyncio.to_thread(self._fts.search_with_payload, query),  # type: ignore[attr-defined]
                return_exceptions=True,
            )
            if isinstance(vector_hits, BaseException):
                logger.warning("qdrant_search_failed: %s", vector_hits)
                vector_hits = []
            if isinstance(fts_hits, BaseException):
                logger.warning("fts_search_failed: %s", fts_hits)
                fts_hits = []
            # type narrowing: after the if-returns guards both are lists.
            assert isinstance(vector_hits, list)
            assert isinstance(fts_hits, list)
        else:
            vector_hits = self._qdrant.search(query_text=query, top_k=top_k)
            fts_hits = []

        # Build Chunk list for both paths. For FTS hits the payload dict
        # already has the same shape as Qdrant payload.
        vector_chunks = [
            self._payload_to_chunk(p, s) for p, s in vector_hits  # type: ignore[union-iter]
        ]
        fts_chunks = [
            self._payload_to_chunk(p, s) for _cid, p, s in fts_hits  # type: ignore[union-iter]
        ]

        # active_scope filter on fused results (R4: scope_priority applied
        # AFTER RRF, per parent plan §Iron Rules).
        if self._fts is not None:
            # RRF fusion (T10a-4). T10a-5 will replace key_fn with chunk_id.
            fused, fusion_stats = reciprocal_rank_fusion(
                [vector_chunks, fts_chunks],  # type: ignore[arg-type]
                key_fn=lambda c: f"{c.doc_hash}:{c.source_block_ids[0]}",
            )
            fused_chunks: Sequence[Chunk] = [c for c, _ in fused]
            fused_scores: List[float] = [s for _, s in fused]
        else:
            # fts=None degradation path (M1+M4: byte-level == Phase 9).
            # Use raw Qdrant scores directly so composite scoring matches
            # Phase 9 exactly (Phase 6B tests assert vector_scores == [0.8, 1.0]).
            fused_chunks = vector_chunks  # type: ignore[assignment]
            fused_scores = [s for _, s in vector_hits]  # type: ignore[union-iter]
            fusion_stats = FusionStats(0, 0, 0)

        filtered_chunks: List[Chunk] = []
        filtered_scores: List[float] = []
        if active_scope is not None:
            for c, s in zip(fused_chunks, fused_scores):
                if not c.scope_path:
                    continue
                if not self._scope_matches(c.scope_path, active_scope):
                    continue
                filtered_chunks.append(c)
                filtered_scores.append(s)
        else:
            filtered_chunks = list(fused_chunks)
            filtered_scores = list(fused_scores)

        # scope_priority re-rank (Phase 6B _rank_by_scope, unchanged).
        chunks, vec_scores, scope_scores, final_scores = self._rank_by_scope(
            filtered_chunks, filtered_scores
        )

        # Hint extraction per chunk (Phase 6B invariant — populate
        # numeric_hints so downstream solver can build evidence).
        for chunk in chunks:
            hints: List[NumericHint] = extract_hints(chunk)
            chunk.numeric_hints = hints

        # fusion_stats: only populated when FTS was active (R4 invariant —
        # fts=None path is byte-level Phase 9 → fusion_stats=None).
        result_fusion_stats: Optional[FusionStats] = fusion_stats if self._fts is not None else None

        logger.debug(
            "Retrieved %d chunks (fts=%s), scope=%s",
            len(chunks), self._fts is not None, active_scope,
        )
        return RetrievalResult(
            chunks=chunks, vector_scores=vec_scores,
            scope_scores=scope_scores, final_scores=final_scores,
            fusion_stats=result_fusion_stats,
        )

    @staticmethod
    def _payload_to_chunk(payload: dict, score: float) -> Chunk:
        """Build Chunk IR from Qdrant / FTS payload dict. Identical shape
        because T10a-1 mirrors the Qdrant payload to ``payload_json`` UNINDEXED."""
        # ``score`` is unused here; the fused score is tracked in
        # ``RetrievalResult.vector_scores`` (parallel list, indexed
        # by chunk position). ``Chunk`` is a frozen Pydantic model —
        # no attribute assignment.
        del score
        return Chunk(
            text=payload.get("text", ""),
            scope_path=payload.get("scope_path", []),
            source_block_ids=payload.get("source_block_ids", []),
            token_count=payload.get("token_count", 0),
            doc_hash=payload.get("doc_hash", ""),
            version=payload.get("version", 0),
            page_numbers=payload.get("page_numbers", []),
            numeric_hints=[],
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
