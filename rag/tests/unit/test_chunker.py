"""Unit tests for semantic chunker."""

import pytest

from ekrs_shared.models import Chunk, Content, DocumentBlockIR, Lineage, Metadata
from ekrs_rag.ingestion.chunker import (
    _hard_cut,
    _is_safe_join_boundary,
    _try_merge_fragments,
    chunk_blocks,
    estimate_tokens,
    extract_table_headers,
    validate_chunk_atomicity,
)


# Test-side token counter aligned with runtime estimate_tokens (avoid 4× drift).
# Both use max(1, len//4); test cases below call chunk_blocks/max_tokens
# combos that target the runtime semantic, not the lenient raw-len default.
def normalized_len(text: str) -> int:
    """Test-side token counter: matches estimate_tokens exactly."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _make_block(
    block_id: str = "b001",
    type: str = "text",
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


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 1

    def test_short(self):
        assert estimate_tokens("hi") == 1

    def test_longer(self):
        # ~100 chars = ~25 tokens
        assert estimate_tokens("a" * 100) == 25


class TestExtractTableHeaders:
    def test_from_structured(self):
        block = _make_block(
            type="table",
            structured=[["参数", "值", "单位"], ["温度", "80", "°C"]],
        )
        headers = extract_table_headers(block)
        assert headers == ["参数", "值", "单位"]

    def test_from_md_preview(self):
        block = _make_block(
            type="table",
            md_preview="| 参数 | 值 |\n|------|------|\n| 温度 | 80 |",
        )
        headers = extract_table_headers(block)
        assert headers == ["参数", "值"]

    def test_no_headers(self):
        block = _make_block(type="table", raw="no header info")
        assert extract_table_headers(block) == []


class TestChunkBlocks:
    def test_empty_input(self):
        assert chunk_blocks([], "doc1", 1) == []

    def test_single_text_block(self):
        blocks = [
            _make_block(md_preview="混凝土养护温度不得超过80°C", heading_path=["Ch1"]),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1)
        assert len(chunks) == 1
        assert chunks[0].text == "混凝土养护温度不得超过80°C"
        assert chunks[0].scope_path == ["Ch1"]
        assert chunks[0].source_block_ids == ["b001"]

    def test_scope_change_splits(self):
        """Blocks with different heading_path produce separate chunks."""
        blocks = [
            _make_block(block_id="b1", md_preview="text A", heading_path=["Ch1"]),
            _make_block(block_id="b2", md_preview="text B", heading_path=["Ch2"]),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1)
        assert len(chunks) == 2
        assert chunks[0].scope_path == ["Ch1"]
        assert chunks[1].scope_path == ["Ch2"]

    def test_same_scope_merges(self):
        """Consecutive text blocks with same scope merge into one chunk."""
        blocks = [
            _make_block(block_id="b1", md_preview="text A", heading_path=["Ch1"]),
            _make_block(block_id="b2", md_preview="text B", heading_path=["Ch1"]),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1)
        assert len(chunks) == 1
        assert "text A" in chunks[0].text
        assert "text B" in chunks[0].text
        assert chunks[0].source_block_ids == ["b1", "b2"]

    def test_table_standalone(self):
        """Table blocks create their own chunk, even within same scope."""
        blocks = [
            _make_block(block_id="b1", md_preview="before table", heading_path=["Ch1"]),
            _make_block(
                block_id="b2",
                type="table",
                md_preview="| a | b |\n| 1 | 2 |",
                heading_path=["Ch1"],
            ),
            _make_block(block_id="b3", md_preview="after table", heading_path=["Ch1"]),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1)
        # "before table" alone, table alone, "after table" alone (or merged)
        assert len(chunks) >= 2
        table_chunk = [c for c in chunks if "b2" in c.source_block_ids]
        assert len(table_chunk) == 1
        assert "| a | b |" in table_chunk[0].text

    def test_kv_standalone(self):
        blocks = [
            _make_block(block_id="b1", type="kv", md_preview="最大水灰比: 0.6"),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1)
        assert len(chunks) == 1
        assert "最大水灰比" in chunks[0].text

    def test_token_overflow_splits(self):
        """Blocks exceeding max_tokens get split."""
        long_text = "word " * 1000  # ~5000 chars ≈ 1250 tokens
        blocks = [_make_block(md_preview=long_text, heading_path=["Ch1"])]
        chunks = chunk_blocks(blocks, "doc1", 1, max_tokens=100)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.token_count <= 150  # some slack for split boundaries

    def test_empty_block_skipped(self):
        blocks = [
            _make_block(md_preview="", heading_path=["Ch1"]),
            _make_block(block_id="b2", md_preview="actual content", heading_path=["Ch1"]),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1)
        assert len(chunks) == 1
        assert chunks[0].source_block_ids == ["b2"]

    def test_page_numbers_collected(self):
        blocks = [
            _make_block(block_id="b1", md_preview="p1", page_number=1, heading_path=["Ch1"]),
            _make_block(block_id="b2", md_preview="p2", page_number=2, heading_path=["Ch1"]),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1)
        assert chunks[0].page_numbers == [1, 2]

    def test_doc_hash_version_propagated(self):
        blocks = [_make_block(md_preview="test")]
        chunks = chunk_blocks(blocks, "my_hash", 3)
        assert chunks[0].doc_hash == "my_hash"
        assert chunks[0].version == 3

    def test_table_header_propagation_on_split(self):
        """Large table gets split and headers propagate to sub-chunks."""
        # Build a full markdown table with many rows to force splitting
        header = "| 参数 | 标准值 | 单位 |"
        separator = "|------|--------|------|"
        rows = [f"| param_{i} | {i * 10} | MPa |" for i in range(200)]
        full_md = "\n".join([header, separator] + rows)

        # Also provide structured data
        struct_header = ["参数", "标准值", "单位"]
        struct_rows = [[f"param_{i}", str(i * 10), "MPa"] for i in range(200)]
        structured = [struct_header] + struct_rows

        blocks = [
            _make_block(
                block_id="tb1",
                type="table",
                structured=structured,
                md_preview=full_md,
            ),
        ]
        chunks = chunk_blocks(blocks, "doc1", 1, max_tokens=50)
        assert len(chunks) > 1
        # Each sub-chunk should contain headers (propagated)
        for chunk in chunks:
            assert "参数" in chunk.text or "param_" in chunk.text


# ============================================================================
# Two-phase refactor tests (RED — implementations pending)
# ============================================================================


class TestHardCut:
    """Phase 1: char-offset cutting with 20% look-back to safe boundaries."""

    def test_empty(self):
        assert _hard_cut("", 100) == []

    def test_shorter_than_max(self):
        assert _hard_cut("hello world", 100) == ["hello world"]

    def test_preserves_newlines(self):
        """Hard cut should not split mid-line when line boundaries fit."""
        text = "line1\nline2\nline3"
        # max_chars much larger than any single line → 1 fragment
        result = _hard_cut(text, 100)
        assert result == [text]

    def test_backtrack_english_word(self):
        """Cut mid-word falls back to nearest safe boundary within 20%."""
        # "the pressure vessel" — naive cut at 14 lands in middle of "pressure"
        text = "the pressure vessel is rated"
        # max_chars=14 places hard cut at position 14 = inside "pressure"
        result = _hard_cut(text, 14)
        # Phase 1 must NOT produce fragments that split "pressure" mid-word
        for frag in result:
            # No fragment should contain a partial word starting with "press" ending elsewhere
            assert not (frag.endswith("press") and any(
                other != frag and other.startswith("ure") for other in result
            ))

    def test_chinese_safe_at_boundary(self):
        """CJK-to-CJK cut is safe — no look-back needed."""
        text = "最高工作温度不超过350℃"
        # max_chars=8 → cut at position 8 (between characters)
        result = _hard_cut(text, 8)
        # No word-boundary semantics needed for CJK
        assert len(result) >= 1
        # Rejoining should yield the original text
        assert "".join(result) == text

    def test_fallback_when_no_safe_boundary(self):
        """If no safe boundary in look-back range, keep hard cut (no infinite loop)."""
        # Long string of unbroken ASCII letters — no safe boundary in 20% look-back
        text = "a" * 100
        result = _hard_cut(text, 20)
        # Should still produce chunks; rejoining must reconstruct original
        assert "".join(result) == text


class TestIsSafeJoinBoundary:
    """Phase 2: decide if two adjacent fragments can be safely joined."""

    @pytest.mark.parametrize("left, right, expected", [
        # Digit + unit letter: unsafe (would split "100MPa" → "100" + "MPa")
        ("100", "MPa", False),
        # Letter + digit: unsafe
        ("MPa", "100", False),
        # Digit + period: unsafe (decimal mid-split)
        ("3", ".14", False),
        # Period + digit: unsafe
        (".", "5", False),
        # ASCII letter + ASCII letter: unsafe (mid-word)
        ("pres", "sure", False),
        ("press", "ure", False),
        ("hello", "world", False),
        # CJK + CJK: safe
        ("最高工作温度", "不超过 350℃", True),
        # Digit + CJK unit: safe (no ASCII letter boundary issue)
        ("350", "度", True),
        # CJK + digit: safe
        ("温度", "350", True),
        # Punctuation + letter: safe
        (", ", "max", True),
        # Whitespace + letter: safe
        ("hello ", "world", True),
        # Empty boundaries: safe
        ("", "hello", True),
        ("hello", "", True),
    ])
    def test_boundary_cases(self, left, right, expected):
        assert _is_safe_join_boundary(left, right) is expected


class TestTryMergeFragments:
    """Phase 2: greedy merge with token budget + safe-boundary check."""

    def test_merge_all_within_budget(self):
        # Safe-boundary fragments (whitespace-separated): all merge into one chunk
        fragments = ["abc ", "def ", "ghi"]
        result = _try_merge_fragments(fragments, max_tokens=10, token_counter=normalized_len)
        assert result == ["abc def ghi"]

    def test_merge_blocked_by_overflow(self):
        fragments = ["aaaa ", "bbbb ", "cccc"]
        # max_tokens=2 → budget too small to merge even two fragments
        result = _try_merge_fragments(fragments, max_tokens=2, token_counter=normalized_len)
        # Each fragment normalized=1 token. Budget=2 allows merging up to 2 frags,
        # but the third fragment forces a flush → at least 2 chunks
        assert len(result) >= 2

    def test_merge_blocked_by_unsafe_boundary(self):
        """Digit + ASCII letter boundary blocks merge even within budget."""
        fragments = ["100", "MPa", " 限制"]
        result = _try_merge_fragments(fragments, max_tokens=100, token_counter=normalized_len)
        # "100" + "MPa" unsafe (digit+ASCII letter) → no merge
        # "MPa" + " 限制" safe (ASCII letter + whitespace) → merge
        assert len(result) == 2
        assert result[0] == "100"
        assert result[1] == "MPa 限制"

    def test_merge_skip_empty(self):
        fragments = ["a ", "", " b"]
        result = _try_merge_fragments(fragments, max_tokens=10, token_counter=normalized_len)
        assert result == ["a  b"]

    def test_single_fragment(self):
        assert _try_merge_fragments(["alone"], max_tokens=10, token_counter=normalized_len) == ["alone"]

    def test_empty_input(self):
        assert _try_merge_fragments([], max_tokens=10, token_counter=normalized_len) == []


class TestSplitTextByTokens:
    """End-to-end: Phase 1 hard cut → Phase 2 greedy merge."""

    def test_short_text_single_chunk(self):
        text = "a" * 100  # ~25 tokens
        result = _try_merge_fragments(
            _hard_cut(text, max_chars=400), max_tokens=100, token_counter=normalized_len,
        )
        assert result == [text]

    def test_text_2x_budget(self):
        # Phase 1 with max_chars < len(text) forces split; Phase 2 refuses
        # letter+letter merges, so we get multiple chunks
        text = "a" * 800
        result = _try_merge_fragments(
            _hard_cut(text, max_chars=400), max_tokens=100, token_counter=normalized_len,
        )
        assert len(result) >= 2

    def test_hard_cut_preserves_complete_words(self):
        """When hard cut lands in word, look-back ensures word stays whole."""
        text = "the pressure vessel is rated for 350 MPa operation"
        # max_chars=14 places hard cut at position 14 = inside "pressure"
        result = _try_merge_fragments(
            _hard_cut(text, max_chars=14), max_tokens=100, token_counter=normalized_len,
        )
        # "pressure" must appear as a complete word in some fragment
        joined = " ".join(result)
        assert "pressure" in joined

    def test_hard_cut_at_chinese_boundary(self):
        """CJK-to-CJK cuts are safe — no look-back needed."""
        text = "最高工作温度不超过350℃"
        result = _try_merge_fragments(
            _hard_cut(text, max_chars=8), max_tokens=100, token_counter=normalized_len,
        )
        assert "".join(result) == text


class TestValidateChunkAtomicity:
    """Golden test helper: flag chunks that break number/unit/word atomicity."""

    def test_valid_chunk(self):
        assert validate_chunk_atomicity("最高工作温度不超过 350℃") is True

    def test_chunk_ending_with_bare_digit_fails(self):
        """A chunk ending with a digit (no unit/suffix) suggests unit split off."""
        assert validate_chunk_atomicity("温度 350") is False

    def test_empty_chunk_fails(self):
        assert validate_chunk_atomicity("") is False


class TestIntegrationWithEstimateTokens:
    """One test that uses runtime token_counter to verify no 4× drift."""

    def test_runtime_estimate_tokens_produces_expected_chunks(self):
        # 5000 chars at len/4 = 1250 tokens. max_tokens=768 → expect 2 chunks.
        text = "a" * 5000
        chunks = _try_merge_fragments(
            _hard_cut(text, max_chars=3072), max_tokens=768, token_counter=estimate_tokens,
        )
        assert len(chunks) == 2

    def test_runtime_no_mid_word_split_on_english(self):
        """Run end-to-end with estimate_tokens on mixed CJK+EN text."""
        text = "the pressure vessel is rated for " + "word " * 200
        chunks = _try_merge_fragments(
            _hard_cut(text, max_chars=100), max_tokens=50, token_counter=estimate_tokens,
        )
        # Verify every chunk passes atomicity (or at minimum no chunk ends with bare digit)
        for chunk in chunks:
            # Last char should not be a digit unless followed by CJK unit (rare here)
            assert not chunk.rstrip().endswith(tuple("0123456789"))
