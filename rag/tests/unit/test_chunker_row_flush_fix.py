"""Phase 12 row-flush fix: guarantee every output chunk has token_count <= max_tokens.

Background: chunker._split_large_block had a row-flush bug at the pre-fix
site (was line 597) — when a single row's token count exceeded max_tokens,
the row was appended without flush, accumulating until the next flush
produced oversized chunks (1500-2200 tokens vs max_tokens=500). The
historical wedge manifested on pathological 267-row tables from
doc-to-md output, where each row ~1200 tokens (repeated OCR-degraded
cells like "A: AIR COMPRESSOR" × 268). bge-m3 ONNX encode on these
oversized chunks exceeded the 600s status timeout and pinned one CPU.

Patch 1 fixes this with two protections:
  1. Pre-check row_tokens > max_tokens: flush current buffer, then
     force-split the oversized row alone via _split_text_two_phase.
     Sub-chunks each carry quality_warning=True (source-quality hint;
     data is still indexed, not dropped).
  2. Post-flush re-check on the joined buffer: if header + near-cap
     rows combine to >max_tokens, force-split instead of emitting a
     single oversized chunk.

The 3 tests below pin these guarantees. Regression guard against the
historical wedge; if any chunk.token_count > max_tokens, the test
fails and bge-m3 will wedge again.
"""

import pytest

from ekrs_rag.ingestion.chunker import _split_large_block
from ekrs_shared.models import Chunk, Content, DocumentBlockIR, Lineage, Metadata


def _make_block(
    *,
    block_id: str = "b001",
    type: str = "table",
    raw: str = "",
    md_preview: str = "",
    structured=None,
    page_number: int = 1,
    heading_path: list[str] | None = None,
) -> DocumentBlockIR:
    return DocumentBlockIR(
        doc_id="test_doc",
        block_id=block_id,
        type=type,
        content=Content(raw=raw, md_preview=md_preview, structured=structured),
        metadata=Metadata(page_number=page_number, heading_path=heading_path),
        lineage=Lineage(),
    )


# Chunk size used by Phase 9 default (DEFAULT_MAX_CHUNK_TOKENS=768) and
# matches the prior 500-token limit that triggered the historical wedge.
# Use 500 here to keep the test brittle against the exact failure mode.
MAX_TOKENS = 500


@pytest.mark.unit
class TestRowFlushNormalRows:
    """Normal-sized rows that fit inside max_tokens produce one chunk per
    buffer-fill with quality_warning=False. No regression vs prior behavior."""

    def test_all_rows_fit_single_chunk_no_quality_warning(self):
        # 5 rows × ~50 chars each = ~250 chars → 62 tokens; well under cap.
        block = _make_block(
            block_id="b001",
            structured=[
                ["参数", "值", "单位"],  # header
                ["温度", "80", "°C"],
                ["压力", "1.6", "MPa"],
                ["流量", "100", "m3/h"],
                ["材质", "A105", ""],
            ],
        )
        text = "\n".join(" | ".join(str(c) for c in row) for row in block.content.structured)
        chunks = _split_large_block(
            block=block,
            text=text,
            max_tokens=MAX_TOKENS,
            doc_hash="doc1",
            version=2,
            scope_path=["Ch1"],
            page_numbers=[1],
        )
        # All rows fit in one buffer-fill → one chunk emitted.
        assert len(chunks) == 1
        c = chunks[0]
        assert c.token_count <= MAX_TOKENS, (
            f"chunk oversize: {c.token_count} > {MAX_TOKENS}"
        )
        assert c.quality_warning is False, (
            "Normal rows must NOT carry quality_warning"
        )

    def test_multiple_buffer_fills_all_under_cap(self):
        # 30 rows × ~110 chars = ~3300 chars → ~825 tokens > cap → multiple fills.
        rows = [["参数", "值", "单位"]] + [
            [f"行{i:03d}", "value" + "x" * 100, "MPa"] for i in range(30)
        ]
        block = _make_block(
            block_id="b002",
            structured=rows,
        )
        text = "\n".join(" | ".join(str(c) for c in row) for row in rows)
        chunks = _split_large_block(
            block=block,
            text=text,
            max_tokens=MAX_TOKENS,
            doc_hash="doc2",
            version=2,
            scope_path=["Ch1"],
            page_numbers=[1],
        )
        assert len(chunks) >= 2, "30 large rows should split across multiple chunks"
        for c in chunks:
            assert c.token_count <= MAX_TOKENS, (
                f"chunk oversize: {c.token_count} > {MAX_TOKENS}"
            )
            assert c.quality_warning is False, (
                "Normal-sized row flushes must NOT carry quality_warning"
            )


@pytest.mark.unit
class TestRowFlushOversizedRow:
    """A single row that exceeds max_tokens is force-split via
    _split_text_two_phase; sub-chunks each ≤max_tokens and carry
    quality_warning=True (source-quality hint, not a skip)."""

    def test_single_oversized_row_force_split_with_quality_warning(self):
        # One row with ~3000 chars = ~750 tokens > MAX_TOKENS=500.
        big_cell = "x" * 3000
        block = _make_block(
            block_id="b003",
            structured=[
                ["参数", "值"],  # header
                ["巨型", big_cell],  # oversized row
            ],
        )
        text = "\n".join(" | ".join(str(c) for c in row) for row in block.content.structured)
        chunks = _split_large_block(
            block=block,
            text=text,
            max_tokens=MAX_TOKENS,
            doc_hash="doc3",
            version=2,
            scope_path=["Ch1"],
            page_numbers=[1],
        )
        # The oversized row force-splits into multiple sub-chunks.
        assert len(chunks) >= 2, (
            f"Oversized row should force-split; got {len(chunks)} chunk(s)"
        )
        for c in chunks:
            assert c.token_count <= MAX_TOKENS, (
                f"force-split chunk oversize: {c.token_count} > {MAX_TOKENS}"
            )
        # All force-split sub-chunks carry quality_warning (pathological data
        # hint). The header-only chunk flushed before the oversized row is a
        # NORMAL flush and must NOT carry quality_warning.
        qw_count = sum(1 for c in chunks if c.quality_warning)
        flagged = [c for c in chunks if c.quality_warning]
        assert qw_count >= 2, (
            f"Oversized row should force-split into ≥2 flagged sub-chunks; "
            f"got {qw_count} flagged out of {len(chunks)} total"
        )
        # Header context is NOT prepended to the force-split text (cleaner
        # semantic: only the pathological row carries the source-quality
        # warning; header metadata propagates via column_headers/scope_path).
        for c in flagged:
            assert "参数 | 值" not in c.text, (
                "Header fragments must NOT carry quality_warning (false-positive "
                "on non-pathological metadata)"
            )

    def test_buffer_flushed_before_force_split(self):
        # Buffer has 2 normal rows; then comes an oversized row.
        # Pre-check must flush buffer first (quality_warning=False),
        # THEN force-split oversized row alone (quality_warning=True).
        big_cell = "y" * 2500  # ~625 tokens > 500
        block = _make_block(
            block_id="b004",
            structured=[
                ["参数", "值"],
                ["温度", "80"],
                ["压力", "1.6"],
                ["巨型", big_cell],  # oversized row triggers pre-check
            ],
        )
        text = "\n".join(" | ".join(str(c) for c in row) for row in block.content.structured)
        chunks = _split_large_block(
            block=block,
            text=text,
            max_tokens=MAX_TOKENS,
            doc_hash="doc4",
            version=2,
            scope_path=["Ch1"],
            page_numbers=[1],
        )
        assert len(chunks) >= 2
        for c in chunks:
            assert c.token_count <= MAX_TOKENS, (
                f"chunk oversize: {c.token_count} > {MAX_TOKENS}"
            )
        # The buffer-flushed chunk (header + 2 normal rows) must NOT carry
        # quality_warning; the force-split sub-chunks must.
        normal = [c for c in chunks if not c.quality_warning]
        flagged = [c for c in chunks if c.quality_warning]
        assert len(normal) == 1, (
            f"Expected 1 normal chunk (header + 2 small rows); got {len(normal)}"
        )
        assert len(flagged) >= 2, (
            f"Oversized row should force-split into ≥2 sub-chunks, all "
            f"quality_warning=True; got {len(flagged)} flagged"
        )


@pytest.mark.unit
class TestRowFlushPathological:
    """Regression guard for the historical 97bc380d566b681b wedge.

    267 rows × ~1200 tokens (OCR-degraded repeated cell data) used to
    produce 133 oversized chunks (1500-2200 tokens) that wedged bge-m3
    encoding past the 600s status timeout. Patch 1 must prevent this.
    """

    def test_pathological_267_rows_no_wedge(self):
        # Each row ~1200 chars = ~300 tokens, but the row text is ~2500
        # chars = ~625 tokens > MAX_TOKENS=500. Force-split per row.
        pathological_row = ["A: AIR COMPRESSOR"] + ["N/A"] * 99
        # Pad to ~2500 chars.
        pathological_row[0] = pathological_row[0] + " x" * 1200  # ~2524 chars
        rows = [["ID", "DESCRIPTION"]] + [pathological_row for _ in range(267)]
        block = _make_block(
            block_id="b005",
            structured=rows,
        )
        text = "\n".join(" | ".join(str(c) for c in row) for row in rows)
        chunks = _split_large_block(
            block=block,
            text=text,
            max_tokens=MAX_TOKENS,
            doc_hash="97bc380d566b681b",
            version=2,
            scope_path=["Ch1"],
            page_numbers=[1],
        )
        # HARD GUARANTEE: no chunk exceeds max_tokens.
        # This is the regression guard. If this assertion fails,
        # bge-m3 will wedge again on ingest.
        oversize = [c for c in chunks if c.token_count > MAX_TOKENS]
        assert not oversize, (
            f"REGRESSION: {len(oversize)} chunk(s) exceed max_tokens "
            f"(max oversize = {max((c.token_count for c in oversize), default=0)}). "
            f"This is the historical wedge pattern; bge-m3 will hang."
        )
        # 267 pathological rows × ~1+ sub-chunks each = ≥267 chunks expected.
        assert len(chunks) >= 267, (
            f"Expected ≥267 sub-chunks (one force-split per pathological row); "
            f"got {len(chunks)}"
        )
        # Most/all chunks carry quality_warning (pathological source data).
        qw_count = sum(1 for c in chunks if c.quality_warning)
        assert qw_count >= 267, (
            f"Pathological force-split chunks must all carry quality_warning; "
            f"got {qw_count}/{len(chunks)}"
        )
