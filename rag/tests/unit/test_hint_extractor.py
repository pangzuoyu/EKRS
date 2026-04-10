"""Tests for ingestion/numeric_hint_extractor.py."""
from __future__ import annotations

import pytest
from ekrs_shared.models import Chunk

from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints


class TestExtractHints:
    """Test NumericHint extraction from chunk text."""

    def test_basic_temperature_celsius(self):
        """°C temperature extraction"""
        chunk = Chunk(
            text="温度不得超过80°C",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        assert len(hints) >= 1
        # Find the temperature hint
        temp_hints = [h for h in hints if h.unit in ("°C", "C", "℃")]
        assert len(temp_hints) >= 1
        # Value should be 80
        assert any(abs(h.value - 80.0) < 0.01 for h in temp_hints)

    def test_temperature_fahrenheit(self):
        """°F temperature extraction"""
        chunk = Chunk(
            text="最高温度200°F",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        temp_hints = [h for h in hints if h.unit in ("°F", "F")]
        assert len(temp_hints) >= 1
        assert any(abs(h.value - 200.0) < 0.01 for h in temp_hints)

    def test_pressure_mpa(self):
        """MPa pressure extraction"""
        chunk = Chunk(
            text="压力不低于1.0MPa",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        press_hints = [h for h in hints if h.unit == "MPa"]
        assert len(press_hints) >= 1
        assert any(abs(h.value - 1.0) < 0.01 for h in press_hints)

    def test_length_mm(self):
        """mm dimension extraction"""
        chunk = Chunk(
            text="直径为25mm",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        mm_hints = [h for h in hints if h.unit == "mm"
]
        assert len(mm_hints) >= 1
        assert any(abs(h.value - 25.0) < 0.01 for h in mm_hints)

    def test_percentage(self):
        """Percentage extraction"""
        chunk = Chunk(
            text="含水率不超过15%",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        pct_hints = [h for h in hints if h.unit == "%"]
        assert len(pct_hints) >= 1
        assert any(abs(h.value - 15.0) < 0.01 for h in pct_hints)

    def test_no_numeric_value_returns_empty(self):
        """Text with no numbers returns []"""
        chunk = Chunk(
            text="本规范未规定温度限制",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        assert len(hints) == 0

    def test_span_boundaries_correct(self):
        """Span (start, end) is relative to chunk.text"""
        chunk = Chunk(
            text="温度80°C",
            scope_path=[],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        # Find the hint for "80°C"
        c_hints = [h for h in hints if "80" in h.source_text]
        assert len(c_hints) >= 1
        h = c_hints[0]
        assert h.span[0] >= 0
        assert h.span[1] <= len(chunk.text)
        assert chunk.text[h.span[0]:h.span[1]] == h.source_text

    def test_scope_path_propagated(self):
        """chunk.scope_path is copied to each hint"""
        chunk = Chunk(
            text="温度80°C",
            scope_path=["enterprise", "Acme"],
            source_block_ids=["b1", "b2"],
            page_numbers=[1, 2],
        )
        hints = extract_hints(chunk)
        assert len(hints) >= 1
        for h in hints:
            assert h.scope_path == ["enterprise", "Acme"]

    def test_source_block_ids_propagated(self):
        """chunk.source_block_ids is joined into hint.block_id"""
        chunk = Chunk(
            text="直径25mm",
            scope_path=["national", "GB"],
            source_block_ids=["b1", "b2"],
            page_numbers=[1],
        )
        hints = extract_hints(chunk)
        assert len(hints) >= 1
        # block_id should contain both source block IDs
        assert "b1" in hints[0].block_id or "b2" in hints[0].block_id

    def test_page_num_from_chunk(self):
        """page_num is taken from chunk.page_numbers"""
        chunk = Chunk(
            text="温度25°C",
            scope_path=[],
            source_block_ids=["b1"],
            page_numbers=[3],
        )
        hints = extract_hints(chunk)
        assert len(hints) >= 1
        assert hints[0].page_num == 3
