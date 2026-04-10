"""Tests for constraint_engine/parser.py — RED phase."""
from __future__ import annotations

import pytest
from ekrs_shared.models import NumericHint

from ekrs_rag.constraint_engine.parser import ConstraintParser


class TestParseConstraints:
    """Test constraint extraction from raw text using numeric hints as anchors."""

    def test_le_chinese_max(self):
        """不得超过/不超过 → <= operator"""
        text = "温度不得超过80°C"
        hints = [NumericHint(parameter_hint="温度", value=80.0, unit="°C", span=(5, 8))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        c = constraints[0]
        assert c.operator == "<="
        assert c.value == 80.0
        assert c.unit == "°C"

    def test_ge_chinese_min(self):
        """不低于/不少于 → >= operator"""
        text = "压力不低于1.0MPa"
        hints = [NumericHint(parameter_hint="压力", value=1.0, unit="MPa", span=(5, 9))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        c = constraints[0]
        assert c.operator == ">="

    def test_eq_chinese(self):
        """等于/为 → == operator"""
        text = "直径为25mm"
        hints = [NumericHint(parameter_hint="直径", value=25.0, unit="mm", span=(3, 5))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        c = constraints[0]
        assert c.operator == "=="
        assert c.value == 25.0

    def test_range_chinese(self):
        """范围...至... / ...到... → range operator"""
        text = "水灰比0.4至0.6"
        # Two hints: one for 0.4, one for 0.6
        hints = [
            NumericHint(parameter_hint="水灰比", value=0.4, unit="", span=(4, 7)),
            NumericHint(parameter_hint="水灰比", value=0.6, unit="", span=(10, 13)),
        ]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        c = constraints[0]
        assert c.operator == "range"
        assert c.value == (0.4, 0.6)

    def test_le_english(self):
        """no more than / at most → <= operator"""
        text = "Temperature shall be no more than 200°C"
        hints = [NumericHint(parameter_hint="Temperature", value=200.0, unit="°C", span=(29, 33))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        assert constraints[0].operator == "<="

    def test_ge_english(self):
        """not less than / at least → >= operator"""
        text = "Pressure must be at least 0.5MPa"
        hints = [NumericHint(parameter_hint="Pressure", value=0.5, unit="MPa", span=(21, 24))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        assert constraints[0].operator == ">="

    def test_eq_english(self):
        """equals / is → == operator"""
        text = "Diameter is 25mm"
        hints = [NumericHint(parameter_hint="Diameter", value=25.0, unit="mm", span=(11, 13))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        assert constraints[0].operator == "=="

    def test_kv_block_format(self):
        """KV block: '键: 值' format"""
        text = "最大温度: 80°C"
        hints = [NumericHint(parameter_hint="最大温度", value=80.0, unit="°C", span=(6, 10))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        # No explicit operator before the value — position inference should NOT infer
        # Since "最大温度: 80" has no operator keyword, it should return empty list
        assert len(constraints) == 0

    def test_no_operator_found_returns_empty(self):
        """Text with numeric value but no operator keyword → empty list"""
        text = "测量温度为80°C，但规范未明确限制"
        hints = [NumericHint(parameter_hint="温度", value=80.0, unit="°C", span=(5, 9))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        # "为" is ambiguous — could be "is" (=) but also part of "测量温度为"
        # Parser should be conservative and return []
        assert len(constraints) == 0

    def test_multiple_hints_same_text(self):
        """Text with multiple numeric values → one constraint per unique (param, operator)"""
        text = "温度范围10°C至25°C，压力不低于0.5MPa"
        hints = [
            NumericHint(parameter_hint="温度", value=10.0, unit="°C", span=(5, 9)),
            NumericHint(parameter_hint="温度", value=25.0, unit="°C", span=(12, 16)),
            NumericHint(parameter_hint="压力", value=0.5, unit="MPa", span=(21, 24)),
        ]
        constraints = ConstraintParser.parse_constraints(text, hints)
        # Should produce 2 constraints: range for temp, >= for pressure
        params = {c.parameter for c in constraints}
        assert "temperature" in params
        assert "pressure" in params

    def test_span_used_as_anchor(self):
        """Hint span positions used to locate operator text (±50 chars)"""
        text = "本规范规定温度不得超过100°C，超出此范围可能导致质量问题。"
        hints = [NumericHint(parameter_hint="温度", value=100.0, unit="°C", span=(13, 18))]
        constraints = ConstraintParser.parse_constraints(text, hints)
        assert len(constraints) == 1
        assert constraints[0].operator == "<="
