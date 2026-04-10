"""Unit tests for normalizer (affine temperature, unit conversion, synonyms)."""

import pytest

from ekrs_shared.normalizer import (
    UnitRegistry,
    is_temperature_unit,
    normalize_parameter,
    normalize_temperature,
)


class TestNormalizeTemperature:
    def test_celsius_identity(self):
        val, unit = normalize_temperature(25.0, "C")
        assert val == 25.0
        assert unit == "°C"

    def test_celsius_with_symbol(self):
        val, unit = normalize_temperature(25.0, "°C")
        assert val == 25.0

    def test_fahrenheit_to_celsius(self):
        val, unit = normalize_temperature(212.0, "F")
        assert abs(val - 100.0) < 0.01
        assert unit == "°C"

    def test_fahrenheit_freezing(self):
        val, unit = normalize_temperature(32.0, "F")
        assert abs(val - 0.0) < 0.01

    def test_kelvin_to_celsius(self):
        val, unit = normalize_temperature(373.15, "K")
        assert abs(val - 100.0) < 0.01
        assert unit == "°C"

    def test_kelvin_absolute_zero(self):
        val, unit = normalize_temperature(0.0, "K")
        assert abs(val - (-273.15)) < 0.01

    def test_unsupported_unit(self):
        with pytest.raises(ValueError, match="Unsupported temperature unit"):
            normalize_temperature(100, "R")


class TestIsTemperatureUnit:
    @pytest.mark.parametrize("unit", ["C", "°C", "c", "F", "f", "K", "k"])
    def test_temperature_units(self, unit):
        assert is_temperature_unit(unit) is True

    def test_non_temperature(self):
        assert is_temperature_unit("MPa") is False


class TestUnitRegistry:
    def test_pressure_mpa(self):
        val, base, cat = UnitRegistry.normalize(1.0, "MPa")
        assert val == 1e6
        assert base == "pa"
        assert cat == "pressure"

    def test_pressure_kpa(self):
        val, base, cat = UnitRegistry.normalize(500.0, "kPa")
        assert val == 5e5
        assert cat == "pressure"

    def test_length_mm(self):
        val, base, cat = UnitRegistry.normalize(1500.0, "mm")
        assert val == 1.5
        assert base == "m"
        assert cat == "length"

    def test_length_chinese(self):
        val, base, cat = UnitRegistry.normalize(3.0, "米")
        assert val == 3.0
        assert cat == "length"

    def test_time_days(self):
        val, base, cat = UnitRegistry.normalize(7.0, "d")
        assert val == 7.0
        assert cat == "time_duration"

    def test_time_chinese(self):
        val, base, cat = UnitRegistry.normalize(30.0, "天")
        assert val == 30.0

    def test_percentage(self):
        val, base, cat = UnitRegistry.normalize(0.8, "%")
        assert val == 0.8
        assert cat == "percentage"

    def test_temperature_via_registry(self):
        val, base, cat = UnitRegistry.normalize(100.0, "F")
        assert abs(val - 37.78) < 0.1
        assert cat == "temperature"

    def test_unknown_unit_passthrough(self):
        val, base, cat = UnitRegistry.normalize(42.0, "xyz")
        assert val == 42.0
        assert base == "xyz"
        assert cat == "unknown"


class TestParseTimeDeadline:
    def test_days_after_event(self):
        result = UnitRegistry.parse_time_deadline("开工后30天内完成")
        assert result is not None
        assert result["reference_event"] == "开工"
        assert result["offset_days"] == 30
        assert result["is_working_day"] is False

    def test_months(self):
        result = UnitRegistry.parse_time_deadline("竣工后3个月内")
        assert result is not None
        assert result["offset_days"] == 90

    def test_no_match(self):
        result = UnitRegistry.parse_time_deadline("这是普通文本没有时间期限")
        assert result is None


class TestNormalizeParameter:
    def test_chinese_synonym(self):
        assert normalize_parameter("温度") == "temperature"

    def test_english_synonym(self):
        assert normalize_parameter("temp") == "temperature"

    def test_unknown_passthrough(self):
        assert normalize_parameter("自定义参数") == "自定义参数"

    def test_whitespace_trimmed(self):
        assert normalize_parameter("  温度  ") == "temperature"
