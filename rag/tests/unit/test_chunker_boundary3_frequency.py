"""T10b-1 Boundary 3 verification: token-overflow flush also triggers.

Per Phase 10 T10b-1 + Phase 12 coordination §V2 follow-up:
- _route_accumulated_group unifies Boundary 2 (scope-change) AND Boundary 3
  (token-overflow). Phase 10 T10b-1 chunker refactor spec mandates the two
  "force-merge" edges stay synchronized.
- V2 test (`test_chunker_boundary2_frequency.py`) covers Boundary 2.
- This test covers Boundary 3: when accumulated text exceeds max_tokens
  budget, chunker MUST flush via _route_accumulated_group → multiple chunks.

Per Phase 10 plan §GSTACK REVIEW [HIGH] finding: deep-nesting docs trigger
token-overflow more often than scope-change. Boundary 3 is therefore the more
frequent trigger in production. This test pins that behavior.

PRR coordination: docs/solutions/integration-issues/ekrs-heading-path-coord-response-2026-08-06.md §V2 follow-up
"""

from __future__ import annotations

import pytest

from ekrs_shared.models import Content, DocumentBlockIR, Metadata
from ekrs_rag.ingestion.chunker import chunk_blocks


def _make_block(text: str, heading_path: list[str] | None, block_id: str) -> DocumentBlockIR:
    """Build a minimal DocumentBlockIR with explicit heading_path."""
    return DocumentBlockIR(
        doc_id="doc-1",
        block_id=block_id,
        type="text",
        content=Content(raw=text, md_preview=text, structured={}),
        metadata=Metadata(
            page_number=1,
            heading_path=heading_path,
        ),
    )


@pytest.mark.unit
def test_boundary3_fires_when_accumulated_text_exceeds_max_tokens():
    """Boundary 3: same-scope blocks with text overflowing max_tokens → split.

    All blocks share heading_path (no Boundary 2 trigger possible — scope never
    changes). Text length is tuned to exceed max_tokens budget on the second
    accumulation, forcing Boundary 3 to flush the first group.
    """
    # max_tokens default in chunker; each block's text is large enough that
    # accumulating two blocks blows past the budget.
    long_text_a = "alpha " * 200  # ~1000 chars
    long_text_b = "beta " * 200   # ~1000 chars
    long_text_c = "gamma " * 200  # ~1000 chars

    blocks = [
        _make_block(long_text_a, ["Same Section"], "b1"),
        _make_block(long_text_b, ["Same Section"], "b2"),  # overflow flush
        _make_block(long_text_c, ["Same Section"], "b3"),
    ]
    chunks = chunk_blocks(blocks, doc_hash="d1", version=1, max_tokens=500)
    # Boundary 3 should split before block 2 → at least 2 chunks
    assert len(chunks) >= 2


@pytest.mark.unit
def test_boundary3_does_not_fire_when_text_fits_budget():
    """Boundary 3: small blocks within budget → 1 chunk (no overflow).

    All blocks share heading_path so Boundary 2 cannot fire. Text is short
    enough that the accumulated total fits under max_tokens, so Boundary 3
    also doesn't fire → single merged chunk.
    """
    blocks = [
        _make_block("text A", ["Same Section"], "b1"),
        _make_block("text B", ["Same Section"], "b2"),
        _make_block("text C", ["Same Section"], "b3"),
    ]
    chunks = chunk_blocks(blocks, doc_hash="d2", version=1, max_tokens=500)
    # Same scope, all under budget → single chunk
    assert len(chunks) == 1


@pytest.mark.unit
def test_boundary3_count_pre_vs_post_budget_simulation():
    """Boundary 3: count flush frequency under token pressure.

    Pre-budget-pressure: small blocks → 1 chunk, Boundary 3 fires 0 times.
    Post-budget-pressure: same-shape blocks with large text + low max_tokens
    → multiple chunks, Boundary 3 fires > 0 times.

    Direct comparison — pins the behavior change.
    """
    # Pre: short text, high budget
    short_blocks = [
        _make_block(f"short {i}", ["Same Section"], f"b{i}")
        for i in range(10)
    ]
    pre_chunks = chunk_blocks(short_blocks, doc_hash="d-pre", version=1, max_tokens=2000)
    # All 10 blocks fit → 1 chunk
    assert len(pre_chunks) == 1

    # Post: long text, tight budget — same scope so Boundary 2 can't fire
    long_blocks = [
        _make_block("word " * 100, ["Same Section"], f"b{i}")  # ~500 chars
        for i in range(10)
    ]
    post_chunks = chunk_blocks(long_blocks, doc_hash="d-post", version=1, max_tokens=200)
    # Each block alone exceeds 200 tokens → every block is its own chunk
    # OR boundary 3 flushes after each accumulation
    assert len(post_chunks) >= 2

    # Boundary 3 acceptance: flush frequency went from 0 to > 0
    boundary3_delta = len(post_chunks) - len(pre_chunks)
    assert boundary3_delta > 0, (
        f"Boundary 3 fail: flush delta = {boundary3_delta} (expected > 0). "
        "When accumulated text exceeds max_tokens, _route_accumulated_group "
        "must flush accumulated group into a separate chunk."
    )


@pytest.mark.unit
def test_boundary3_distinct_from_boundary2():
    """Boundary 3 is INDEPENDENT of Boundary 2.

    Critical invariant: Boundary 3 fires on token-overflow even when scope
    NEVER changes. This distinguishes it from Boundary 2 (scope-change flush).

    Test setup: 5 blocks, all with the same scope (no Boundary 2 trigger),
    each large enough to force its own chunk via Boundary 3 token-overflow.
    """
    # 5 huge blocks, same scope → no Boundary 2, but each overflows budget
    huge_blocks = [
        _make_block("word " * 200, ["Constant Scope"], f"b{i}")
        for i in range(5)
    ]
    chunks = chunk_blocks(huge_blocks, doc_hash="d3", version=1, max_tokens=100)

    # Should produce multiple chunks via Boundary 3 alone
    assert len(chunks) >= 2

    # All chunks share the same scope_path (since Boundary 2 never fired)
    scope_paths = {tuple(c.scope_path) for c in chunks}
    assert len(scope_paths) == 1, (
        f"Boundary 3 should preserve scope_path when only token-overflow "
        f"fires. Got multiple distinct scope_paths: {scope_paths}"
    )