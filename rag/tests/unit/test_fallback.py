"""Tests for IntervalSolver.solve_with_fallback() with allow_soft_fallback (spec §8.2, D3 strict-priority).

DEVIATION FROM BRIEF: tests call `solver.solve_with_fallback()` instead of
`solver.solve()`. Reason: the existing `solve()` returns the V2 multi-branch
shape (`{status, branches, primary_branch, conflicts, trace}`) consumed by
`/v1/constraints`. The brief's tests use the V1 shape (`dict[str, _ParameterResult]`).
Adding a separate `solve_with_fallback()` method preserves the V2 endpoint
unchanged while exposing the soft-fallback path for `/v1/calculate`.
"""
from __future__ import annotations

import pytest
import portion  # type: ignore[import]

from ekrs_shared.models import Constraint, Priority

from ekrs_rag.constraint_engine.solver import IntervalSolver


def _hard(value: float, op: str = "<=", priority=Priority.NATIONAL) -> Constraint:
    return Constraint(
        parameter="temperature", operator=op, value=value, unit="°C",
        priority=priority, confidence=0.95, source={"block_id": "b1"},
    )


def _soft(value: float, op: str = "<=") -> Constraint:
    return Constraint(
        parameter="temperature", operator=op, value=value, unit="°C",
        priority=Priority.REFERENCE, confidence=0.5, source={"block_id": "b2"},
    )


def test_hard_non_empty_direct_solve():
    solver = IntervalSolver()
    result = solver.solve_with_fallback([_hard(100, "<=")], allow_soft_fallback=True)
    assert result["temperature"].interval.upper <= 100


def test_hard_empty_with_soft_falls_back_when_allowed():
    solver = IntervalSolver()
    # Hard constraints are mutually exclusive (impossible)
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    result = solver.solve_with_fallback(
        hard + soft, allow_soft_fallback=True, strict=False
    )
    # Soft constraint temp <= 200 should be the result
    assert result["temperature"].interval.upper <= 200


def test_strict_blocks_soft_fallback_returns_400_via_caller():
    """Caller maps StrictViolationError → 400 strict_violation (R6 enforcement)."""
    from ekrs_rag.constraint_engine.solver import StrictViolationError
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    with pytest.raises(StrictViolationError):
        solver.solve_with_fallback(
            hard + soft, allow_soft_fallback=True, strict=True
        )


def test_allow_soft_false_blocks_fallback():
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    result = solver.solve_with_fallback(
        hard + soft, allow_soft_fallback=False, strict=False
    )
    # Hard empty + no fallback → empty interval
    assert result["temperature"].interval == portion.empty()


def test_no_soft_constraints_returns_empty_when_hard_empty():
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    result = solver.solve_with_fallback(hard, allow_soft_fallback=True, strict=False)
    assert result["temperature"].interval == portion.empty()


def test_default_allow_soft_true_preserves_backward_compat():
    """Existing callers that don't pass allow_soft_fallback still get fallback."""
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    result = solver.solve_with_fallback(hard + soft)  # defaults
    assert result["temperature"].interval.upper <= 200
