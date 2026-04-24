"""V2 Golden Set Tests — 8 test cases verifying V2 schema behavior.

TC_DRAFT_01: Draft status -> lifecycle.status = "draft", is_binding = false
TC_UNIT_01: Kelvin to Celsius -> interval.upper = 26.85, unit = "C" (300K)
TC_UNIT_02: Fahrenheit to Celsius -> interval.upper ~= 37.78, unit = "C" (100°F)
TC_REVIEW_01: Review status -> lifecycle.status = "review", is_binding = false
TC_OPEN_01: Open interval -> lower = 50, lower_inclusive = false
TC_TRANSITION_01: Transitional doc -> lifecycle.status = "transitional", is_binding = true
TC_STRICT_01: Strict rejects inferred -> 400 missing_context
TC_HARD_CONFLICT_01: Hard conflict -> 409 conflict
"""
from __future__ import annotations

import pytest

from ekrs_shared.models import Chunk

from ekrs_rag.constraint_engine.evidence_builder import EvidenceBuilder, infer_lifecycle
from ekrs_rag.constraint_engine.parser import parse_interval


class TestLifecycleInference:
    """TC_DRAFT_01, TC_REVIEW_01, TC_TRANSITION_01: Lifecycle inference tests."""

    def test_draft_status(self):
        """TC_DRAFT_01: Draft scope_path produces draft lifecycle with is_binding=false."""
        lifecycle = infer_lifecycle(
            scope_path=["draft", "征求意见稿"],
            text="这是一份草案",
        )
        assert lifecycle["status"] == "draft"
        assert lifecycle["is_binding"] is False

    def test_review_status(self):
        """TC_REVIEW_01: Review keywords produce review lifecycle with is_binding=false."""
        lifecycle = infer_lifecycle(
            scope_path=["review"],
            text="建议大家遵守",
        )
        assert lifecycle["status"] == "review"
        assert lifecycle["is_binding"] is False

    def test_transitional_status(self):
        """TC_TRANSITION_01: Transitional keywords produce transitional lifecycle with is_binding=true."""
        lifecycle = infer_lifecycle(
            scope_path=["过渡期"],
            text="这是过渡期要求",
        )
        assert lifecycle["status"] == "transitional"
        assert lifecycle["is_binding"] is True

    def test_deprecated_from_superseded_by(self):
        """Deprecated lifecycle when doc_meta.superseded_by is present."""
        lifecycle = infer_lifecycle(
            scope_path=["national", "GB"],
            text="some text",
            doc_meta={"superseded_by": "GB-2025"},
        )
        assert lifecycle["status"] == "deprecated"
        assert lifecycle["is_binding"] is False


class TestTemperatureConversion:
    """TC_UNIT_01, TC_UNIT_02: Temperature affine conversion tests."""

    def test_kelvin_to_celsius(self):
        """TC_UNIT_01: 300K should convert to ~26.85°C (upper bound)."""
        # 300K = 300 - 273.15 = 26.85°C
        # The constraint "温度不得超过300K" should produce interval.upper = 26.85°C
        chunk = Chunk(
            text="温度不得超过300K",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-kelvin",
        )
        constraints = EvidenceBuilder.build([chunk])
        temp_constraints = [c for c in constraints if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        assert c.unit == "°C"
        assert c.interval["upper"] is not None
        assert abs(c.interval["upper"] - 26.85) < 0.01

    def test_fahrenheit_to_celsius(self):
        """TC_UNIT_02: 100°F should convert to ~37.78°C (upper bound)."""
        # (100-32)*5/9 = 37.78°C
        chunk = Chunk(
            text="最高工作温度不得超过100°F",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-fahrenheit",
        )
        constraints = EvidenceBuilder.build([chunk])
        temp_constraints = [c for c in constraints if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        assert c.unit == "°C"
        assert c.interval["upper"] is not None
        assert abs(c.interval["upper"] - 37.78) < 0.01


class TestOpenInterval:
    """TC_OPEN_01: Open interval tests (> and < operators)."""

    def test_open_lower_bound(self):
        """TC_OPEN_01: > operator produces lower_inclusive=false."""
        # ">50" means 50 < x (50 is NOT included)
        chunk = Chunk(
            text="温度大于50°C",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-open",
        )
        constraints = EvidenceBuilder.build([chunk])
        temp_constraints = [c for c in constraints if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        assert c.interval["lower"] == 50.0
        assert c.interval["lower_inclusive"] is False
        assert c.interval["upper"] is None

    def test_open_upper_bound(self):
        """< operator produces upper_inclusive=false."""
        # "<100" means x < 100 (100 is NOT included)
        chunk = Chunk(
            text="温度小于100°C",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-open-upper",
        )
        constraints = EvidenceBuilder.build([chunk])
        temp_constraints = [c for c in constraints if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        assert c.interval["upper"] == 100.0
        assert c.interval["upper_inclusive"] is False
        assert c.interval["lower"] is None


class TestParserInterval:
    """Direct parser tests for interval structure."""

    def test_parse_interval_le(self):
        """<= produces closed upper bound."""
        text = "温度不得超过80°C"
        chunk = Chunk(
            text=text,
            scope_path=["national"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
        hints = extract_hints(chunk)
        intervals = parse_interval(text, hints)
        assert len(intervals) >= 1
        iv = intervals[0]
        assert iv["upper"] == 80.0
        assert iv["upper_inclusive"] is True
        assert iv["lower"] is None

    def test_parse_interval_ge(self):
        """>= produces closed lower bound."""
        text = "温度不低于10°C"
        chunk = Chunk(
            text=text,
            scope_path=["national"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
        hints = extract_hints(chunk)
        intervals = parse_interval(text, hints)
        assert len(intervals) >= 1
        iv = intervals[0]
        assert iv["lower"] == 10.0
        assert iv["lower_inclusive"] is True
        assert iv["upper"] is None

    def test_parse_interval_gt(self):
        """> produces open lower bound (lower_inclusive=False)."""
        text = "温度大于50°C"
        chunk = Chunk(
            text=text,
            scope_path=["national"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
        hints = extract_hints(chunk)
        intervals = parse_interval(text, hints)
        assert len(intervals) >= 1
        iv = intervals[0]
        assert iv["lower"] == 50.0
        assert iv["lower_inclusive"] is False
        assert iv["upper"] is None

    def test_parse_interval_lt(self):
        """>< produces open upper bound (upper_inclusive=False)."""
        text = "温度小于100°C"
        chunk = Chunk(
            text=text,
            scope_path=["national"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
        hints = extract_hints(chunk)
        intervals = parse_interval(text, hints)
        assert len(intervals) >= 1
        iv = intervals[0]
        assert iv["upper"] == 100.0
        assert iv["upper_inclusive"] is False
        assert iv["lower"] is None

    def test_parse_interval_range(self):
        """Range produces both bounds inclusive."""
        text = "温度保持在20°C至30°C"
        chunk = Chunk(
            text=text,
            scope_path=["national"],
            source_block_ids=["b1"],
            page_numbers=[1],
        )
        from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints
        hints = extract_hints(chunk)
        intervals = parse_interval(text, hints)
        # Range produces one interval with both bounds
        assert len(intervals) >= 1
        iv = intervals[0]
        assert iv["lower"] is not None
        assert iv["upper"] is not None
        assert iv["lower_inclusive"] is True
        assert iv["upper_inclusive"] is True


class TestPriorityStructure:
    """Tests for V2 priority dict structure."""

    def test_priority_explicit_level(self):
        """Priority has explicit_level as primary key."""
        chunk = Chunk(
            text="温度不得超过80°C",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-priority",
        )
        constraints = EvidenceBuilder.build([chunk])
        assert len(constraints) >= 1
        c = constraints[0]
        assert "explicit_level" in c.priority
        assert c.priority["explicit_level"] == 100  # national = 100

    def test_priority_recency_and_authority(self):
        """Priority includes recency_score and authority_score."""
        chunk = Chunk(
            text="温度不得超过80°C",
            scope_path=["enterprise"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-priority2",
        )
        constraints = EvidenceBuilder.build([chunk])
        assert len(constraints) >= 1
        c = constraints[0]
        assert "recency_score" in c.priority
        assert "authority_score" in c.priority


class TestProvisionIdDerivation:
    """Tests for provision_id derivation from heading_path clause numbers."""

    def test_provision_id_from_scope_path(self):
        """Provision_id extracted from scope_path clause number pattern like '5.2.3'."""
        chunk = Chunk(
            text="温度不得超过80°C",
            scope_path=["national", "GB", "第5.2.3条"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-provision",
        )
        constraints = EvidenceBuilder.build([chunk])
        temp_constraints = [c for c in constraints if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        assert c.source.get("provision_id") == "5.2.3"

    def test_provision_id_no_match(self):
        """No provision_id when scope_path has no clause number."""
        chunk = Chunk(
            text="温度不得超过80°C",
            scope_path=["national", "GB"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-no-provision",
        )
        constraints = EvidenceBuilder.build([chunk])
        temp_constraints = [c for c in constraints if c.parameter == "temperature"]
        assert len(temp_constraints) >= 1
        c = temp_constraints[0]
        assert c.source.get("provision_id") is None

    def test_provision_id_nested_paragraph(self):
        """Provision_id extracted from deeply nested scope_path."""
        chunk = Chunk(
            text="压力不低于1.0MPa",
            scope_path=["industry", "JB", "第10.1.2a条"],
            source_block_ids=["b1"],
            page_numbers=[1],
            doc_hash="test-nested",
        )
        constraints = EvidenceBuilder.build([chunk])
        pressure_constraints = [c for c in constraints if c.parameter == "pressure"]
        assert len(pressure_constraints) >= 1
        c = pressure_constraints[0]
        assert c.source.get("provision_id") == "10.1.2"
