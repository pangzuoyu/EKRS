"""Tests for IntervalSolver — RED phase (tests written before implementation)."""
from __future__ import annotations

import portion  # type: ignore[import]

import pytest
from ekrs_shared.models import Constraint, Priority

from ekrs_rag.constraint_engine.solver import IntervalSolver


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def empty_constraints() -> list[Constraint]:
    return []


@pytest.fixture
def single_le_constraint() -> list[Constraint]:
    """Temperature <= 80°C"""
    return [
        Constraint(
            parameter="temperature",
            operator="<=",
            value=80.0,
            unit="°C",
            priority=Priority.INDUSTRY,
            confidence=0.95,
            source={"block_id": "b1"},
        )
    ]


@pytest.fixture
def single_ge_constraint() -> list[Constraint]:
    """Temperature >= 10°C"""
    return [
        Constraint(
            parameter="temperature",
            operator=">=",
            value=10.0,
            unit="°C",
            priority=Priority.NATIONAL,
            confidence=0.90,
            source={"block_id": "b2"},
        )
    ]


@pytest.fixture
def range_constraint() -> list[Constraint]:
    """Temperature 10-80°C"""
    return [
        Constraint(
            parameter="temperature",
            operator="range",
            value=(10.0, 80.0),
            unit="°C",
            priority=Priority.NATIONAL,
            confidence=0.95,
            source={"block_id": "b3"},
        )
    ]


@pytest.fixture
def conflicting_constraints() -> list[Constraint]:
    """Temperature <= 50°C AND Temperature >= 80°C — conflict"""
    return [
        Constraint(
            parameter="temperature",
            operator="<=",
            value=50.0,
            unit="°C",
            priority=Priority.INDUSTRY,
            confidence=0.95,
            source={"block_id": "b1"},
        ),
        Constraint(
            parameter="temperature",
            operator=">=",
            value=80.0,
            unit="°C",
            priority=Priority.NATIONAL,
            confidence=0.90,
            source={"block_id": "b2"},
        ),
    ]


@pytest.fixture
def multi_parameter_constraints() -> list[Constraint]:
    """Temperature and pressure constraints"""
    return [
        Constraint(
            parameter="temperature",
            operator="<=",
            value=80.0,
            unit="°C",
            priority=Priority.INDUSTRY,
            confidence=0.95,
            source={"block_id": "b1"},
        ),
        Constraint(
            parameter="pressure",
            operator=">=",
            value=1.0,
            unit="MPa",
            priority=Priority.NATIONAL,
            confidence=0.90,
            source={"block_id": "b2"},
        ),
    ]


@pytest.fixture
def priority_override_constraints() -> list[Constraint]:
    """Same parameter, different priorities — higher should win"""
    return [
        Constraint(
            parameter="temperature",
            operator="<=",
            value=100.0,
            unit="°C",
            priority=Priority.REFERENCE,  # 20 — low
            confidence=0.80,
            source={"block_id": "b1"},
        ),
        Constraint(
            parameter="temperature",
            operator="<=",
            value=60.0,
            unit="°C",
            priority=Priority.NATIONAL,  # 100 — high
            confidence=0.95,
            source={"block_id": "b2"},
        ),
    ]


@pytest.fixture
def scope_filter_constraints() -> list[Constraint]:
    """Constraints with different scope paths"""
    return [
        Constraint(
            parameter="temperature",
            operator="<=",
            value=80.0,
            unit="°C",
            priority=Priority.INDUSTRY,
            confidence=0.95,
            scope_path=["national", "GB"],
            source={"block_id": "b1"},
        ),
        Constraint(
            parameter="temperature",
            operator=">=",
            value=200.0,
            unit="°C",
            priority=Priority.NATIONAL,
            confidence=0.90,
            scope_path=["enterprise", "Acme"],
            source={"block_id": "b2"},
        ),
    ]


@pytest.fixture
def temperature_affine_constraints() -> list[Constraint]:
    """Temperature constraints in Fahrenheit — should be converted to Celsius"""
    return [
        Constraint(
            parameter="temperature",
            operator="<=",
            value=176.0,  # 80°C in Fahrenheit
            unit="°F",
            priority=Priority.INDUSTRY,
            confidence=0.95,
            source={"block_id": "b1"},
        )
    ]


# =============================================================================
# RED Phase — Tests that should FAIL until solver.py is implemented
# =============================================================================


class TestSolveEmpty:
    def test_empty_input_returns_empty_branches(self, empty_constraints):
        """Empty constraint list should return EMPTY status with no branches."""
        result = IntervalSolver.solve(empty_constraints)
        assert result["status"] == "EMPTY"
        assert result["branches"] == {}
        assert result["conflicts"] == []
        assert result["trace"] == []


class TestSolveSingle:
    def test_single_le_constraint(self, single_le_constraint):
        """<= constraint produces upper-bounded interval"""
        result = IntervalSolver.solve(single_le_constraint)
        assert result["status"] == "OK"
        assert "temperature" in result["branches"]["general"]
        temp = result["branches"]["general"]["temperature"]
        assert temp["range"][0] is None  # no lower bound
        assert temp["range"][1] == 80.0
        assert temp["unit"] == "°C"
        assert temp["confidence"] == 0.95

    def test_single_ge_constraint(self, single_ge_constraint):
        """>= constraint produces lower-bounded interval"""
        result = IntervalSolver.solve(single_ge_constraint)
        assert result["status"] == "OK"
        temp = result["branches"]["general"]["temperature"]
        assert temp["range"][0] == 10.0
        assert temp["range"][1] is None  # no upper bound

    def test_range_constraint(self, range_constraint):
        """range constraint produces bounded interval"""
        result = IntervalSolver.solve(range_constraint)
        assert result["status"] == "OK"
        temp = result["branches"]["general"]["temperature"]
        assert temp["range"][0] == 10.0
        assert temp["range"][1] == 80.0


class TestSolveConflict:
    def test_conflicting_constraints_detected(self, conflicting_constraints):
        """Conflicting constraints should return CONFLICT status"""
        result = IntervalSolver.solve(conflicting_constraints)
        assert result["status"] == "CONFLICT"
        assert len(result["conflicts"]) > 0
        # Should still have trace of the conflict
        assert len(result["trace"]) > 0


class TestSolveMultiParameter:
    def test_multi_parameter_constraints(self, multi_parameter_constraints):
        """Multiple parameters should all appear in result"""
        result = IntervalSolver.solve(multi_parameter_constraints)
        assert result["status"] == "OK"
        assert "temperature" in result["branches"]["general"]
        assert "pressure" in result["branches"]["general"]
        temp = result["branches"]["general"]["temperature"]
        assert temp["range"][1] == 80.0
        press = result["branches"]["general"]["pressure"]
        assert press["range"][0] == 1.0


class TestPriorityOrdering:
    def test_higher_priority_constraint_wins(self, priority_override_constraints):
        """When same parameter has multiple constraints, highest priority wins"""
        result = IntervalSolver.solve(priority_override_constraints)
        assert result["status"] == "OK"
        temp = result["branches"]["general"]["temperature"]
        # NATIONAL(100) << REFERENCE(20), so 60°C should be the upper bound
        assert temp["range"][1] == 60.0


class TestScopeFilter:
    def test_active_scope_filters_constraints(self, scope_filter_constraints):
        """Constraints whose scope_path doesn't match active_scope should be skipped"""
        # Filter to only national/GB scope
        result = IntervalSolver.solve(scope_filter_constraints, active_scope=["national", "GB"])
        assert result["status"] == "OK"
        temp = result["branches"]["general"]["temperature"]
        # Only the <=80°C constraint should be applied (from GB scope)
        assert temp["range"][1] == 80.0
        assert temp["range"][0] is None  # no lower bound from this constraint

    def test_no_scope_match_returns_empty(self, scope_filter_constraints):
        """When no constraint matches active_scope, return EMPTY"""
        result = IntervalSolver.solve(
            scope_filter_constraints,
            active_scope=["project", "secret"],
        )
        assert result["status"] == "EMPTY"


class TestTemperatureAffine:
    def test_fahrenheit_to_celsius_conversion(self, temperature_affine_constraints):
        """°F constraints should be converted using affine transformation"""
        result = IntervalSolver.solve(temperature_affine_constraints)
        assert result["status"] == "OK"
        temp = result["branches"]["general"]["temperature"]
        # 80°C = (80-32)*9/5 = 176°F — but we check the conversion is affine
        # The constraint is 176°F, so upper bound should be (176-32)*5/9 = 80°C
        assert abs(temp["range"][1] - 80.0) < 0.01
        assert temp["unit"] == "°C"  # unit should be normalized to °C


class TestDeterminism:
    def test_same_input_produces_same_output_10x(self, multi_parameter_constraints):
        """Solver must be deterministic — same input always gives same output"""
        results = [IntervalSolver.solve(multi_parameter_constraints) for _ in range(10)]
        assert all(r == results[0] for r in results), "Solver is non-deterministic!"


class TestTrace:
    def test_trace_contains_every_step(self, multi_parameter_constraints):
        """Trace should record every constraint application or rejection"""
        result = IntervalSolver.solve(multi_parameter_constraints)
        assert len(result["trace"]) >= len(multi_parameter_constraints)
        # Each trace entry should describe what happened
        for entry in result["trace"]:
            assert "parameter" in entry
            assert "action" in entry
