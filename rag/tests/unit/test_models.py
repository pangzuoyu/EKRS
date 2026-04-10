"""Unit tests for shared Pydantic models."""

import pytest
from pydantic import ValidationError

from ekrs_shared.models import (
    Chunk,
    Condition,
    Constraint,
    Content,
    DocumentBlockIR,
    Evidence,
    IngestionNotification,
    IngestionStatus,
    Lineage,
    Metadata,
    NumericHint,
    Priority,
)


class TestDocumentBlockIR:
    def test_valid_minimal(self):
        block = DocumentBlockIR(
            doc_id="abc123",
            block_id="b001",
            type="text",
            content=Content(md_preview="hello"),
        )
        assert block.doc_id == "abc123"
        assert block.metadata.page_number == 1  # default

    def test_valid_full(self):
        block = DocumentBlockIR(
            doc_id="abc123",
            block_id="b001",
            type="table",
            content=Content(
                raw="raw text",
                structured=[["a", "b"], ["1", "2"]],
                md_preview="| a | b |\n| 1 | 2 |",
            ),
            metadata=Metadata(
                page_number=5,
                heading_path=["Chapter 1", "Section 1.1"],
            ),
            lineage=Lineage(parser_version="1.0"),
        )
        assert block.content.structured == [["a", "b"], ["1", "2"]]
        assert block.metadata.heading_path == ["Chapter 1", "Section 1.1"]

    def test_from_dict(self):
        data = {
            "doc_id": "abc",
            "block_id": "b1",
            "type": "text",
            "content": {"md_preview": "test"},
        }
        block = DocumentBlockIR(**data)
        assert block.type == "text"

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            DocumentBlockIR(doc_id="abc", type="text")  # missing block_id


class TestNumericHint:
    def test_valid(self):
        hint = NumericHint(
            parameter_hint="温度",
            value=80.0,
            unit="°C",
            span=(5, 10),
            source_text="不得超过80°C",
            block_id="b001",
        )
        assert hint.value == 80.0
        assert hint.span == (5, 10)

    def test_minimal(self):
        hint = NumericHint(value=42.0, unit="MPa", span=(0, 5))
        assert hint.parameter_hint == ""
        assert hint.scope_path is None


class TestConstraint:
    def test_simple_constraint(self):
        c = Constraint(
            parameter="temperature",
            operator="<=",
            value=80.0,
            unit="°C",
        )
        assert c.priority == Priority.PROJECT
        assert c.confidence == 1.0

    def test_range_constraint(self):
        c = Constraint(
            parameter="pressure",
            operator="range",
            value=(0.5, 1.0),
            unit="MPa",
        )
        assert c.value == (0.5, 1.0)

    def test_with_conditions(self):
        c = Constraint(
            parameter="temperature",
            operator="<=",
            value=35.0,
            unit="°C",
            conditions=[
                Condition(parameter="environment", operator="==", value="高温"),
            ],
        )
        assert len(c.conditions) == 1


class TestChunk:
    def test_minimal(self):
        chunk = Chunk(text="hello world", token_count=3)
        assert chunk.scope_path == []
        assert chunk.doc_hash == ""

    def test_full(self):
        chunk = Chunk(
            text="混凝土养护温度不得超过80°C",
            scope_path=["第3章", "3.1"],
            source_block_ids=["b001", "b002"],
            token_count=15,
            doc_hash="abc123",
            version=1,
            page_numbers=[1, 2],
        )
        assert chunk.scope_path == ["第3章", "3.1"]


class TestIngestionNotification:
    def test_valid(self):
        n = IngestionNotification(
            doc_hash="abc123",
            version=1,
            output_path="/parsed_lib/abc/2026-04-09/",
            callback_url="http://parser:8000/v1/callback",
        )
        assert n.doc_hash == "abc123"

    def test_with_trace_id(self):
        n = IngestionNotification(
            trace_id="trace-123",
            doc_hash="abc",
            version=1,
            output_path="/tmp/test",
        )
        assert n.trace_id == "trace-123"


class TestIngestionStatus:
    def test_success(self):
        s = IngestionStatus(status="success", chunks_indexed=42, version=1)
        assert s.chunks_indexed == 42
        assert s.error is None

    def test_failed(self):
        s = IngestionStatus(status="failed", error="Qdrant unavailable")
        assert s.chunks_indexed == 0
