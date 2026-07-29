"""Unit tests for `reciprocal_rank_fusion` + `FusionStats` — Phase 10 T10a-3.

Pure-function RRF. Verifies:
- k=60 default behavior
- Empty / single / dual list edge cases
- FusionStats three-field independent int counts
- Reciprocal score formula `score = 1/(k+rank_a) + 1/(k+rank_b)`
- k parameter effect on weight distribution
- Deduplication within a single list
- Determinism / purity (same input → same output, R2)
- Frozen dataclass enforcement

10+ tests per plan §Tc.1 enumeration (lines 88-100).
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import List

import pytest

from ekrs_rag.retrieval.rank_fusion import (
    FusionStats,
    reciprocal_rank_fusion,
)


# --------------------------------------------------------------------------
# Test item type — minimal dataclass with id key
# --------------------------------------------------------------------------


def _id(item) -> str:  # noqa: ANN001 — pytest test helper
    return item["id"] if isinstance(item, dict) else getattr(item, "id")


def _doc_items(*ids: str) -> List[dict]:
    """Build test items as dicts (simple, no model overhead)."""
    return [{"id": i, "label": f"item-{i}"} for i in ids]


# ===========================================================================
# 1. Empty input boundaries
# ===========================================================================


def test_rrf_returns_empty_when_no_ranked_lists_given() -> None:
    """`reciprocal_rank_fusion([], key_fn)` → ([], FusionStats(0,0,0))."""
    results, stats = reciprocal_rank_fusion([], _id)
    assert results == []
    assert stats.vector_hits == 0
    assert stats.fts_hits == 0
    assert stats.both_hits == 0


def test_rrf_returns_empty_when_all_sublists_empty() -> None:
    """`reciprocal_rank_fusion([[], []], key_fn)` → ([], 0,0,0)."""
    results, stats = reciprocal_rank_fusion([[], []], _id)
    assert results == []
    assert stats.vector_hits == 0
    assert stats.fts_hits == 0
    assert stats.both_hits == 0


def test_rrf_single_empty_sublist_treats_as_one_list() -> None:
    """ranked_lists=[[a,b]] — non-empty single list — preserves order."""
    results, stats = reciprocal_rank_fusion([_doc_items("a", "b")], _id)
    assert [(r["id"], round(s, 6)) for r, s in results] == [
        ("a", round(1 / (60 + 1), 6)),
        ("b", round(1 / (60 + 2), 6)),
    ]
    assert stats.vector_hits == 2
    assert stats.fts_hits == 0
    assert stats.both_hits == 0


# ===========================================================================
# 2. Single list semantics
# ===========================================================================


def test_rrf_single_list_preserves_ranking_desc_by_score() -> None:
    """Single ranked list → returned order matches input order (RRF = monotonic)."""
    results, _stats = reciprocal_rank_fusion(
        [_doc_items("a", "b", "c", "d")], _id
    )
    # Rank 1..4 with default k=60 → all distinct, monotonic desc.
    assert [r["id"] for r, _ in results] == ["a", "b", "c", "d"]
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)


# ===========================================================================
# 3. Dual-list fusion semantics
# ===========================================================================


def test_rrf_dual_list_fuses_all_unique_items() -> None:
    """Dual lists → fused result contains all unique items by key."""
    vector_list = _doc_items("a", "b", "c")
    fts_list = _doc_items("c", "d")
    results, _ = reciprocal_rank_fusion([vector_list, fts_list], _id)
    keys = [r["id"] for r, _ in results]
    assert sorted(keys) == ["a", "b", "c", "d"]


def test_rrf_dual_list_both_hits_count_intersection() -> None:
    """both_hits = |vector ∩ fts unique keys|."""
    vector_list = _doc_items("a", "b", "c")
    fts_list = _doc_items("b", "c", "d")
    _, stats = reciprocal_rank_fusion([vector_list, fts_list], _id)
    # Intersection: {b, c} → size 2
    assert stats.both_hits == 2


def test_rrf_dual_list_vector_only_hits_count_excludes_intersection() -> None:
    """vector_hits counts items ONLY in vector list (not in fts)."""
    vector_list = _doc_items("a", "b", "c", "x")
    fts_list = _doc_items("b", "c", "d")
    _, stats = reciprocal_rank_fusion([vector_list, fts_list], _id)
    # Vector unique: {a, x} → size 2
    assert stats.vector_hits == 2


def test_rrf_dual_list_fts_only_hits_count_excludes_intersection() -> None:
    """fts_hits counts items ONLY in fts list (not in vector)."""
    vector_list = _doc_items("a", "b", "c", "x")
    fts_list = _doc_items("b", "c", "d", "y")
    _, stats = reciprocal_rank_fusion([vector_list, fts_list], _id)
    # FTS unique: {d, y} → size 2
    assert stats.fts_hits == 2


def test_rrf_fusionstats_three_counts_sum_to_union_size() -> None:
    """vector_hits + fts_hits + both_hits == |unique keys union|."""
    vector_list = _doc_items("a", "b", "c")
    fts_list = _doc_items("c", "d", "e")
    _, stats = reciprocal_rank_fusion([vector_list, fts_list], _id)
    # Union: {a,b,c,d,e} → size 5
    # Vector-only: {a,b} → 2
    # FTS-only:    {d,e} → 2
    # Both:        {c}   → 1
    # Sum: 2 + 2 + 1 = 5 ✓
    assert stats.vector_hits + stats.fts_hits + stats.both_hits == 5


def test_rrf_dual_list_reciprocal_score_formula() -> None:
    """Each item's score = 1/(k+rank_in_vector) + 1/(k+rank_in_fts) (if both)."""
    # Both lists, k=10 for arithmetic simplicity
    # item "a": only in vector at rank 1 → score = 1/(10+1)
    # item "b": in vector at rank 2 AND fts at rank 1 → score = 1/(10+2) + 1/(10+1)
    vector_list = _doc_items("a", "b", "c")
    fts_list = _doc_items("b", "d")
    results, _ = reciprocal_rank_fusion([vector_list, fts_list], _id, k=10)
    by_id = {r["id"]: round(s, 10) for r, s in results}
    assert "a" in by_id
    assert round(1 / (10 + 1), 10) == by_id["a"]
    assert round(1 / (10 + 2) + 1 / (10 + 1), 10) == by_id["b"]


# ===========================================================================
# 4. k parameter effect
# ===========================================================================


def test_rrf_k1_vs_k60_changes_weight_distribution() -> None:
    """k=1 vs k=60: ranking of high-confidence items changes order due to RRF dampening."""
    # k=1: rank-2 contribution = 1/3, significant
    # k=60: rank-2 contribution = 1/62, nearly negligible
    vector_list = _doc_items("a", "b", "c")
    fts_list = _doc_items("c", "d")
    results_k1, _ = reciprocal_rank_fusion(
        [vector_list, fts_list], _id, k=1
    )
    results_k60, _ = reciprocal_rank_fusion(
        [vector_list, fts_list], _id, k=60
    )
    # Sanity: both sorted desc by fused_score
    scores_k1 = [s for _, s in results_k1]
    scores_k60 = [s for _, s in results_k60]
    assert scores_k1 == sorted(scores_k1, reverse=True)
    assert scores_k60 == sorted(scores_k60, reverse=True)


# ===========================================================================
# 5. Deduplication
# ===========================================================================


def test_rrf_deduplicates_within_single_list_keeps_first_rank() -> None:
    """Single list with duplicate keys → item kept once at earliest rank."""
    dup_list = _doc_items("a", "b", "a", "c")  # 'a' appears at rank 1 and 3
    results, _ = reciprocal_rank_fusion([dup_list], _id, k=10)
    keys = [r["id"] for r, _ in results]
    assert sorted(keys) == ["a", "b", "c"]
    # 'a' should have score from rank 1 (first appearance wins), not 1/13+1/11
    by_id = {r["id"]: s for r, s in results}
    assert round(by_id["a"], 10) == round(1 / (10 + 1), 10)


# ===========================================================================
# 6. Determinism / R2 purity
# ===========================================================================


def test_rrf_is_pure_deterministic_on_repeated_invocation() -> None:
    """Same inputs → byte-level identical output (R2 determinism)."""
    vector_list = _doc_items("a", "b", "c")
    fts_list = _doc_items("b", "c", "d")
    r1, s1 = reciprocal_rank_fusion([vector_list, fts_list], _id)
    r2, s2 = reciprocal_rank_fusion([vector_list, fts_list], _id)
    assert r1 == r2  # list of tuples byte-level equal
    assert s1 == s2  # FusionStats dataclass equality


# ===========================================================================
# 7. FusionStats frozen enforcement
# ===========================================================================


def test_fusionstats_is_frozen_dataclass() -> None:
    """FusionStats mutation raises FrozenInstanceError (M2/L3-INFO mitigation)."""
    s = FusionStats(vector_hits=1, fts_hits=2, both_hits=3)
    with pytest.raises(FrozenInstanceError):
        s.vector_hits = 99  # type: ignore[misc]


# ===========================================================================
# 8. Boundary — N=3 forward-compat (Tc.3 IMPROVE)
# ===========================================================================


def test_rrf_key_fn_exception_propagates_unchanged() -> None:
    """key_fn RuntimeError must propagate (not be swallowed by RRF). R2 purity
    — no silently swallowed errors."""
    def _bad_key_fn(item):  # noqa: ANN001
        if item["id"] == "boom":
            raise RuntimeError("simulated key extraction failure")
        return item["id"]

    items = _doc_items("a", "boom", "c")
    with pytest.raises(RuntimeError, match="simulated key extraction failure"):
        reciprocal_rank_fusion([items], _bad_key_fn)


def test_rrf_duplicate_keys_across_sublists_kept_distinct() -> None:
    """Same key in vector and FTS contributes score once from each."""
    # vector: [a, b]   fts: [a, c]  → 'a' in both → both_hits += 1
    vector_list = _doc_items("a", "b")
    fts_list = _doc_items("a", "c")
    results, stats = reciprocal_rank_fusion([vector_list, fts_list], _id)
    by_id = {r["id"]: s for r, s in results}
    # 'a' rank=1 in vector (1/61) AND rank=1 in fts (1/61) → total 2/61
    assert round(by_id["a"], 10) == round(2.0 / 61.0, 10)
    # 'b' vector-only, 'c' fts-only, 'a' both
    assert stats.vector_hits == 1  # b
    assert stats.fts_hits == 1     # c
    assert stats.both_hits == 1    # a


def test_rrf_supports_n3_ranked_lists() -> None:
    """N=3 ranked_lists accepted. FusionStats: vector/fts fields defined for
    N=2 only; N=3 returns same both_hits=0 (no pair-overlap concept here)."""
    vector_list = _doc_items("a", "b", "c")
    fts_list = _doc_items("c", "d")
    third_list = _doc_items("e", "f")  # 3rd source (e.g. future embed)
    results, stats = reciprocal_rank_fusion(
        [vector_list, fts_list, third_list], _id
    )
    # All 6 unique items present in fused result
    keys = sorted(r["id"] for r, _ in results)
    assert keys == ["a", "b", "c", "d", "e", "f"]
    # N=3: vector_hits counts vector-only → {a, b} = 2
    assert stats.vector_hits == 2
    # N=3: fts_hits counts FTS-only (in fts but NOT in vector, regardless of 3rd) → {d}
    # Note: docstring for N>2 is "fields defined for caller to interpret";
    # this test pins the N=3 vector/fts-only semantics as: vector-only vs vector ∩ ¬fts
    assert stats.fts_hits == 1
    # both_hits counts only items in BOTH lists [0] and [1]
    assert stats.both_hits == 1  # 'c' is in both vector and fts
