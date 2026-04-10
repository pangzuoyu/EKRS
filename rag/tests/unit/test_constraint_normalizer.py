"""Tests for constraint_engine/normalizer.py."""
from __future__ import annotations

import pytest
from ekrs_shared.models import NumericHint

from ekrs_rag.constraint_engine.normalizer import (
    normalize_constraint_hint,
    normalize_constraint_parameter,
)


class TestNormalizeConstraintHint:
    def test_fahrenheit_to_celsius(self):
        """°F → °C via affine: (F-32)*5/9"""
        hint = NumericHint(parameter_hint="温度", value=176.0, unit="°F", span=(0, 5))
        val, unit = normalize_constraint_hint(hint)
        assert abs(val - 80.0) < 0.01
        assert unit == "°C"

    def test_kelvin_to_celsius(self):
        """K → °C via affine: K-273.15"""
        hint = NumericHint(parameter_hint="温度", value=293.15, unit="K", span=(0, 5))
        val, unit = normalize_constraint_hint(hint)
        assert abs(val - 20.0) < 0.01
        assert unit == "°C"

    def test_celsius_passthrough(self):
        """°C stays as °C"""
        hint = NumericHint(parameter_hint="温度", value=25.0, unit="°C", span=(0, 5))
        val, unit = normalize_constraint_hint(hint)
        assert val == 25.0
        assert unit == "°C"

    def test_pressure_scalar_conversion(self):
        """MPa → Pa via scalar (×10⁶)"""
        hint = NumericHint(parameter_hint="压力", value=1.0, unit="MPa", span=(0, 5))
        val, unit = normalize_constraint_hint(hint)
        assert val == 1_000_000.0
        assert unit == "pa"

    def test_length_mm_to_m(self):
        """mm → m via scalar (×0.001)"""
        hint = NumericHint(parameter_hint="直径", value=25.0, unit="mm", span=(0, 5))
        val, unit = normalize_constraint_hint(hint)
        assert val == 0.025
        assert unit == "m"

    def test_unknown_unit_passthrough(self):
        """Unknown non-temperature unit passes through UnitRegistry unchanged"""
        hint = NumericHint(parameter_hint="温度", value=100.0, unit="°R", span=(0, 5))
        val, unit = normalize_constraint_hint(hint)
        # °R not recognized as temperature, goes through UnitRegistry as unknown
        # UnitRegistry returns (value, unit, "unknown") for unknown units
        assert val == 100.0
        assert unit == "°R"


class TestNormalizeConstraintParameter:
    def test_chinese_temperature(self):
        """温度 → temperature"""
        assert normalize_constraint_parameter("温度") == "temperature"

    def test_chinese_pressure(self):
        """压力 → pressure"""
        assert normalize_constraint_parameter("压力") == "pressure"

    def test_english_passthrough(self):
        """English params lowercased with underscores"""
        assert normalize_constraint_parameter("Diameter") == "diameter"

    def test_whitespace_trimmed(self):
        """Leading/trailing whitespace trimmed"""
        assert normalize_constraint_parameter("  温度  ") == "temperature"
