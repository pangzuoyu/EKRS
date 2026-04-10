"""Constraint normalizer — delegates to ekrs_shared.normalizer, adds constraint-specific logic."""
from __future__ import annotations

from typing import Tuple

from ekrs_shared.models import NumericHint
from ekrs_shared.normalizer import (
    UnitRegistry,
    is_temperature_unit,
    normalize_parameter,
    normalize_temperature,
)


def normalize_constraint_hint(hint: NumericHint) -> Tuple[float, str]:
    """Normalize a NumericHint's value to base units.

    Handles temperature with affine conversion (°F→°C, K→°C).
    Raises ValueError on unsupported temperature units.

    Returns: (normalized_value, normalized_unit)
    """
    if is_temperature_unit(hint.unit):
        return normalize_temperature(hint.value, hint.unit)
    else:
        converted, base_unit, _ = UnitRegistry.normalize(hint.value, hint.unit)
        return converted, base_unit


def normalize_constraint_parameter(param: str) -> str:
    """Normalize a constraint parameter name via synonym mapping."""
    return normalize_parameter(param)
