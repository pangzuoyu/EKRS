"""EvidenceBuilder — orchestrates chunks → NumericHints → Constraints."""
from __future__ import annotations

from collections import defaultdict
from typing import List

from ekrs_shared.models import Chunk, Constraint, NumericHint

from ekrs_rag.constraint_engine.normalizer import (
    normalize_constraint_hint,
    normalize_constraint_parameter,
)
from ekrs_rag.constraint_engine.parser import ConstraintParser
from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints


class EvidenceBuilder:
    """Builds constraint evidence chains from chunks.

    Flow: List[Chunk] → extract_hints() → ConstraintParser.parse_constraints()
          → normalize_parameter/unit() → deduplicate → List[Constraint]
    """

    @staticmethod
    def build(chunks: List[Chunk]) -> List[Constraint]:
        """Build deduplicated constraints from chunks.

        Args:
            chunks: List of Chunk objects from the chunker

        Returns:
            Deduplicated list of Constraint objects (highest priority wins on conflict)
        """
        all_constraints: List[Constraint] = []

        for chunk in chunks:
            # Step 1: Extract NumericHints from chunk.text
            hints = extract_hints(chunk)
            if not hints:
                continue

            # Step 2: Parse constraints from chunk using hints as anchors
            parsed = ConstraintParser.parse_constraints(chunk.text, hints)
            if not parsed:
                continue

            # Step 3: Normalize parameters and units for each constraint
            for constraint in parsed:
                normalized_param = normalize_constraint_parameter(constraint.parameter)
                normalized_value = constraint.value
                normalized_unit = constraint.unit

                # Normalize hint values (temperature affine, etc.)
                # Find the matching hint in original hints
                for hint in hints:
                    # Match by span proximity (hint span overlaps with constraint)
                    if _hint_matches_constraint(hint, constraint):
                        norm_val, norm_unit = normalize_constraint_hint(hint)
                        normalized_value = norm_val
                        normalized_unit = norm_unit
                        break

                constraint.parameter = normalized_param
                constraint.value = normalized_value
                constraint.unit = normalized_unit
                all_constraints.append(constraint)

        # Step 4: Deduplicate — same (parameter, operator, value, unit)
        # Scope path is NOT in the key so constraints from different scopes
        # compete on priority (highest wins), not on scope_path.
        # Keep highest-priority constraint for each unique key
        deduped: dict[tuple, Constraint] = {}
        for c in all_constraints:
            # Infer priority from scope_path prefix before deduplication
            c.priority = _priority_from_scope_path(c.scope_path)

            key = (
                c.parameter,
                c.operator,
                str(c.value),
                c.unit,
            )
            existing = deduped.get(key)
            if existing is None or c.priority.value > existing.priority.value:
                deduped[key] = c
            elif c.priority.value == existing.priority.value:
                # Same priority: keep higher confidence
                if c.confidence > existing.confidence:
                    deduped[key] = c

        return list(deduped.values())


def _hint_matches_constraint(hint: NumericHint, constraint: Constraint) -> bool:
    """Check if a hint matches a constraint (approximate span overlap)."""
    if hint.block_id and constraint.source.get("block_id"):
        return hint.block_id == constraint.source["block_id"]
    # Fallback: check if hint value is consistent with constraint value
    try:
        if isinstance(constraint.value, tuple):
            # Range constraint
            return any(
                abs(hint.value - v) < 0.001 for v in constraint.value
            )
        else:
            return abs(hint.value - constraint.value) < 0.001
    except (ValueError, TypeError):
        return False


# Priority hierarchy derived from scope_path prefix
_SCOPE_PRIORITY_MAP = {
    "national": 100,
    "industry": 80,
    "enterprise": 60,
    "project": 40,
    "reference": 20,
}


def _priority_from_scope_path(scope_path: list[str] | None) -> Priority:
    """Infer Priority from the first element of scope_path.

    NATIONAL > INDUSTRY > ENTERPRISE > PROJECT > REFERENCE.
    Falls back to PROJECT if scope_path is empty or unknown.
    """
    from ekrs_shared.models import Priority

    if not scope_path:
        return Priority.PROJECT
    first = scope_path[0].lower()
    return Priority(_SCOPE_PRIORITY_MAP.get(first, 40))
