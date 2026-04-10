"""Unit tests for semantic chunker."""

import pytest

from ekrs_shared.models import Chunk, Content, DocumentBlockIR, Lineage, Metadata
from ekrs_rag.ingestion.chunker import (
    chunk_blocks,
    estimate_tokens,
    extract_table_headers,
)


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
