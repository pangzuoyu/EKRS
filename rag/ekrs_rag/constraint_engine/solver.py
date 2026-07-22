"""IntervalSolver — pure-function constraint solver using portion.Interval arithmetic."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import portion  # type: ignore[import]

from ekrs_shared.models import ConstraintV2, Evidence, Priority
from ekrs_shared.normalizer import normalize_temperature, normalize_parameter
from ekrs_shared.models import Constraint as ConstraintV1


class StrictViolationError(Exception):
    """Raised when strict mode forbids a soft fallback (R6 enforcement, D3)."""


# =============================================================================
# Types
# =============================================================================


@dataclass
class _TraceEntry:
    parameter: str
    action: str  # "applied" | "skipped_scope" | "skipped_priority" | "conflict"
    operator: str
    value: Any
    interval_snapshot: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class _ParameterResult:
    interval: portion.Interval
    unit: str
    confidence: float
    evidence: list[Evidence]
    trace: list[_TraceEntry]
    had_conflict: bool = False  # True if at least one constraint was applied but resulted in empty


# =============================================================================
# IntervalSolver
# =============================================================================



def _ensure_v2(c: ConstraintV2 | ConstraintV1) -> ConstraintV2:
    """Convert V1 Constraint to V2 if needed (backward compat for existing tests)."""
    if isinstance(c, ConstraintV2):
        return c
    # V1 -> V2 conversion
    op = c.operator
    interval: dict[str, float | tuple[float, float] | None]
    if op == "==":
        interval = {"lower": c.value, "upper": c.value, "lower_inclusive": True, "upper_inclusive": True}
    elif op == "<=":
        interval = {"lower": None, "upper": c.value, "lower_inclusive": True, "upper_inclusive": True}
    elif op == ">=":
        interval = {"lower": c.value, "upper": None, "lower_inclusive": True, "upper_inclusive": True}
    elif op == "<":
        interval = {"lower": None, "upper": c.value, "lower_inclusive": True, "upper_inclusive": False}
    elif op == ">":
        interval = {"lower": c.value, "upper": None, "lower_inclusive": False, "upper_inclusive": True}
    elif op == "range":
        # V1 range: value is a tuple (lower, upper)
        if isinstance(c.value, tuple) and len(c.value) == 2:
            interval = {
                "lower": c.value[0],
                "upper": c.value[1],
                "lower_inclusive": True,
                "upper_inclusive": True,
            }
        else:
            interval = {"lower": None, "upper": None, "lower_inclusive": True, "upper_inclusive": True}
    else:
        interval = {"lower": None, "upper": None, "lower_inclusive": True, "upper_inclusive": True}

    # Map V1 Priority IntEnum to V2 explicit_level
    from ekrs_shared.models import Priority as V1Priority
    if isinstance(c.priority, V1Priority):
        explicit_level = int(c.priority)  # NATIONAL=100, INDUSTRY=80, etc.
    else:
        explicit_level = 50

    # Apply temperature affine conversion if needed (V1 doesn't normalize)
    unit = c.unit
    if _is_temperature_unit(unit):
        scalar = c.value[0] if isinstance(c.value, tuple) else c.value
        if scalar is not None:
            scalar, unit = normalize_temperature(scalar, unit)
            # Update interval bounds with converted value
            if op == "<=":
                interval = {"lower": None, "upper": scalar, "lower_inclusive": True, "upper_inclusive": True}
            elif op == ">=":
                interval = {"lower": scalar, "upper": None, "lower_inclusive": True, "upper_inclusive": True}
            elif op == "<":
                interval = {"lower": None, "upper": scalar, "lower_inclusive": True, "upper_inclusive": False}
            elif op == ">":
                interval = {"lower": scalar, "upper": None, "lower_inclusive": False, "upper_inclusive": True}
            elif op == "==":
                interval = {"lower": scalar, "upper": scalar, "lower_inclusive": True, "upper_inclusive": True}
            elif op == "range" and isinstance(c.value, tuple) and len(c.value) == 2:
                # Use ORIGINAL unit for range bounds (unit was updated above for scalar case)
                orig_unit = c.unit
                lo_norm, _ = normalize_temperature(c.value[0], orig_unit)
                hi_norm, _ = normalize_temperature(c.value[1], orig_unit)
                interval = {
                    "lower": lo_norm,
                    "upper": hi_norm,
                    "lower_inclusive": True,
                    "upper_inclusive": True,
                }

    return ConstraintV2(
        parameter=c.parameter,
        value_type="interval",
        interval=interval,
        unit=unit,
        inferred=False,
        priority={"explicit_level": explicit_level, "recency_score": 0.0, "authority_score": 0.0},
        scope_path=c.scope_path or None,
        confidence=c.confidence,
        source=c.source,
        lifecycle={"status": "active", "is_binding": True},
    )


class IntervalSolver:
    """Pure-function interval arithmetic constraint solver.

    R2: No I/O, no state, no side effects.
    """

    @staticmethod
    def solve(
        constraints: list[ConstraintV2],
        active_scope: Optional[list[str]] = None,
    ) -> dict:
        """Solve constraints and return structured result with multi-branch support (V2).

        Args:
            constraints: List of ConstraintV2 objects
            active_scope: Optional scope filter (scope_path must match to be applied)

        Returns:
            {
                "status": "OK" | "CONFLICT" | "EMPTY",
                "branches": {
                    "general": {
                        "temperature": {"range": [lower, upper], "unit": "C", ...},
                    },
                    "高温环境": {
                        "temperature": {"range": [60, 100], "unit": "C", ...},
                    },
                },
                "primary_branch": "general",
                "conflicts": [...],
                "trace": [...],
            }
        """
        if not constraints:
            return {
                "status": "EMPTY",
                "branches": {},
                "primary_branch": None,
                "conflicts": [],
                "trace": [],
            }

        # Convert V1 Constraint objects to V2 ConstraintV2 (backward compat)
        constraints = [_ensure_v2(c) for c in constraints]

        # Step 1: Group constraints by branch key (conditions)
        branch_groups: dict[str, list[ConstraintV2]] = defaultdict(list)
        for c in constraints:
            branch_key = _get_branch_key(c)
            branch_groups[branch_key].append(c)

        # Step 2: Solve each branch independently
        branches: dict[str, dict] = {}
        all_trace: list[dict] = []
        has_conflict = False
        conflicts: list[dict] = []

        for branch_key, branch_constraints in branch_groups.items():
            # Group by parameter within this branch
            grouped: dict[str, list[ConstraintV2]] = defaultdict(list)
            for c in branch_constraints:
                param = normalize_parameter(c.parameter)
                grouped[param].append(c)

            branch_params: dict[str, dict] = {}

            for param, param_constraints in grouped.items():
                result = _solve_parameter(param, param_constraints, active_scope)
                all_trace.extend(_trace_entry_to_dict(t) for t in result.trace)

                if result.had_conflict:
                    has_conflict = True
                    conflicts.append({
                        "parameter": param,
                        "branch": branch_key,
                        "interval": str(result.interval),
                        "constraints": [
                            {"interval": c.interval, "value_type": c.value_type, "unit": c.unit}
                            for c in param_constraints
                        ],
                    })
                elif result.interval.empty or result.interval == portion.Interval():
                    continue
                else:
                    if result.interval.left == portion.CLOSED:
                        lo_val: Optional[float] = result.interval.lower
                    else:
                        lo_val = None
                    if result.interval.right == portion.CLOSED:
                        hi_val: Optional[float] = result.interval.upper
                    else:
                        hi_val = None

                    branch_params[param] = {
                        "range": [lo_val, hi_val],
                        "unit": result.unit,
                        "confidence": result.confidence,
                        "evidence": [e.model_dump() for e in result.evidence],
                    }

            branches[branch_key] = branch_params

        # Determine status
        non_empty_branches = {k: v for k, v in branches.items() if v}
        if not non_empty_branches and not has_conflict:
            status = "EMPTY"
        elif has_conflict:
            status = "CONFLICT"
        else:
            status = "OK"

        # Primary branch is "general" if it exists, otherwise first non-empty
        primary = "general" if "general" in non_empty_branches else (
            next(iter(non_empty_branches)) if non_empty_branches else None
        )

        return {
            "status": status,
            "branches": non_empty_branches,
            "primary_branch": primary,
            "conflicts": conflicts,
            "trace": all_trace,
        }

    # =====================================================================
    # Phase 6A (D3, D4, §8.2): soft-fallback path for /v1/calculate
    # =====================================================================

    def solve_with_fallback(
        self,
        constraints: Sequence[ConstraintV2 | ConstraintV1],
        active_scope: Optional[list[str]] = None,
        *,
        allow_soft_fallback: bool = True,
        strict: bool = False,
    ) -> dict[str, _ParameterResult]:
        """Solve with soft-fallback support. Returns dict keyed by parameter.

        D3: strict mode disables soft fallback (R6: "no inference" wins).
        D4: each `_ParameterResult.had_conflict` is True when the soft-fallback
        path was taken, so /v1/calculate can emit `conflict_details` to the
        audit log for lineage explainability.

        Return shape: `dict[str, _ParameterResult]` (V1 shape) — distinct from
        the V2 multi-branch shape returned by `solve()`, so the /v1/calculate
        endpoint can iterate per-parameter cleanly.
        """
        if not constraints:
            return {}

        constraints_v2 = [_ensure_v2(c) for c in constraints]
        hard, soft = _partition_by_priority(constraints_v2)

        # Group by parameter
        grouped: dict[str, list[ConstraintV2]] = defaultdict(list)
        for c in constraints_v2:
            param = normalize_parameter(c.parameter)
            grouped[param].append(c)

        # Solve hard constraints per parameter
        results: dict[str, _ParameterResult] = {}
        for param, param_constraints in grouped.items():
            results[param] = _solve_parameter(param, param_constraints, active_scope)

        # If hard is non-empty for all parameters, return as-is
        if not any(r.interval.empty for r in results.values()):
            return results

        # Hard is unsatisfiable for at least one parameter
        if strict:
            raise StrictViolationError(
                "Hard constraints are unsatisfiable and soft fallback is "
                "disabled by strict mode (R6 enforcement, D3)"
            )

        if not allow_soft_fallback or not soft:
            # No fallback path: return hard results (with had_conflict set
            # on the parameters that went empty).
            return results

        # D4: take soft path → mark each result had_conflict=True
        return self._intersect_with_fallback(soft, active_scope)

    def _intersect_with_fallback(
        self,
        soft: list[ConstraintV2],
        active_scope: Optional[list[str]],
    ) -> dict[str, _ParameterResult]:
        """Intersect only the soft (REFERENCE-priority) constraints.

        Returns `dict[str, _ParameterResult]` keyed by parameter. Each
        result has `had_conflict = True` so downstream callers can emit
        `conflict_details` audit events.
        """
        grouped: dict[str, list[ConstraintV2]] = defaultdict(list)
        for c in soft:
            param = normalize_parameter(c.parameter)
            grouped[param].append(c)

        results: dict[str, _ParameterResult] = {}
        for param, param_constraints in grouped.items():
            result = _solve_parameter(param, param_constraints, active_scope)
            result.had_conflict = True
            results[param] = result
        return results


# =============================================================================
# Internal helpers
# =============================================================================


def _get_branch_key(constraint: ConstraintV2) -> str:
    """Get branch key from constraint conditions.

    Returns "general" for no conditions, otherwise the first
    environment/condition parameter value as the branch identifier.
    """
    if not constraint.conditions:
        return "general"
    for cond in constraint.conditions:
        if cond.parameter in ("environment", "condition"):
            if cond.value:
                return str(cond.value)
    return "general"


def _solve_parameter(
    param: str,
    constraints: list[ConstraintV2],
    active_scope: Optional[list[str]],
) -> _ParameterResult:
    """Solve constraints for a single parameter (V2).

    Returns _ParameterResult with interval, unit, confidence, evidence, trace.
    """
    # Sort by priority.explicit_level (desc), then confidence (desc)
    sorted_constraints = sorted(
        constraints,
        key=lambda c: (-c.priority.get("explicit_level", 0), -c.confidence),
    )

    # Current working interval (starts as open(-inf, +inf) = full real line)
    interval = portion.open(-portion.inf, portion.inf)

    unit = "°C"
    confidence = 1.0
    evidence: list[Evidence] = []
    trace: list[_TraceEntry] = []

    applied_any = False
    had_conflict = False

    for c in sorted_constraints:
        # --- Scope filter ---
        if active_scope is not None:
            if not _scope_matches(c.scope_path, active_scope):
                trace.append(_TraceEntry(
                    parameter=param,
                    action="skipped_scope",
                    operator="interval",
                    value=c.interval,
                    interval_snapshot=str(interval),
                    reason=f"scope {c.scope_path} != {active_scope}",
                ))
                continue

        # --- Build constraint interval from V2 interval dict ---
        c_interval = _v2_interval_to_portion(c.interval)
        if c_interval is None:
            continue  # Skip if no interval

        # --- Get unit (handle temperature affine) ---
        unit = c.unit
        if _is_temperature_unit(c.unit):
            # Extract scalar value for temperature conversion
            scalar = c.scalar_value if c.value_type == "scalar" else None
            if scalar is not None:
                _, unit = normalize_temperature(scalar, c.unit)

        trace.append(_TraceEntry(
            parameter=param,
            action="applied",
            operator="interval",
            value=c.interval,
            interval_snapshot=str(interval),
        ))

        # --- Intersect ---
        new_interval = interval & c_interval
        if new_interval.empty:
            # Conflict — record but keep going to record full trace
            trace[-1].action = "conflict"
            trace[-1].reason = f"intersection with interval {c.interval} is empty"
            interval = new_interval
            had_conflict = True
        else:
            interval = new_interval
            applied_any = True
            confidence = min(confidence, c.confidence)
            if c.source:
                ev = Evidence(
                    doc_id=c.source.get("doc_id", ""),
                    block_id="",
                    page_num=None,
                    scope_path=c.scope_path,
                    source_text="",
                )
                evidence.append(ev)

    if not applied_any:
        interval = portion.Interval()  # empty — nothing was applied

    return _ParameterResult(
        interval=interval,
        unit=unit,
        confidence=confidence,
        evidence=evidence,
        trace=trace,
        had_conflict=had_conflict,
    )


def _v2_interval_to_portion(interval_dict: dict | None) -> portion.Interval | None:
    """Convert V2 interval dict to portion.Interval using factory functions.

    V2 interval dict: {lower, upper, lower_inclusive, upper_inclusive}
    portion factory functions:
      closed(lower, upper)      — [lower, upper]
      open(lower, upper)        — (lower, upper)
      closedopen(lower, upper)  — [lower, upper)
      openclosed(lower, upper)  — (lower, upper]
    """
    if interval_dict is None:
        return None

    lower = interval_dict.get("lower")
    upper = interval_dict.get("upper")
    lower_inc = interval_dict.get("lower_inclusive", True)
    upper_inc = interval_dict.get("upper_inclusive", True)

    # Handle None bounds (infinite)
    if lower is None and upper is None:
        return portion.open(-portion.inf, portion.inf)
    elif lower is None:
        # Only upper bound
        if upper_inc:
            return portion.openclosed(-portion.inf, upper)
        else:
            return portion.open(-portion.inf, upper)
    elif upper is None:
        # Only lower bound
        if lower_inc:
            return portion.closedopen(lower, portion.inf)
        else:
            return portion.open(lower, portion.inf)
    else:
        # Both bounds specified
        if lower_inc and upper_inc:
            return portion.closed(lower, upper)
        elif lower_inc and not upper_inc:
            return portion.closedopen(lower, upper)
        elif not lower_inc and upper_inc:
            return portion.openclosed(lower, upper)
        else:
            return portion.open(lower, upper)


def _scope_matches(constraint_scope: Optional[list[str]], active_scope: list[str]) -> bool:
    """Check if constraint scope matches active scope.

    Constraint scope must be a prefix of active_scope (constraint applies
    at least as broadly as the query). OR constraint scope is None
    (unscoped = applies everywhere).
    """
    if constraint_scope is None:
        return True
    # constraint_scope must be a prefix of active_scope
    if len(constraint_scope) > len(active_scope):
        return False
    return active_scope[:len(constraint_scope)] == constraint_scope


def _is_temperature_unit(unit: str) -> bool:
    """Check if unit is a temperature unit."""
    u = unit.upper().replace("°", "")
    return u in ("C", "F", "K")


def _trace_entry_to_dict(entry: _TraceEntry) -> dict:
    return {
        "parameter": entry.parameter,
        "action": entry.action,
        "operator": entry.operator,
        "value": entry.value,
        "interval_snapshot": entry.interval_snapshot,
        "reason": entry.reason,
    }


def _partition_by_priority(
    constraints: list[ConstraintV2],
) -> tuple[list[ConstraintV2], list[ConstraintV2]]:
    """Split constraints into (hard, soft) by explicit_level.

    Hard: PROJECT (40) and above — the default "active" rules.
    Soft: REFERENCE (20) and below — fallback pool used when hard is empty.
    """
    hard: list[ConstraintV2] = []
    soft: list[ConstraintV2] = []
    for c in constraints:
        level = c.priority.get("explicit_level", int(Priority.PROJECT))
        if level >= int(Priority.PROJECT):
            hard.append(c)
        else:
            soft.append(c)
    return hard, soft
