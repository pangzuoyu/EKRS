"""T2a RED: IR parser must load form_fields / column_headers from data.jsonl.

Per Phase 12 plan §三.0:
- T1 added Optional fields to Metadata with default_factory=list
- T2a verifies IRParser (parse_document_block / parse_jsonl_file) loads them
- T2b (conditional): if any of these fail, fix ir_parser.py

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md §三.0
"""

import json

import pytest

from ekrs_rag.ingestion.ir_parser import (
    IRParseError,
    parse_document_block,
    parse_jsonl_file,
)


def _make_block_line(
    doc_id: str = "doc-1",
    block_id: str = "block-1",
    block_type: str = "text",
    form_fields=None,
    column_headers=None,
) -> str:
    """Build a minimal valid JSONL line."""
    payload = {
        "doc_id": doc_id,
        "block_id": block_id,
        "type": block_type,
        "content": {"md_preview": "hello", "raw": "hello"},
        "metadata": {"page_number": 1},
    }
    if form_fields is not None:
        payload["metadata"]["form_fields"] = form_fields
    if column_headers is not None:
        payload["metadata"]["column_headers"] = column_headers
    return json.dumps(payload)


@pytest.mark.unit
def test_ir_parser_loads_form_fields_from_data_jsonl():
    """T2a: data.jsonl metadata.form_fields survives IR parse."""
    line = _make_block_line(
        form_fields=[{"key": "SYSTEM NO", "value": "Lot 49"}]
    )
    block = parse_document_block(line)
    assert block.metadata.form_fields == [{"key": "SYSTEM NO", "value": "Lot 49"}]
    assert isinstance(block.metadata.form_fields, list)


@pytest.mark.unit
def test_ir_parser_loads_column_headers_from_data_jsonl():
    """T2a: data.jsonl metadata.column_headers survives IR parse."""
    line = _make_block_line(
        column_headers=[{"index": 0, "header": "A105"}]
    )
    block = parse_document_block(line)
    assert block.metadata.column_headers == [{"index": 0, "header": "A105"}]
    assert isinstance(block.metadata.column_headers, list)


@pytest.mark.unit
def test_ir_parser_loads_both_fields_concurrently():
    """T2a: form_fields + column_headers loaded together."""
    line = _make_block_line(
        form_fields=[{"key": "PROJECT", "value": "ACME"}],
        column_headers=[{"index": 0, "header": "Item"}],
    )
    block = parse_document_block(line)
    assert block.metadata.form_fields == [{"key": "PROJECT", "value": "ACME"}]
    assert block.metadata.column_headers == [{"index": 0, "header": "Item"}]


@pytest.mark.unit
def test_ir_parser_does_not_silently_drop_unknown_fields():
    """T2a guard: Pydantic extra='ignore' must not drop OUR declared fields.

    Document scope: We only assert that fields declared on Metadata are NOT
    dropped. Pydantic by default drops extra fields silently — we are free
    to add stricter handling later, but for T1-T2 we trust the new fields
    declared on the model to round-trip.
    """
    line = _make_block_line(
        form_fields=[{"key": "K1", "value": "V1"}],
        column_headers=[{"index": 1, "header": "H1"}],
    )
    block = parse_document_block(line)

    # Both fields populated after parse
    assert len(block.metadata.form_fields) == 1
    assert len(block.metadata.column_headers) == 1
    # Original key/value preserved (not just length)
    assert block.metadata.form_fields[0]["key"] == "K1"
    assert block.metadata.column_headers[0]["header"] == "H1"


@pytest.mark.unit
def test_ir_parser_jsonl_file_full_load(tmp_path):
    """T2a: parse_jsonl_file iterates and loads form_fields across all blocks."""
    lines = [
        _make_block_line(
            block_id=f"block-{i}",
            form_fields=[{"key": f"K{i}", "value": f"V{i}"}] if i % 2 == 0 else None,
        )
        for i in range(3)
    ]
    jsonl_path = tmp_path / "data.jsonl"
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    blocks = parse_jsonl_file(str(jsonl_path))
    assert len(blocks) == 3
    # Block 0: form_fields set
    assert blocks[0].metadata.form_fields == [{"key": "K0", "value": "V0"}]
    # Block 1: form_fields missing → default_factory=[]
    assert blocks[1].metadata.form_fields == []
    # Block 2: form_fields set
    assert blocks[2].metadata.form_fields == [{"key": "K2", "value": "V2"}]
