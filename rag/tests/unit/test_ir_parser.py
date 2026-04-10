"""Unit tests for IR parser (DocumentBlock IR JSONL parsing)."""

import json
import os
import tempfile

import pytest

from ekrs_rag.ingestion.ir_parser import (
    IRParseError,
    extract_metadata,
    extract_text,
    parse_document_block,
    parse_jsonl_file,
)


def _valid_block_json(**overrides) -> str:
    data = {
        "doc_id": "doc1",
        "block_id": "b001",
        "type": "text",
        "content": {"raw": "raw text", "md_preview": "preview text"},
        "metadata": {"page_number": 1},
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class TestParseDocumentBlock:
    def test_valid_text_block(self):
        block = parse_document_block(_valid_block_json())
        assert block.doc_id == "doc1"
        assert block.type == "text"
        assert block.content.md_preview == "preview text"

    def test_valid_table_block(self):
        line = _valid_block_json(
            type="table",
            content={
                "raw": "",
                "md_preview": "| a | b |",
                "structured": [["a", "b"], ["1", "2"]],
            },
        )
        block = parse_document_block(line)
        assert block.type == "table"
        assert block.content.structured == [["a", "b"], ["1", "2"]]

    def test_valid_with_heading_path(self):
        line = _valid_block_json(
            metadata={"page_number": 5, "heading_path": ["Ch1", "Sec1.1"]},
        )
        block = parse_document_block(line)
        assert block.metadata.heading_path == ["Ch1", "Sec1.1"]

    def test_empty_line(self):
        with pytest.raises(IRParseError, match="Empty line"):
            parse_document_block("")

    def test_whitespace_line(self):
        with pytest.raises(IRParseError, match="Empty line"):
            parse_document_block("   \n")

    def test_invalid_json(self):
        with pytest.raises(IRParseError, match="Invalid JSON"):
            parse_document_block("{not json}")

    def test_missing_doc_id(self):
        line = json.dumps({"block_id": "b1", "type": "text", "content": {}})
        with pytest.raises(IRParseError, match="Missing required field: doc_id"):
            parse_document_block(line)

    def test_missing_block_id(self):
        line = json.dumps({"doc_id": "d1", "type": "text", "content": {}})
        with pytest.raises(IRParseError, match="Missing required field: block_id"):
            parse_document_block(line)

    def test_missing_type(self):
        line = json.dumps({"doc_id": "d1", "block_id": "b1", "content": {}})
        with pytest.raises(IRParseError, match="Missing required field: type"):
            parse_document_block(line)

    def test_missing_content_defaults(self):
        """Content defaults to empty strings if missing."""
        line = json.dumps({"doc_id": "d1", "block_id": "b1", "type": "text"})
        block = parse_document_block(line)
        assert block.content.md_preview == ""
        assert block.content.raw == ""

    def test_chinese_text(self):
        line = _valid_block_json(
            content={"raw": "混凝土养护温度", "md_preview": "混凝土养护温度"},
        )
        block = parse_document_block(line)
        assert "混凝土" in block.content.md_preview


class TestExtractText:
    def test_md_preview_preferred(self):
        from ekrs_shared.models import DocumentBlockIR, Content
        block = DocumentBlockIR(
            doc_id="d1", block_id="b1", type="text",
            content=Content(raw="raw", md_preview="preview"),
        )
        assert extract_text(block) == "preview"

    def test_raw_fallback(self):
        from ekrs_shared.models import DocumentBlockIR, Content
        block = DocumentBlockIR(
            doc_id="d1", block_id="b1", type="text",
            content=Content(raw="raw text", md_preview=""),
        )
        assert extract_text(block) == "raw text"

    def test_empty_both(self):
        from ekrs_shared.models import DocumentBlockIR, Content
        block = DocumentBlockIR(
            doc_id="d1", block_id="b1", type="text",
            content=Content(),
        )
        assert extract_text(block) == ""


class TestExtractMetadata:
    def test_full_metadata(self):
        from ekrs_shared.models import DocumentBlockIR, Content, Metadata
        block = DocumentBlockIR(
            doc_id="doc1",
            block_id="b001",
            type="table",
            content=Content(md_preview="test"),
            metadata=Metadata(page_number=5, heading_path=["Ch1", "Sec1"]),
        )
        meta = extract_metadata(block)
        assert meta["page_number"] == 5
        assert meta["heading_path"] == ["Ch1", "Sec1"]
        assert meta["block_id"] == "b001"
        assert meta["type"] == "table"

    def test_default_metadata(self):
        from ekrs_shared.models import DocumentBlockIR, Content
        block = DocumentBlockIR(
            doc_id="doc1", block_id="b001", type="text",
            content=Content(md_preview="test"),
        )
        meta = extract_metadata(block)
        assert meta["page_number"] == 1
        assert meta["heading_path"] == []


class TestParseJsonlFile:
    def test_valid_file(self):
        lines = [
            _valid_block_json(block_id="b1", content={"md_preview": "text 1"}),
            _valid_block_json(block_id="b2", content={"md_preview": "text 2"}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            blocks = parse_jsonl_file(f.name)

        os.unlink(f.name)
        assert len(blocks) == 2
        assert blocks[0].block_id == "b1"
        assert blocks[1].block_id == "b2"

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("")
            f.flush()
            blocks = parse_jsonl_file(f.name)

        os.unlink(f.name)
        assert blocks == []

    def test_invalid_line_fails_fast(self):
        lines = [
            _valid_block_json(block_id="b1"),
            "{invalid json}",
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            with pytest.raises(IRParseError, match="Line 2"):
                parse_jsonl_file(f.name)

        os.unlink(f.name)

    def test_skips_blank_lines(self):
        lines = [
            _valid_block_json(block_id="b1"),
            "",
            "   ",
            _valid_block_json(block_id="b2"),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            blocks = parse_jsonl_file(f.name)

        os.unlink(f.name)
        assert len(blocks) == 2
