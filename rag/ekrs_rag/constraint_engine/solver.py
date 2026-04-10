"""IntervalSolver — pure-function constraint solver using portion.Interval arithmetic."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

import portion  # type: ignore[import]

from ekrs_shared.models import Constraint, Evidence
from ekrs_shared.normalizer import normalize_temperature, normalize_parameter


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


class IntervalSolver:
    """Pure-function interval arithmetic constraint solver.

    R2: No I/O, no state, no side effects.
    """

    @staticmethod
    def solve(
        constraints: list[Constraint],
        active_scope: Optional[list[str]] = None,
    ) -> dict:
        """Solve constraints and return structured result.

        Args:
            constraints: List of Constraint objects
            active_scope: Optional scope filter (scope_path must match to be applied)

        Returns:
            {
                "status": "OK" | "CONFLICT" | "EMPTY",
                "parameters": {
                    "temperature": {
                        "range": [lower, upper],  # None = -inf, None = +inf
                        "unit": "°C",
                        "confidence": 0.95,
                        "evidence": [...],
                    },
                    ...
                },
                "conflicts": [...],  # only if CONFLICT
                "trace": [...],  # every intersection step
            }
        """
        if not constraints:
            return {"status": "EMPTY", "parameters": {}, "conflicts": [], "trace": []}

        # Group constraints by normalized parameter
        grouped: dict[str, list[Constraint]] = defaultdict(list)
        for c in constraints:
            param = normalize_parameter(c.parameter)
            grouped[param].append(c)

        # Process each parameter group
        parameters: dict[str, dict] = {}
        all_trace: list[dict] = []
        has_conflict = False
        conflicts: list[dict] = []

        for param, param_constraints in grouped.items():
            result = _solve_parameter(param, param_constraints, active_scope)
            all_trace.extend(_trace_entry_to_dict(t) for t in result.trace)

            if result.had_conflict:
                # Constraints were applied but resulted in empty intersection
                has_conflict = True
                conflicts.append({
                    "parameter": param,
                    "interval": str(result.interval),
                    "constraints": [
                        {"operator": c.operator, "value": c.value, "unit": c.unit}
                        for c in param_constraints
                    ],
                })
            elif result.interval.empty or result.interval == portion.Interval():
                # No constraints applied at all (e.g. all filtered by scope)
                continue
            else:
                # Extract closed/open bounds
                # portion uses CLOSED=0, OPEN=1 for bound types
                if result.interval.left == portion.CLOSED:
                    lo_val: Optional[float] = result.interval.lower
                else:
                    lo_val = None
                if result.interval.right == portion.CLOSED:
                    hi_val: Optional[float] = result.interval.upper
                else:
                    hi_val = None

                parameters[param] = {
                    "range": [lo_val, hi_val],
                    "unit": result.unit,
                    "confidence": result.confidence,
                    "evidence": [e.model_dump() for e in result.evidence],
                }

        # Determine status
        if not parameters and not has_conflict:
            status = "EMPTY"
        elif has_conflict:
            status = "CONFLICT"
        else:
            status = "OK"

        return {
            "status": status,
            "parameters": parameters,
            "conflicts": conflicts,
            "trace": all_trace,
        }


# =============================================================================
# Internal helpers
# =============================================================================


def _solve_parameter(
    param: str,
    constraints: list[Constraint],
    active_scope: Optional[list[str]],
) -> _ParameterResult:
    """Solve constraints for a single parameter.

    Returns _ParameterResult with interval, unit, confidence, evidence, trace.
    """
    # Sort by priority (desc), then confidence (desc)
    sorted_constraints = sorted(
        constraints,
        key=lambda c: (-c.priority.value, -c.confidence),
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
                    operator=c.operator,
                    value=c.value,
                    interval_snapshot=str(interval),
                    reason=f"scope {c.scope_path} != {active_scope}",
                ))
                continue

        # --- Convert value (handle temperature affine) ---
        value = c.value
        unit = c.unit
        if _is_temperature_unit(c.unit):
            value, unit = normalize_temperature(c.value, c.unit)

        # --- Build constraint interval ---
        c_interval = _operator_to_interval(c.operator, value)

        trace.append(_TraceEntry(
            parameter=param,
            action="applied",
            operator=c.operator,
            value=c.value,
            interval_snapshot=str(interval),
        ))

        # --- Intersect ---
        new_interval = interval & c_interval
        if new_interval.empty:
            # Conflict — record but keep going to record full trace
            trace[-1].action = "conflict"
            trace[-1].reason = f"intersection with {c.operator}{c.value} is empty"
            interval = new_interval
            had_conflict = True
        else:
            interval = new_interval
            applied_any = True
            confidence = min(confidence, c.confidence)
            if c.source:
                ev = Evidence(
                    doc_id=c.source.get("doc_id", ""),
                    block_id=c.source.get("block_id", ""),
                    page_num=c.source.get("page_num"),
                    scope_path=c.scope_path,
                    source_text=c.source.get("source_text", ""),
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


def _operator_to_interval(operator: str, value: float | tuple[float, float]) -> portion.Interval:
    """Convert a constraint operator and value to a portion.Interval.

    portion API:
      closed(a, b)     = [a, b]
      open(a, b)       = (a, b)
      closedopen(a, b) = [a, b)   — left closed, right open
      openclosed(a, b) = (a, b]   — left open, right closed
    """
    if operator == "<=":
        # upper bound closed: (-inf, value]
        return portion.openclosed(-portion.inf, value)
    elif operator == ">=":
        # lower bound closed: [value, +inf)
        return portion.closedopen(value, portion.inf)
    elif operator == "==":
        return portion.closed(value, value)
    elif operator == "range":
        lo, hi = value  # type: ignore[misc]
        return portion.closed(lo, hi)
    else:
        # Unknown operator — treat as no-op (full range)
        return portion.open(-portion.inf, portion.inf)


def _scope_matches(constraint_scope: Optional[list[str]], active_scope: list[str]) -> bool:
    """Check if constraint scope matches active scope.

    Active scope must be a prefix of constraint scope for a match.
    OR constraint scope is None (unscoped = applies everywhere).
    """
    if constraint_scope is None:
        return True
    # Active scope must match the constraint scope prefix
    if len(active_scope) > len(constraint_scope):
        return False
    return constraint_scope[:len(active_scope)] == active_scope


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
