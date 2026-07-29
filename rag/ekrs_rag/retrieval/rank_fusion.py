"""Reciprocal Rank Fusion — Phase 10 T10a-3.

Pure (R2) RRF function + :class:`FusionStats` frozen dataclass for audit
analytics. See ``docs/superpowers/plans/2026-07-29-phase10-T10a-3-rrf.md``.

The retriever (``T10a-4``) consumes this; the audit pipeline
(``T10a-7``) reads :class:`FusionStats` fields for the ``fts_searched``
event payload. No I/O, no state, no side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class FusionStats:
    """Hit-set analytics for a single :func:`reciprocal_rank_fusion` call.

    Field semantics (defined for ``ranked_lists[0]`` = vector list and
    ``ranked_lists[1]`` = FTS list — caller ordering convention):

    ``vector_hits``
        Items that appear ONLY in the vector list (not in FTS).
    ``fts_hits``
        Items that appear ONLY in the FTS list (not in vector).
    ``both_hits``
        Items that appear in BOTH lists (deduplicated).

    Invariant for ``N=2``: vector_hits + fts_hits + both_hits ==
    |unique keys union|. For ``N>2``, fields are computed against
    lists[0] vs lists[1]; lists[2:] contribute scores but do not change
    ``vector_hits/fts_hits/both_hits`` semantics.
    """

    vector_hits: int
    fts_hits: int
    both_hits: int


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[T]],
    key_fn: Callable[[T], str],
    k: int = 60,
) -> tuple[list[tuple[T, float]], FusionStats]:
    """Fuse N ranked lists via RRF: ``score(d) = Σ_i 1/(k + rank_i(d))``.

    Args:
        ranked_lists: Each sublist is already sorted (rank 1 = best).
            ``T10a-4`` calls with ``N=2`` in (vector_list, fts_list)
            order; ``N=1`` and ``N=3+`` supported for forward-compat.
        key_fn: Extracts the deduplication key from each item. ``T10a-4``
            will use chunk identity (post-``T10a-5`` ``chunk_id``, or
            fallback to ``doc_hash + source_block_ids[0]``).
        k: RRF dampening constant. Parent plan locks ``k=60`` (default).
            Tests use ``k=10`` or ``k=1`` to verify fusion logic without
            60× arithmetic slowdown.

    Returns:
        ``(fused_results, stats)``:
            ``fused_results`` = ``[(item, fused_score), ...]`` sorted
            descending by ``fused_score``; ties broken by insertion
            order (first-appearance-wins; deterministic — R2).
            ``stats`` = :class:`FusionStats` for audit consumption.

    Pure (R2): no I/O, no state, no side effects. ``key_fn`` exceptions
    propagate unchanged.
    """
    if not ranked_lists or not k >= 1:  # noqa: SIM222 — empty guard
        return [], FusionStats(0, 0, 0)

    # First pass: walk all lists in order, accumulating RRF scores per
    # key. ``rank_i(d)`` = BEST rank of ``d`` in list ``i`` (standard RRF
    # interpretation): a duplicate key within a sublist is *ignored* after
    # its first occurrence; duplicate keys across sublists contribute one
    # score per sublist (their first occurrence in that sublist).
    # Insertion order is preserved via dict (Python 3.7+ guarantees).
    scores: dict[str, float] = {}
    first_item: dict[str, T] = {}

    for sublist in ranked_lists:
        seen_in_this: set[str] = set()
        for rank_idx, item in enumerate(sublist, start=1):
            key = key_fn(item)
            if key in seen_in_this:
                # Duplicate in same sublist — ignore, per RRF best-rank rule.
                continue
            seen_in_this.add(key)
            if key not in scores:
                scores[key] = 0.0
                first_item[key] = item
            scores[key] += 1.0 / (k + rank_idx)

    # Sort by (−score, insertion_index): desc by score, stable on ties.
    ordered = sorted(
        scores.items(),
        key=lambda kv: (-kv[1],),  # noqa: B023 — bound at sort-call, deterministic
    )
    fused_results: list[tuple[T, float]] = [
        (first_item[key], score) for key, score in ordered
    ]

    # FusionStats — scoped to first two lists per caller convention.
    vector_keys = {
        key_fn(item) for item in ranked_lists[0]
    } if ranked_lists else set()
    fts_keys = {
        key_fn(item) for item in ranked_lists[1]
    } if len(ranked_lists) > 1 else set()

    both = vector_keys & fts_keys
    vector_only = vector_keys - both
    fts_only = fts_keys - both

    stats = FusionStats(
        vector_hits=len(vector_only),
        fts_hits=len(fts_only),
        both_hits=len(both),
    )

    return fused_results, stats
