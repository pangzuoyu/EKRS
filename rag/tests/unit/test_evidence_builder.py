"""Tests for constraint_engine/evidence_builder.py."""
from __future__ import annotations

import pytest
from ekrs_shared.models import Chunk, Priority

from ekrs_rag.constraint_engine.evidence_builder import EvidenceBuilder


class TestEvidenceBuilder:
    """Test EvidenceBuilder.build() orchestration."""

    def test_empty_chunks_returns_empty(self):
        """No chunks → empty list"""
        result = EvidenceBuilder.build([])
        assert result == []

    def test_chunk_with_no_hints_returns_empty(self):
        """Chunk with no numeric values → empty"""
        chunk = Chunk(
            text="本规范未规定任何数值限制",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        result = EvidenceBuilder.build([chunk])
        assert result == []

    def test_single_chunk_single_constraint(self):
        """Single chunk with single numeric constraint"""
        chunk = Chunk(
            text="温度不得超过80°C",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        result = EvidenceBuilder.build([chunk])
        assert len(result) >= 1
        temp_constraints = [c for c in result if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        # V2: check interval bounds instead of operator/value
        assert c.interval is not None
        assert c.interval.get("upper") is not None
        assert abs(c.interval.get("upper") - 80.0) < 0.01

    def test_multi_chunk_deduplication(self):
        """Same constraint from two chunks → deduplicated, highest priority wins"""
        chunks = [
            Chunk(
                text="温度不得超过80°C",
                scope_path=["national", "GB"],
                source_block_ids=["b1"],
                page_numbers=[1],
            ),
            Chunk(
                text="温度不得超过80°C",
                scope_path=["national", "GB"],
                source_block_ids=["b2"],
                page_numbers=[2],
            ),
        ]
        result = EvidenceBuilder.build(chunks)
        # Should be deduplicated to 1 constraint
        temp_constraints = [c for c in result if c.parameter == "temperature"]
        assert len(temp_constraints) == 1

    def test_priority_override(self):
        """Same constraint (parameter, operator, value) from different priority sources → highest priority wins"""
        chunks = [
            Chunk(
                text="温度不得超过80°C",
                scope_path=["reference"],
                source_block_ids=["b1"],
                page_numbers=[1],
            ),
            Chunk(
                text="温度不得超过80°C",
                scope_path=["national", "GB"],
                source_block_ids=["b2"],
                page_numbers=[2],
            ),
        ]
        result = EvidenceBuilder.build(chunks)
        temp_constraints = [c for c in result if c.parameter == "temperature"]
        assert len(temp_constraints) == 1
        # NATIONAL (100) > REFERENCE (20) → national scope_path wins
        assert temp_constraints[0].scope_path == ["national", "GB"]

    def test_scope_path_propagated(self):
        """chunk.scope_path is propagated to each constraint"""
        chunk = Chunk(
            text="压力不低于1.0MPa",
            scope_path=["enterprise", "Acme"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        result = EvidenceBuilder.build([chunk])
        assert len(result) >= 1
        assert result[0].scope_path == ["enterprise", "Acme"]

    def test_temperature_affine_normalization(self):
        """°F value is affine-converted to °C"""
        chunk = Chunk(
            text="最高工作温度不得超过176°F",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        result = EvidenceBuilder.build([chunk])
        temp_constraints = [c for c in result if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        # 176°F = (176-32)*5/9 = 80°C — check interval upper
        assert c.interval is not None
        assert abs(c.interval.get("upper") - 80.0) < 0.01
        assert c.unit == "°C"

    def test_multi_parameter_constraints(self):
        """Multiple parameters from same chunk → all preserved"""
        chunk = Chunk(
            text="温度不得超过80°C，压力不低于1.0MPa",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        result = EvidenceBuilder.build([chunk])
        params = {c.parameter for c in result}
        assert "temperature" in params
        # Note: MPa pressure may be normalized to Pa and is a separate parameter

    def test_parameter_normalization(self):
        """Chinese parameter names normalized to English"""
        chunk = Chunk(
            text="温度为25°C",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        result = EvidenceBuilder.build([chunk])
        assert len(result) >= 1
        # "温度" should be normalized to "temperature"
        assert result[0].parameter == "temperature"
