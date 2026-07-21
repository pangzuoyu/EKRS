"""NumericHint extraction from chunk.text via regex."""
from __future__ import annotations

import re
from typing import List

from ekrs_shared.models import Chunk, NumericHint


# =============================================================================
# Extraction patterns
# =============================================================================

# Numeric value + unit patterns (order matters: more specific first)
# Each pattern must have at least one capturing group for the numeric value
# Generic numbers without units are skipped (not useful for constraints)
_NUMERIC_UNIT_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Temperature: °C, ℃, C, K, °F, F — handles both "80°C" and "80 °C"
    ("temperature", re.compile(r"(\d+\.?\d*)\s*(°?C(?!\|[a-zA-Z])|°?K(?!\|[a-zA-Z])|°?F(?!\|[a-zA-Z]))", re.IGNORECASE)),
    # Pressure
    ("pressure", re.compile(r"(\d+\.?\d*)\s*(MPa|Mpa|kPa|kpa|bar|psi|Pa)", re.IGNORECASE)),
    # Length/Dimension
    ("length", re.compile(r"(\d+\.?\d*)\s*(mm|cm|m|km|英寸|寸)", re.IGNORECASE)),
    # Percentage
    ("percentage", re.compile(r"(\d+\.?\d*)\s*(%)")),
]

# Chinese parameter name patterns (temperature)
_TEMP_CHINESE_PATTERNS = [
    (re.compile(r"温度\s*[为是]?\s*"), "temperature"),
    (re.compile(r"气温\s*[为是]?\s*"), "temperature"),
]

# Parameter context patterns — words that appear before the numeric value
_PARAM_CONTEXT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("temperature", re.compile(r"(?:温度|气温|水温|室温|环境温度|油温|水温)[\s:：]*\d", re.IGNORECASE)),
    ("pressure", re.compile(r"(?:压力|压强|气压|油压|水压)[\s:：]*\d", re.IGNORECASE)),
    ("diameter", re.compile(r"(?:直径|外径|内径|公称直径)[\s:：]*\d", re.IGNORECASE)),
    ("length", re.compile(r"(?:长度|宽度|高度|厚度|半径)[\s:：]*\d", re.IGNORECASE)),
    ("strength", re.compile(r"(?:强度|抗压强度|抗拉强度|屈服强度)[\s:：]*\d", re.IGNORECASE)),
    ("percentage", re.compile(r"(?:比例|水灰比|含水率|孔隙率|掺量)[\s:：]*\d", re.IGNORECASE)),
]


def extract_hints(chunk: Chunk) -> List[NumericHint]:
    """Extract NumericHints from a Chunk's text.

    Operates on chunk.text directly. Spans are relative to chunk.text.
    Copies chunk.scope_path and chunk.source_block_ids into each hint.

    Args:
        chunk: The Chunk to extract hints from

    Returns:
        List of NumericHint objects
    """
    text = chunk.text
    hints: List[NumericHint] = []

    # Track which positions have been covered to avoid overlapping hints
    covered: set[int] = set()

    # --- Extract value+unit patterns ---
    for category, pattern in _NUMERIC_UNIT_PATTERNS:
        for m in pattern.finditer(text):
            # Skip if this position is already covered
            if any(pos in covered for pos in range(m.start(), m.end())):
                continue

            value_str = m.group(1)
            unit = m.group(2) or "" if m.lastindex and m.lastindex >= 2 else ""

            try:
                value = float(value_str)
            except ValueError:
                continue

            # Determine parameter_hint from surrounding context
            parameter_hint = _extract_parameter_context(m.start(), text)

            hint = NumericHint(
                parameter_hint=parameter_hint or category,
                value=value,
                unit=unit,
                span=(m.start(), m.end()),
                source_text=m.group(0),
                block_id=",".join(chunk.source_block_ids) if chunk.source_block_ids else "",
                page_num=chunk.page_numbers[0] if chunk.page_numbers else None,
                scope_path=chunk.scope_path,
            )
            hints.append(hint)

            # Mark positions as covered
            for pos in range(m.start(), m.end()):
                covered.add(pos)

    # --- Extract Chinese temperature patterns like "温度80°C" (no space) ---
    for regex, param in _TEMP_CHINESE_PATTERNS:
        for m in regex.finditer(text):
            # Find the number immediately after the keyword
            num_match = re.search(r"\d+\.?\d*", m.group(0))
            if not num_match:
                continue
            # The number starts at m.start() + offset
            num_start = m.start() + m.group(0).index(num_match.group(0))
            num_end = num_start + len(num_match.group(0))
            if any(pos in covered for pos in range(num_start, num_end)):
                continue
            try:
                value = float(num_match.group(0))
            except ValueError:
                continue
            hint = NumericHint(
                parameter_hint=param,
                value=value,
                unit="°C",  # Chinese "温度" implies °C unless specified
                span=(num_start, num_end),
                source_text=num_match.group(0),
                block_id=",".join(chunk.source_block_ids) if chunk.source_block_ids else "",
                page_num=chunk.page_numbers[0] if chunk.page_numbers else None,
                scope_path=chunk.scope_path,
            )
            hints.append(hint)
            for pos in range(num_start, num_end):
                covered.add(pos)

    return hints


def _extract_parameter_context(pos: int, text: str) -> str:
    """Look backwards from pos to find a parameter context keyword.

    Searches up to 20 characters before pos.
    """
    start = max(0, pos - 20)
    preceding = text[start:pos]

    for param, pattern in _PARAM_CONTEXT_PATTERNS:
        if pattern.search(preceding):
            return param

    return ""
