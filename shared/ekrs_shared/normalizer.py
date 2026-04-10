"""Unit conversion and parameter normalization.

Affine temperature conversion (F→C, K→C) is critical for correctness.
All temperatures unified to °C. Other units use scalar conversion.
"""

from __future__ import annotations

from typing import Tuple

# --- Temperature conversion (affine, not scalar) ---


def normalize_temperature(value: float, unit: str) -> Tuple[float, str]:
    """Convert any temperature unit to °C. Affine transform.

    F→C: (F - 32) * 5/9  (NOT F * factor)
    K→C: K - 273.15       (NOT K * factor)
    """
    u = unit.upper().replace("°", "")
    if u in ("C", "°C"):
        return value, "°C"
    elif u == "F":
        return (value - 32) * 5 / 9, "°C"
    elif u == "K":
        return value - 273.15, "°C"
    else:
        raise ValueError(f"Unsupported temperature unit: {unit}")


def is_temperature_unit(unit: str) -> bool:
    return unit.upper().replace("°", "") in ("C", "F", "K")


# --- Scalar unit conversions ---


class UnitRegistry:
    """Unit conversion registry for engineering parameters.

    All conversions produce a base unit and a scalar factor.
    Temperature is special (affine) and handled separately.
    """

    CONVERSIONS = {
        "length": {
            "m": 1.0,
            "mm": 0.001,
            "cm": 0.01,
            "km": 1000.0,
            "in": 0.0254,
            "ft": 0.3048,
            "米": 1.0,
            "毫米": 0.001,
        },
        "area": {
            "m2": 1.0,
            "mm2": 1e-6,
            "cm2": 1e-4,
            "km2": 1e6,
            "hectare": 10000.0,
            "acre": 4046.86,
            "平方米": 1.0,
            "公顷": 10000.0,
            "亩": 666.67,
        },
        "time_duration": {
            "d": 1.0,
            "h": 1 / 24,
            "min": 1 / 1440,
            "s": 1 / 86400,
            "week": 7.0,
            "month": 30.0,
            "year": 365.0,
            "天": 1.0,
            "日": 1.0,
            "小时": 1 / 24,
            "个月": 30.0,
            "年": 365.0,
        },
        "pressure": {
            "pa": 1.0,
            "mpa": 1e6,
            "kpa": 1e3,
            "bar": 1e5,
            "psi": 6894.76,
        },
        "percentage": {
            "%": 1.0,
        },
    }

    @classmethod
    def normalize(cls, value: float, unit: str) -> Tuple[float, str, str]:
        """Normalize a value to its base unit.

        Returns: (converted_value, base_unit, category)
        For temperature: uses affine conversion, always returns °C.
        For others: scalar conversion to the first entry in the category.
        """
        if is_temperature_unit(unit):
            converted, base = normalize_temperature(value, unit)
            return converted, base, "temperature"

        unit_lower = unit.lower().replace("°", "").replace(" ", "")
        for category, units in cls.CONVERSIONS.items():
            if unit_lower in units:
                factor = units[unit_lower]
                base_unit = list(units.keys())[0]
                return value * factor, base_unit, category

        # Unknown unit: pass through without conversion
        return value, unit, "unknown"

    @staticmethod
    def parse_time_deadline(text: str) -> dict | None:
        """Parse Chinese time deadline patterns.

        Example: '开工后30天内' → {'reference_event': '开工', 'offset_days': 30, 'is_working_day': False}
        """
        import re

        pattern = r"(.*?)(?:后|起)\s*(\d+)\s*(天|日|个月|周|工作日)"
        match = re.search(pattern, text)
        if not match:
            return None

        ref_event = match.group(1).strip()
        offset = int(match.group(2))
        unit = match.group(3)

        # Convert to days
        if unit in ("天", "日"):
            offset_days = offset
        elif unit == "周":
            offset_days = offset * 7
        elif unit == "个月":
            offset_days = offset * 30
        else:
            offset_days = offset

        return {
            "reference_event": ref_event,
            "offset_days": offset_days,
            "is_working_day": unit == "工作日",
        }


# --- Parameter synonym mapping ---

PARAMETER_SYNONYMS: dict[str, str] = {
    "温度": "temperature",
    "气温": "temperature",
    "temp": "temperature",
    "压力": "pressure",
    "压强": "pressure",
    "湿度": "humidity",
    "强度": "strength",
    "抗压强度": "compressive_strength",
    "抗拉强度": "tensile_strength",
    "厚度": "thickness",
    "直径": "diameter",
    "长度": "length",
    "宽度": "width",
    "高度": "height",
    "龄期": "age",
    "标号": "grade",
    "等级": "grade",
}


def normalize_parameter(raw: str) -> str:
    """Normalize a parameter name using synonym mapping."""
    stripped = raw.strip()
    return PARAMETER_SYNONYMS.get(stripped, stripped.lower().replace(" ", "_"))
