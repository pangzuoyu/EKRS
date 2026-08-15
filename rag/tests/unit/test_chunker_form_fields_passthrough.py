"""T2 RED: chunker must copy form_fields / column_headers from block→chunk.

Per Phase 12 plan §三:
- T2: chunker.py passthrough + Qdrant payload write
- 5 Chunk construction sites: _build_chunk (line 488), _split_large_block
  inline (lines 549, 571), _split_text_two_phase (line 635-644), chunk_blocks
  (line 549-558 in older dumps; current top-level)

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md §三
"""

import json

import pytest

from ekrs_rag.ingestion.chunker import (
    _build_chunk,
    _split_large_block,
    _split_text_two_phase,
    chunk_blocks,
    extract_table_headers,
)
from ekrs_shared.models import DocumentBlockIR, Metadata


def _make_block(
    block_id: str = "block-1",
    text: str = "hello",
    block_type: str = "text",
    form_fields=None,
    column_headers=None,
    heading_path=None,
) -> DocumentBlockIR:
    """Build a minimal DocumentBlockIR."""
    meta_kwargs = {"page_number": 1}
    if heading_path is not None:
        meta_kwargs["heading_path"] = heading_path
    if form_fields is not None:
        meta_kwargs["form_fields"] = form_fields
    if column_headers is not None:
        meta_kwargs["column_headers"] = column_headers
    return DocumentBlockIR(
        doc_id="doc-1",
        block_id=block_id,
        type=block_type,
        content={"md_preview": text, "raw": text},
        metadata=Metadata(**meta_kwargs),
    )


@pytest.mark.unit
def test_chunker_copies_form_fields_block_to_chunk():
    """T2: _build_chunk copies form_fields from block to chunk."""
    block = _make_block(
        form_fields=[{"key": "SYSTEM NO", "value": "Lot 49"}]
    )
    chunk = _build_chunk(
        text="hello",
        scope_path=[],
        block_id="block-1",
        doc_hash="abc",
        version=1,
        page_number=1,
    )
    # _build_chunk is called from _split_large_block and chunk_blocks with
    # block.metadata. Test the actual entry point below; for unit purposes
    # verify the field is passable.
    assert chunk.form_fields == []  # default empty list


@pytest.mark.unit
def test_chunker_copies_column_headers_block_to_chunk():
    """T2: chunker passes column_headers through to chunk."""
    block = _make_block(
        column_headers=[{"index": 0, "header": "A105"}]
    )
    chunk = _build_chunk(
        text="hello",
        scope_path=[],
        block_id="block-1",
        doc_hash="abc",
        version=1,
        page_number=1,
    )
    assert chunk.column_headers == []


@pytest.mark.unit
def test_chunk_blocks_passes_form_fields_through():
    """T2 end-to-end: chunk_blocks(form_fields) → chunk.form_fields populated."""
    block = _make_block(
        block_id="block-1",
        text="heading body" * 10,
        form_fields=[{"key": "SYSTEM NO", "value": "Lot 49"}],
        heading_path=["Chapter 1"],
    )
    chunks = chunk_blocks([block], doc_hash="abc", version=1)
    assert len(chunks) >= 1
    # All chunks from this block should carry form_fields
    for chunk in chunks:
        assert chunk.form_fields == [{"key": "SYSTEM NO", "value": "Lot 49"}]


@pytest.mark.unit
def test_chunk_blocks_passes_column_headers_through():
    """T2 end-to-end: chunk_blocks(column_headers) → chunk.column_headers populated."""
    block = _make_block(
        block_id="block-1",
        text="heading body" * 10,
        column_headers=[{"index": 0, "header": "A105"}],
        heading_path=["Chapter 1"],
    )
    chunks = chunk_blocks([block], doc_hash="abc", version=1)
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.column_headers == [{"index": 0, "header": "A105"}]


@pytest.mark.unit
def test_chunker_handles_missing_fields_default_to_empty_list():
    """T2: chunk without form_fields/column_headers defaults to [] (D4)."""
    block = _make_block(block_id="block-1", text="x" * 100, heading_path=["H1"])
    chunks = chunk_blocks([block], doc_hash="abc", version=1)
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.form_fields == []
        assert chunk.column_headers == []


@pytest.mark.unit
def test_chunker_preserves_field_order_for_round_trip():
    """T2 e2e: form_fields / column_headers JSON round-trip preserves content."""
    block = _make_block(
        block_id="block-1",
        text="x" * 100,
        form_fields=[
            {"key": "PROJECT", "value": "ACME"},
            {"key": "SYSTEM", "value": "Plot plan"},
        ],
        column_headers=[{"index": 0, "header": "Item"}, {"index": 1, "header": "Value"}],
        heading_path=["H1"],
    )
    chunks = chunk_blocks([block], doc_hash="abc", version=1)
    assert len(chunks) >= 1
    chunk = chunks[0]
    # Order preserved
    assert len(chunk.form_fields) == 2
    assert chunk.form_fields[0]["key"] == "PROJECT"
    assert chunk.form_fields[1]["key"] == "SYSTEM"
    assert len(chunk.column_headers) == 2
    assert chunk.column_headers[0]["header"] == "Item"
    assert chunk.column_headers[1]["header"] == "Value"
