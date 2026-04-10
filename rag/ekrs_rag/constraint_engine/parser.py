"""ConstraintParser — regex-based operator extraction from raw text using numeric hints as anchors."""
from __future__ import annotations

import re
from typing import List, Optional

from ekrs_shared.models import Constraint, NumericHint, Priority
from ekrs_rag.constraint_engine.normalizer import normalize_constraint_parameter


# =============================================================================
# Operator patterns (Chinese + English)
# =============================================================================

# Order matters: longer/more specific patterns first
_OPERATOR_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # range operators
    ("range", "至", re.compile(r"至", re.IGNORECASE)),
    ("range", "到", re.compile(r"到", re.IGNORECASE)),
    ("range", "..to..", re.compile(r"\bto\b", re.IGNORECASE)),
    # <= operators
    ("<=", "不得超过", re.compile(r"不得超过")),
    ("<=", "不超过", re.compile(r"不超过")),
    ("<=", "no more than", re.compile(r"no more than", re.IGNORECASE)),
    ("<=", "at most", re.compile(r"at most", re.IGNORECASE)),
    (">=", "不低于", re.compile(r"不低于")),
    (">=", "不少于", re.compile(r"不少于")),
    (">=", "not less than", re.compile(r"not less than", re.IGNORECASE)),
    (">=", "at least", re.compile(r"at least", re.IGNORECASE)),
    ("==", "等于", re.compile(r"等于")),
    ("==", "为", re.compile(r"为")),  # "直径为25mm"
    ("==", "is", re.compile(r"\bis\b", re.IGNORECASE)),
    ("==", "equals", re.compile(r"equals", re.IGNORECASE)),
]

# Context window radius for searching near hint
_CONTEXT_RADIUS = 50


class ConstraintParser:
    """Regex-based constraint operator parser using numeric hints as anchors."""

    @staticmethod
    def parse_constraints(text: str, hints: List[NumericHint]) -> List[Constraint]:
        """Parse constraints from text using numeric hints as anchors.

        Args:
            text: The raw text to parse
            hints: NumericHint anchors found in the text

        Returns:
            List of Constraint objects (one per operator found)
        """
        if not hints:
            return []

        constraints: List[Constraint] = []
        seen: set[tuple] = set()  # deduplication key

        for hint in hints:
            # Find the operator within ±CONTEXT_RADIUS of the hint span
            start = max(0, hint.span[0] - _CONTEXT_RADIUS)
            end = min(len(text), hint.span[1] + _CONTEXT_RADIUS)
            context = text[start:end]

            operator, value = _find_operator_in_context(
                context, hint.value, hint.unit, hint.span[0]
            )

            if operator is None:
                continue

            # Build constraint
            param = normalize_constraint_parameter(hint.parameter_hint)
            constraint = Constraint(
                parameter=param,
                operator=operator,
                value=value,
                unit=hint.unit,
                priority=Priority.PROJECT,
                confidence=1.0,
                source={
                    "block_id": hint.block_id,
                    "doc_id": "",
                    "page_num": hint.page_num,
                    "source_text": hint.source_text,
                },
                scope_path=hint.scope_path,
            )

            # Deduplicate by (parameter, operator, value, unit, scope_path)
            key = (
                constraint.parameter,
                constraint.operator,
                str(constraint.value),
                constraint.unit,
                tuple(constraint.scope_path) if constraint.scope_path else None,
            )
            if key not in seen:
                seen.add(key)
                constraints.append(constraint)

        return constraints


def _find_operator_in_context(
    context: str, value: float, unit: str, hint_start: int
) -> tuple[str, float | tuple[float, float]] | tuple[None, None]:
    """Find the operator near the value in context.

    For "为" (==): only matches if:
      (a) it appears BEFORE the value position, AND
      (b) the character immediately before "为" is NOT a measurement verb
          (测量, 检测, 记录, 观测, 显示, 表明, 发现)
    For range operators: matches if the range pattern spans two numbers.

    Returns: (operator, value_or_range) or (None, None)
    """
    # Measurement verbs that precede "为" and make it non-constraint
    _MEASUREMENT_VERBS = frozenset(["测量", "检测", "记录", "观测", "显示", "表明", "发现"])

    # Find where the value appears in the context (for position checks)
    val_pattern = re.compile(r"[+-]?\d+\.?\d*")
    val_pos = None
    for m in val_pattern.finditer(context):
        if abs(float(m.group()) - value) < 0.001:
            val_pos = m.start()
            break

    # Try each operator pattern in order
    for op, label, pattern in _OPERATOR_PATTERNS:
        match = pattern.search(context)
        if not match:
            continue

        if op == "range":
            range_match = _find_range_in_context(context, value)
            if range_match is not None:
                return ("range", range_match)
        elif op == "==":
            # For "为" (=): only accept if it appears BEFORE the value position.
            # Additionally, skip if the text before "为" starts with a measurement verb
            # followed immediately by a short parameter name (<=3 chars).
            # e.g., "测量温度为80°C" — "测量" is measurement verb, "温度" is parameter.
            if val_pos is not None and match.start() >= val_pos:
                continue  # "为" is after the value — not a constraint
            before = context[:match.start()]  # substring before "为"
            # Check for measurement verb pattern: "测量+short_param" before "为"
            is_measurement = False
            for verb in _MEASUREMENT_VERBS:
                if before.startswith(verb):
                    remaining = before[len(verb):]
                    if len(remaining) <= 3:
                        # "测量" + short param ("温度", "直径", "压力") → measurement
                        is_measurement = True
                        break
            if is_measurement:
                continue  # skip to next operator pattern
            return ("==", value)
        else:
            # For <= and >=, accept regardless of position
            return (op, value)

    return (None, None)


def _find_range_in_context(context: str, value: float) -> tuple[float, float] | None:
    """Find a range in the context given one endpoint.

    Looks for "value ...至... X" or "X ...至... value" or "value ...到... X"
    Returns (lo, hi) or None.
    """
    # Pattern: any two numbers with 至 or 到 between them
    range_pattern = re.compile(
        r"([+-]?\d+\.?\d*)\s*至\s*([+-]?\d+\.?\d*)"
        r"|"
        r"([+-]?\d+\.?\d*)\s*到\s*([+-]?\d+\.?\d*)"
        r"|"
        r"([+-]?\d+\.?\d*)\s*to\s([+-]?\d+\.?\d*)",
        re.IGNORECASE,
    )

    for m in range_pattern.finditer(context):
        # Extract both numbers from the match groups
        groups = m.groups()
        if groups[0] is not None and groups[1] is not None:
            lo, hi = float(groups[0]), float(groups[1])
            return (lo, hi)
        elif groups[2] is not None and groups[3] is not None:
            lo, hi = float(groups[2]), float(groups[3])
            return (lo, hi)
        elif groups[4] is not None and groups[5] is not None:
            lo, hi = float(groups[4]), float(groups[5])
            return (lo, hi)

    return None
