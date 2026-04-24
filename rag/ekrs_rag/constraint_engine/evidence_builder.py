"""EvidenceBuilder — orchestrates chunks → NumericHints → Constraints (V2)."""
from __future__ import annotations

from collections import defaultdict
from typing import List, Optional

from ekrs_shared.models import Chunk, ConstraintV2, NumericHint

from ekrs_rag.constraint_engine.normalizer import (
    normalize_constraint_hint,
    normalize_constraint_parameter,
)
from ekrs_rag.constraint_engine.parser import parse_interval
from ekrs_rag.ingestion.numeric_hint_extractor import extract_hints


# Priority hierarchy derived from scope_path prefix
_SCOPE_PRIORITY_MAP = {
    "national": 100,
    "industry": 80,
    "enterprise": 60,
    "project": 40,
    "reference": 20,
}


# Lifecycle status keywords
_LIFECYCLE_DRAFT_KEYWORDS = {"draft", "征求意见稿", "draft document"}
_LIFECYCLE_REVIEW_KEYWORDS = {
    "review",
    "建议",
    "审阅",
    "review document",
    "征求意见",
}
_LIFECYCLE_TRANSITIONAL_KEYWORDS = {"过渡期", "transition period", "transitional"}


def infer_lifecycle(
    scope_path: list[str] | None,
    text: str,
    doc_meta: dict | None = None,
) -> dict:
    """Infer lifecycle status from scope_path, text, and doc metadata.

    L5 rules:
    - draft / 征求意见稿 → status: "draft", is_binding: false
    - review / doc_type == "review" / 建议 / 审阅 → status: "review", is_binding: false
    - 过渡期 / transition period → status: "transitional", is_binding: true
    - doc_meta.superseded_by present → status: "deprecated"
    - Default → status: "active", is_binding: true
    """
    status = "active"
    is_binding = True

    # Check doc_meta.superseded_by first (highest priority)
    if doc_meta and doc_meta.get("superseded_by"):
        status = "deprecated"
        is_binding = False
        return {"status": status, "is_binding": is_binding}

    # Check scope_path for lifecycle keywords
    if scope_path:
        scope_text = " ".join(scope_path).lower()
        if any(kw in scope_text for kw in _LIFECYCLE_DRAFT_KEYWORDS):
            status = "draft"
            is_binding = False
        elif any(kw in scope_text for kw in _LIFECYCLE_REVIEW_KEYWORDS):
            status = "review"
            is_binding = False
        elif any(kw in scope_text for kw in _LIFECYCLE_TRANSITIONAL_KEYWORDS):
            status = "transitional"
            is_binding = True

    # Check text content if no match yet
    if status == "active":
        text_lower = text.lower()
        if any(kw in text_lower for kw in _LIFECYCLE_DRAFT_KEYWORDS):
            status = "draft"
            is_binding = False
        elif any(kw in text_lower for kw in _LIFECYCLE_REVIEW_KEYWORDS):
            status = "review"
            is_binding = False
        elif any(kw in text_lower for kw in _LIFECYCLE_TRANSITIONAL_KEYWORDS):
            status = "transitional"
            is_binding = True

    return {
        "status": status,
        "is_binding": is_binding,
        "effective_date": None,
        "expiry_date": None,
    }


def _priority_from_scope_path(scope_path: list[str] | None) -> dict:
    """Infer priority from the first element of scope_path.

    Returns dict with {explicit_level, recency_score, authority_score}.
    NATIONAL > INDUSTRY > ENTERPRISE > PROJECT > REFERENCE.
    Falls back to PROJECT if scope_path is empty or unknown.
    """
    if not scope_path:
        return {"explicit_level": 40, "recency_score": 0.0, "authority_score": 0.0}

    first = scope_path[0].lower()
    explicit_level = _SCOPE_PRIORITY_MAP.get(first, 40)

    # recency_score and authority_score could be computed from doc metadata
    # For now, set to 0.0 (can be enhanced later)
    return {
        "explicit_level": explicit_level,
        "recency_score": 0.0,
        "authority_score": 0.0,
    }


def _extract_provision_id(scope_path: list[str] | None) -> str | None:
    """Extract provision_id from heading_path clause number pattern.

    Looks for patterns like "5.2.3" in the heading_path.
    """
    if not scope_path:
        return None

    import re

    for segment in scope_path:
        # Match clause number patterns like "5.2.3", "第5.2条", etc.
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", segment)
        if match:
            return match.group(1)
    return None


def _infer_doc_type(scope_path: list[str] | None) -> str:
    """Infer doc_type from the first element of scope_path.

    Valid values: national, industry, enterprise, project, reference.
    """
    if not scope_path:
        return "project"

    first = scope_path[0].lower()
    if first in ("national", "industry", "enterprise", "project", "reference"):
        return first
    return "project"


def _extract_conditions(text: str) -> list[dict]:
    """Extract applicability conditions from constraint text.

    Looks for patterns like "在...环境下" (in ... environment).

    Returns:
        List of Condition dicts matching Pydantic model: [{"parameter": "environment", "operator": "=", "value": "高温"}]
    """
    import re

    conditions = []

    # 在...环境下 pattern
    m = re.search(r"在([^。，,，\\s]+)环境下", text)
    if m:
        conditions.append({
            "parameter": "environment",
            "operator": "=",
            "value": m.group(1),
        })

    # 在...条件下 pattern
    m = re.search(r"在([^。，,，\\s]+)条件下", text)
    if m:
        conditions.append({
            "parameter": "condition",
            "operator": "=",
            "value": m.group(1),
        })

    return conditions


class EvidenceBuilder:
    """Builds constraint evidence chains from chunks (V2).

    Flow: List[Chunk] → extract_hints() → parse_interval() (V2)
          → normalize_parameter/unit() → infer_lifecycle() → deduplicate → List[ConstraintV2]
    """

    @staticmethod
    def build(
        chunks: List[Chunk],
        inferred: bool = False,
        doc_meta: dict | None = None,
    ) -> List[ConstraintV2]:
        """Build deduplicated constraints from chunks (V2).

        Args:
            chunks: List of Chunk objects from the chunker
            inferred: True if constraints are inferred from context (for strict mode)
            doc_meta: Optional document metadata for lifecycle inference

        Returns:
            Deduplicated list of ConstraintV2 objects (highest priority wins on conflict)
        """
        all_constraints: List[ConstraintV2] = []

        for chunk in chunks:
            # Step 1: Extract NumericHints from chunk.text
            hints = extract_hints(chunk)
            if not hints:
                continue

            # Step 2: Parse intervals from chunk using hints as anchors (V2)
            intervals = parse_interval(chunk.text, hints)
            if not intervals:
                continue

            # Extract conditions from text (scope/applicability)
            conditions = _extract_conditions(chunk.text)

            # Step 3: Build V2 constraints from intervals
            # intervals and hints are 1:1 from parse_interval (parallel iteration)
            for interval, matching_hint in zip(intervals, hints):
                # Normalize parameter and unit
                normalized_param = normalize_constraint_parameter(
                    matching_hint.parameter_hint
                )
                norm_val, norm_unit = normalize_constraint_hint(matching_hint)

                # Get operator from interval bounds to determine value_type
                value_type = "interval"
                scalar_value = None
                if interval.get("lower") == interval.get("upper"):
                    # Single value — could be scalar
                    if interval.get("lower_inclusive") and interval.get("upper_inclusive"):
                        value_type = "scalar"
                        scalar_value = interval.get("lower")

                # Infer lifecycle and priority from scope_path
                lifecycle = infer_lifecycle(
                    matching_hint.scope_path, chunk.text, doc_meta
                )
                priority = _priority_from_scope_path(matching_hint.scope_path)

                # Build V2 source
                provision_id = _extract_provision_id(matching_hint.scope_path)
                doc_type = _infer_doc_type(matching_hint.scope_path)
                source = {
                    "doc_id": chunk.doc_hash,  # Use doc_hash as doc_id
                    "provision_id": provision_id,
                    "doc_type": doc_type,
                    "authority_score": priority.get("authority_score", 0.0),
                }

                constraint = ConstraintV2(
                    parameter=normalized_param,
                    value_type=value_type,
                    interval=interval if value_type == "interval" else None,
                    scalar_value=scalar_value,
                    unit=norm_unit,
                    category="general",
                    priority=priority,
                    confidence=1.0,
                    inferred=inferred,
                    lifecycle=lifecycle,
                    source=source,
                    conditions=conditions,
                    scope_path=matching_hint.scope_path,
                )
                all_constraints.append(constraint)

        # Step 4: Deduplicate — same (parameter, lower, upper, unit)
        # Scope path is NOT in the key so constraints from different scopes
        # compete on priority (highest wins), not on scope_path.
        # Keep highest-priority constraint for each unique key
        deduped: dict[tuple, ConstraintV2] = {}
        for c in all_constraints:
            key = (
                c.parameter,
                c.interval.get("lower") if c.interval else None,
                c.interval.get("upper") if c.interval else None,
                c.unit,
            )
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = c
            elif c.priority.get("explicit_level", 0) > existing.priority.get(
                "explicit_level", 0
            ):
                deduped[key] = c
            elif c.priority.get("explicit_level", 0) == existing.priority.get(
                "explicit_level", 0
            ):
                # Same priority: keep higher confidence
                if c.confidence > existing.confidence:
                    deduped[key] = c

        return list(deduped.values())

