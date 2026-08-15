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
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ekrs_shared.models import Chunk, NumericHint

from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
from ekrs_rag.retrieval.qdrant_client import QdrantManager
from ekrs_rag.retrieval.rank_fusion import FusionStats, reciprocal_rank_fusion

logger = logging.getLogger(__name__)

# Phase 12 §七 Item 3: env-var default for ``retrieve(..., form_field_boost=...)``.
# ``"true"`` (default) preserves Phase 12 T4 behavior post-toggle; setting
# ``EKRS_FORM_FIELD_BOOST_ENABLED=false`` flips to legacy T3 base-only path
# for the §七 Item 3 recall@10 baseline script's "boost OFF" round.
_FORM_FIELD_BOOST_ENABLED_ENV_DEFAULT: bool = (
    os.getenv("EKRS_FORM_FIELD_BOOST_ENABLED", "true").lower() != "false"
)

_SCOPE_PRIORITY_MAP = {
    "national": 100, "industry": 80, "enterprise": 60, "project": 40, "reference": 20,
}

# Phase 12 T4: form_field / column_header R4 scope-aware boost.
# Q3 §9.6 last-mile: form-extracted fields carry strong semantic authority
# (e.g. LOT 49 / SYSTEM NO from a project checklist) — boost the scope
# score so they outrank reference-tier chunks with same vector score.
# FORM_FIELD_WEIGHT > COLUMN_HEADER_WEIGHT because form metadata is
# document-level (key=value identity), while column_headers are
# table-presentation. R6 strict parity: deterministic, no LLM/cross-encoder.
FORM_FIELD_WEIGHT = 0.9
COLUMN_HEADER_WEIGHT = 0.7


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
    # T10b-3: ``True`` when retriever bypassed RRF because user query
    # was a substring of at least one retrieved chunk's text (strong-
    # signal optimization). Defaults to ``False`` so consumers built
    # before T10b-3 see the same field set + same False default → no
    # behavior change for non-matching queries (Phase 10 baseline).
    short_circuit: bool = False

    @property
    def scores(self) -> List[float]:
        return self.vector_scores


class EKRSRetriever:
    def __init__(
        self,
        qdrant: QdrantManager,
        fts: Optional["object"] = None,  # type: ignore[type-arg]  # FTSManager | None
        audit_writer: Optional["object"] = None,  # type: ignore[type-arg]  # AuditWriter | None
    ) -> None:
        # ``fts`` is duck-typed (needs ``search_with_payload(query)``). Using a
        # forward-reference avoids a circular import with FTSManager.
        # ``audit_writer`` (Phase 10 T10a-7) is duck-typed (needs ``.write()``);
        # it may also be the shared AuditLogger base if a future caller
        # wires a non-writer. ``None`` (default) preserves the Phase 9
        # byte-level baseline — no audit emit.
        self._qdrant = qdrant
        self._fts = fts
        self._audit_writer = audit_writer

    async def retrieve(
        self,
        query: str,
        top_k: int = 40,
        active_scope: Optional[List[str]] = None,
        form_field_boost: Optional[bool] = None,
    ) -> RetrievalResult:
        # Phase 12 §七 Item 3: ``form_field_boost=None`` falls back to module-
        # level env var ``EKRS_FORM_FIELD_BOOST_ENABLED`` (default ``True``,
        # preserves Phase 12 T4 behavior). Explicit ``True``/``False``
        # overrides env var. The §七 Item 3 baseline script passes both
        # values explicitly so env var flakiness doesn't bias the run.
        if form_field_boost is None:
            form_field_boost = _FORM_FIELD_BOOST_ENABLED_ENV_DEFAULT
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

        # T10b-3: short-circuit evaluation. When user query is a
        # substring of any retrieved chunk's text, bypass RRF and
        # return matched chunks directly with score=1.0 (deterministic
        # strong-signal optimization). Gated on fts!=None to preserve
        # Phase 9 byte-level baseline when fts is disabled.
        # T10a-4 path retained as the no-match fallback; T10a-5
        # chunk_id key continues to be preferred (legacy fallback).
        short_circuit = False
        if self._fts is not None:
            # T10a-5 chunk_key reused as RRF key_fn + union dedup key.
            _chunk_key = EKRSRetriever._chunk_key
            # Build deduped union: vector first (insertion-order
            # stable), then FTS. _chunk_key reused by both short-
            # circuit dedup and RRF fusion below.
            unioned: List[Chunk] = []
            seen_keys: set = set()
            for c in list(vector_chunks) + list(fts_chunks):
                k = _chunk_key(c)
                if k not in seen_keys:
                    seen_keys.add(k)
                    unioned.append(c)

            exact_match_idx = EKRSRetriever._is_exact_match(query, unioned)
            if exact_match_idx:
                # Short-circuit: matched chunks returned with score=1.0.
                # fusion_stats reflects vector-only contribution
                # (fts=0, both=0) — short-circuit bypass skips RRF's
                # overlap calc; the chunk is in vector (else not
                # matched) but overlap concept doesn't apply here.
                short_chunks = [unioned[i] for i in exact_match_idx]
                fused_chunks = short_chunks
                fused_scores = [1.0] * len(short_chunks)
                fusion_stats = FusionStats(len(short_chunks), 0, 0)
                short_circuit = True
            else:
                # Standard RRF fusion path (T10a-4).
                fused, fusion_stats = reciprocal_rank_fusion(
                    [vector_chunks, fts_chunks],  # type: ignore[arg-type]
                    key_fn=_chunk_key,
                )
                fused_chunks = [c for c, _ in fused]
                fused_scores = [s for _, s in fused]
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
            filtered_chunks, filtered_scores, form_field_boost=form_field_boost
        )

        # Hint extraction per chunk (Phase 6B invariant — populate
        # numeric_hints so downstream solver can build evidence).
        for chunk in chunks:
            hints: List[NumericHint] = extract_hints(chunk)
            chunk.numeric_hints = hints

        # fusion_stats: only populated when FTS was active (R4 invariant —
        # fts=None path is byte-level Phase 9 → fusion_stats=None).
        result_fusion_stats: Optional[FusionStats] = fusion_stats if self._fts is not None else None

        # Phase 10 T10a-7: emit ``fts_searched`` audit event when FTS is
        # configured (i.e. RRF actually ran). Audit emit is best-effort —
        # a failing write must not propagate to the retriever caller
        # (parent §204 "审计永远不阻塞业务").
        if self._audit_writer is not None and self._fts is not None and result_fusion_stats is not None:
            try:
                self._audit_writer.write(  # type: ignore[attr-defined]
                    "fts_searched",
                    vector_hits=result_fusion_stats.vector_hits,
                    fts_hits=result_fusion_stats.fts_hits,
                    both_hits=result_fusion_stats.both_hits,
                )
            except Exception as audit_err:
                logger.warning("fts_searched_audit_emit_failed: %s", audit_err)

        logger.debug(
            "Retrieved %d chunks (fts=%s short_circuit=%s), scope=%s",
            len(chunks), self._fts is not None, short_circuit, active_scope,
        )
        return RetrievalResult(
            chunks=chunks, vector_scores=vec_scores,
            scope_scores=scope_scores, final_scores=final_scores,
            fusion_stats=result_fusion_stats,
            short_circuit=short_circuit,
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
            chunk_id=payload.get("chunk_id"),  # T10a-5: FTS↔Qdrant round-trip
            # Phase 12 T4: form_fields / column_headers populated from Qdrant
            # payload (T2 chunker + QdrantManager wrote them). Defaults to
            # empty list (gstack D4) for legacy chunks pre-T2.
            form_fields=payload.get("form_fields", []),
            column_headers=payload.get("column_headers", []),
        )

    @staticmethod
    def _scope_priority(chunk: Chunk, form_field_boost: bool = True) -> float:
        """Compute R4 scope-aware priority score for a chunk.

        Base score from scope_path[0] (national=1.0 .. reference=0.2).
        Phase 12 T4 boost: form_fields → max with FORM_FIELD_WEIGHT=0.9;
        column_headers → max with COLUMN_HEADER_WEIGHT=0.7. Both boosts
        are deterministic (R6 strict parity).

        ``form_field_boost`` (Phase 12 §七 Item 3, default ``True``):
        ``False`` skips both form_field/column_header max — returns base
        only. Used by the recall@10 baseline script to compare boost
        ON vs OFF on the same corpus. Phase 12 T4 default behavior is
        preserved when callers omit the kwarg.
        """
        if chunk.scope_path:
            first = chunk.scope_path[0].lower()
            base = _SCOPE_PRIORITY_MAP.get(first, 40) / 100.0
        else:
            base = 0.0
        score = base
        if form_field_boost:
            if chunk.form_fields:
                score = max(score, FORM_FIELD_WEIGHT)
            if chunk.column_headers:
                score = max(score, COLUMN_HEADER_WEIGHT)
        return score

    @staticmethod
    def _chunk_key(chunk: Chunk) -> str:
        """Stable key for dedup + RRF. T10a-5: prefer ``chunk_id`` (set
        by QdrantManager.upsert_chunks); fall back to ``doc_hash:block_id``
        for legacy chunks (pre-T10a-5 ingestion, no chunk_id in payload).
        Raises IndexError if both are missing — caller guarantees
        ``source_block_ids`` non-empty by construction (chunker invariant).
        """
        if chunk.chunk_id:
            return chunk.chunk_id
        return f"{chunk.doc_hash}:{chunk.source_block_ids[0]}"

    @staticmethod
    def _is_exact_match(query: str, chunks: List[Chunk]) -> List[int]:
        """T10b-3 short-circuit predicate. Return indices of chunks
        whose ``text`` contains ``query`` as a substring.

        Defaults: case-sensitive (engineering identifiers like
        ``A312-TP316`` are case-sensitive; CJK queries are naturally
        case-insensitive). Substring (not whole-match) per parent plan
        §25 — phrase like ``"温度 ≤ 80℃"`` should match a chunk
        containing the literal phrase.

        Empty / whitespace-only query returns ``[]`` (no false-positive).
        """
        q = query.strip()
        if not q:
            return []
        return [i for i, c in enumerate(chunks) if q in c.text]  # type: ignore[operator]  # Chunk.text is str

    def _rank_by_scope(self, chunks, vector_scores, form_field_boost: bool = True):
        if not chunks:
            return [], [], [], []
        scope_scores = [self._scope_priority(c, form_field_boost=form_field_boost) for c in chunks]
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
