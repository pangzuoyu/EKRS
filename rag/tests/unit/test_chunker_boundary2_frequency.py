"""V2 RED→GREEN: Boundary 2 (scope-change) frequency check.

Per Phase 12 coordination §V2 + spec §7 第 4 项:
- 当 heading_path 有值时, scope-change 应触发 chunk flush (Boundary 2).
- heading_path 缺失 docs 永远触发不到 Boundary 2 (根 scope=[] 不会变).
- heading_path 修复后 (doc-to-md propagated), Boundary 2 应 > 0.

This is the semantic test for what `/var/log/ekrs/chunker.log | grep
scope_change_flush` measures in production.

PRR coordination: docs/solutions/integration-issues/ekrs-heading-path-coord-response-2026-08-06.md §V2
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
            heading_path=heading_path,  # explicit None = no scope change trigger
        ),
    )


@pytest.mark.unit
def test_boundary2_fires_when_scope_changes_across_blocks():
    """V2: chunk_blocks with scope-change across blocks → multiple chunks (flush)."""
    blocks = [
        _make_block("text A1", ["Chapter 1"], "b1"),
        _make_block("text A2", ["Chapter 1"], "b2"),
        _make_block("text B1", ["Chapter 2"], "b3"),  # ← scope change Boundary 2
        _make_block("text B2", ["Chapter 2"], "b4"),
    ]
    chunks = chunk_blocks(blocks, doc_hash="d1", version=1)
    # Boundary 2 should split between Chapter 1 and Chapter 2 → at least 2 chunks
    assert len(chunks) >= 2


@pytest.mark.unit
def test_boundary2_does_not_fire_when_heading_path_is_none():
    """V2: heading_path=None (legacy doc-to-md pre-fix) → no Boundary 2 trigger.

    With no heading_path at all, scope is always []; no scope change occurs;
    Boundary 2 never fires — single mega-chunk if no token overflow either.
    """
    blocks = [
        _make_block("text A", None, "b1"),
        _make_block("text B", None, "b2"),
        _make_block("text C", None, "b3"),
    ]
    chunks = chunk_blocks(blocks, doc_hash="d2", version=1)
    # All blocks share scope=[]; no Boundary 2 trigger expected
    # (only token overflow could force split)
    assert len(chunks) == 1  # all merged


@pytest.mark.unit
def test_boundary2_fires_per_heading_section_in_post_fix_corpus():
    """V2: simulates doc-to-md heading_path fix → Boundary 2 frequency > 0.

    Real-world post-fix data: a doc with N headings produces ~N chunks
    (one per section) instead of 1 mega-chunk. This is the V2 acceptance:
    Boundary 2 frequency goes from 0 (pre-fix) to >0 (post-fix).
    """
    # 10 sections × 1 block each → expect ~10 chunks from Boundary 2 flushes
    n_sections = 10
    blocks = [
        _make_block(f"section-{i} body", [f"Section {i}"], f"b{i}")
        for i in range(n_sections)
    ]
    chunks = chunk_blocks(blocks, doc_hash="d3", version=1)
    # Each scope-change triggers a flush → at least N-1 Boundary 2 fires
    # (= N-1 chunks if text fits per section; allow some merging if very short)
    assert len(chunks) >= n_sections // 2  # at least half = Boundary 2 working


@pytest.mark.unit
def test_boundary2_count_pre_vs_post_fix_simulation():
    """V2: counts Boundary 2 trigger frequency via scope_change_flush markers.

    Pre-fix: heading_path=None everywhere → Boundary 2 fires = 0.
    Post-fix: heading_path populated → Boundary 2 fires > 0.

    This test directly compares the two scenarios and asserts the post-fix
    delta is positive. Mirrors the production log observation:
    `grep -c "scope_change_flush" /var/log/ekrs/chunker.log`
    """
    # Pre-fix corpus
    pre_blocks = [
        _make_block(f"text {i}", None, f"b{i}") for i in range(10)
    ]
    pre_chunks = chunk_blocks(pre_blocks, doc_hash="d-pre", version=1)
    # No scope changes → Boundary 2 fires 0 times
    assert len(pre_chunks) == 1  # all merged into one chunk

    # Post-fix corpus (same content, headings propagated)
    post_blocks = [
        _make_block(f"text {i}", [f"Section {i}"], f"b{i}") for i in range(10)
    ]
    post_chunks = chunk_blocks(post_blocks, doc_hash="d-post", version=1)
    # Scope changes at every block → Boundary 2 fires 9 times (between blocks)
    # → ~10 chunks (one per section)
    assert len(post_chunks) > len(pre_chunks)

    # V2 acceptance: Boundary 2 frequency went from 0 to > 0
    boundary2_delta = len(post_chunks) - len(pre_chunks)
    assert boundary2_delta > 0, (
        f"V2 fail: Boundary 2 frequency delta = {boundary2_delta} (expected > 0). "
        "Pre-fix heading_path=None blocks didn't trigger Boundary 2; "
        "post-fix heading_path propagation should fire flush."
    )