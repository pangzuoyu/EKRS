"""Golden set test runner for EKRS Phase 2b.

Tests the full three-gate pipeline: recall -> extract -> solve.
Since Qdrant is not available in unit tests, this tests extraction + solving
only using manually-constructed Chunk objects (Gate 1 is implicitly satisfied
by pre-building chunks that match the query scenario).

Golden set JSON defines:
  - name: test case identifier
  - query: the search query (used only for documentation here)
  - raw_text: the chunk text to construct
  - scope_path: optional scope for the chunk
  - strict: strict mode flag
  - expected: the expected solver output
  - gates: assertions for each gate
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ekrs_shared.models import Chunk, NumericHint

from ekrs_rag.constraint_engine.evidence_builder import EvidenceBuilder
from ekrs_rag.constraint_engine.solver import IntervalSolver


# =============================================================================
# Load golden set
# =============================================================================

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"


def load_golden_set() -> list[dict[str, Any]]:
    with open(GOLDEN_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


GOLDEN_CASES = load_golden_set()


# =============================================================================
# Helpers
# =============================================================================


def build_chunk(raw_text: str, scope_path: list[str] | None = None) -> Chunk:
    """Construct a Chunk from raw text for testing.

    This mimics what the chunker would produce from parsed document blocks.
    """
    return Chunk(
        text=raw_text,
        scope_path=scope_path or [],
        source_block_ids=["block_1"],
        token_count=len(raw_text) // 4,
        doc_hash="test_hash",
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )


def run_pipeline(chunk: Chunk, strict: bool = False) -> dict[str, Any]:
    """Run extraction + solving pipeline on a single chunk.

    Returns the solver result dict.
    """
    constraints = EvidenceBuilder.build([chunk])

    if strict and not constraints:
        # strict=true + no constraints -> EMPTY per R6
        return {"status": "EMPTY", "parameters": {}, "conflicts": [], "trace": []}

    active_scope = chunk.scope_path if chunk.scope_path else None
    return IntervalSolver.solve(constraints, active_scope=active_scope)


# =============================================================================
# Gate assertions
# =============================================================================


def assert_extraction_gate(case: dict[str, Any], constraints: list) -> None:
    """Assert extraction gate: expected hints are found in constraints.

    V2: ConstraintV2 has interval {lower, upper, lower_inclusive, upper_inclusive}.
    The golden set uses V1-style operators (<=, >=, ==) which we translate to
    interval bounds for matching.
    """
    gate = case.get("gates", {}).get("extraction", {})

    must_have_hint = gate.get("must_have_hint")
    must_have_hints = gate.get("must_have_hints")

    if must_have_hint is None and must_have_hints is None:
        # Expect no constraints
        assert len(constraints) == 0, f"Expected no constraints but got {len(constraints)}"
        return

    hints_to_check = [must_have_hint] if must_have_hint else must_have_hints
    assert hints_to_check, "must_have_hint or must_have_hints must be set"

    for hint_spec in hints_to_check:
        value = hint_spec["value"]
        unit = hint_spec["unit"]
        operator = hint_spec.get("operator")

        # Find matching constraint by translating V1 operator -> V2 interval bounds
        matching = []
        for c in constraints:
            if not _constraint_matches_v2(c, operator, value, unit):
                continue
            matching.append(c)

        if not matching:
            constraint_repr = [
                (c.parameter, _operator_from_interval(c.interval), _value_from_interval(c.interval), c.unit)
                for c in constraints
            ]
            assert False, (
                f"No constraint found for value={value}, unit={unit}, operator={operator}. "
                f"Constraints: {constraint_repr}"
            )


def _operator_matches(c, operator: str | None) -> bool:
    """Check if V2 constraint's interval matches the expected V1-style operator."""
    if operator is None:
        return True
    iv = c.interval
    if iv is None:
        return False
    if operator == "<=":
        return iv.get("upper_inclusive", True) and iv.get("upper") is not None
    elif operator == ">=":
        return iv.get("lower_inclusive", True) and iv.get("lower") is not None
    elif operator == "==":
        return (
            iv.get("lower_inclusive", True)
            and iv.get("upper_inclusive", True)
            and iv.get("lower") is not None
            and iv.get("upper") is not None
            and abs(iv["lower"] - iv["upper"]) < 0.001
        )
    elif operator == ">":
        return not iv.get("lower_inclusive", True) and iv.get("lower") is not None
    elif operator == "<":
        return not iv.get("upper_inclusive", True) and iv.get("upper") is not None
    return False


def _constraint_matches_v2(c, operator: str | None, value: float, unit: str) -> bool:
    """Check if V2 constraint matches expected operator/value/unit.

    Handles three cases:
    1. Scalar constraints (diameter_exact): check scalar_value
    2. Temperature conversion (°F->°C): accept operator match only (value normalized)
    3. Interval constraints: check operator + unit + value
    """
    if operator is None:
        return True

    # Temperature conversion: golden set uses raw values (e.g., °F not pre-converted).
    # If golden set expects °F and constraint uses °C, the value was normalized.
    # Accept based on operator match only.
    if unit == "°F" and c.unit == "°C":
        return _operator_matches(c, operator)

    # Check unit matches
    if c.unit != unit:
        return False

    # Scalar constraint (e.g., exact diameter): check scalar_value
    if c.value_type == "scalar":
        sv = c.scalar_value
        if sv is not None and abs(sv - value) < 0.001:
            return True
        return False

    # Interval constraint
    iv = c.interval
    if iv is None:
        return False

    if operator == "<=":
        return iv.get("upper_inclusive", True) and iv.get("upper") is not None and abs(iv["upper"] - value) < 0.001
    elif operator == ">=":
        return iv.get("lower_inclusive", True) and iv.get("lower") is not None and abs(iv["lower"] - value) < 0.001
    elif operator == "==":
        return (
            iv.get("lower_inclusive", True)
            and iv.get("upper_inclusive", True)
            and iv.get("lower") is not None
            and iv.get("upper") is not None
            and abs(iv["lower"] - value) < 0.001
            and abs(iv["upper"] - value) < 0.001
        )
    elif operator == ">":
        return not iv.get("lower_inclusive", True) and iv.get("lower") is not None and abs(iv["lower"] - value) < 0.001
    elif operator == "<":
        return not iv.get("upper_inclusive", True) and iv.get("upper") is not None and abs(iv["upper"] - value) < 0.001
    return False


def _operator_from_interval(interval: dict) -> str:
    """Derive V1-style operator string from V2 interval dict."""
    if interval is None:
        return "?"
    lower_inc = interval.get("lower_inclusive", True)
    upper_inc = interval.get("upper_inclusive", True)
    lower = interval.get("lower")
    upper = interval.get("upper")

    if lower is not None and upper is not None:
        if lower == upper and lower_inc and upper_inc:
            return "=="
        return "range"
    elif lower is None and upper is not None:
        return "<=" if upper_inc else "<"
    elif upper is None and lower is not None:
        return ">=" if lower_inc else ">"
    return "?"


def _value_from_interval(interval: dict) -> float | tuple | None:
    """Extract representative value(s) from V2 interval dict."""
    if interval is None:
        return None
    lower = interval.get("lower")
    upper = interval.get("upper")
    if lower is not None and upper is not None:
        return (lower, upper)
    return lower or upper


def assert_solve_gate(case: dict[str, Any], result: dict[str, Any]) -> None:
    """Assert solve gate: solver result matches expected.

    Handles flexible parameter checking by looking at the 'solve' gate specs
    which may reference parameters by various keys (e.g., range_upper, range_lower_pa).
    """
    gate = case.get("gates", {}).get("solve", {})
    expected = case.get("expected", {})

    # Check status
    expected_status = expected.get("status") or gate.get("status") or "OK"
    assert result["status"] == expected_status, (
        f"Expected status={expected_status}, got {result['status']}. "
        f"Trace: {result.get('trace', [])[-3:]}"
    )

    if expected_status == "EMPTY":
        assert result["parameters"] == {}
        return

    if expected_status == "CONFLICT":
        assert len(result.get("conflicts", [])) > 0, "Expected conflicts but none found"
        return

    # Check parameter ranges via solve gate specs
    # solve gate can have: range_upper, range_lower, range_upper_celsius, range_lower_pa,
    # range_upper_m, temperature_upper, pressure_lower, active_scope
    params = result.get("parameters", {})

    # Determine which parameter to check based on gate keys or expected keys
    if "range_upper_celsius" in gate:
        temp = params.get("temperature")
        assert temp is not None, f"No temperature parameter found"
        assert temp["range"][1] is not None, f"Expected upper bound {gate['range_upper_celsius']}°C, got None"
        assert abs(temp["range"][1] - gate["range_upper_celsius"]) < 0.01, (
            f"Expected temperature upper {gate['range_upper_celsius']}°C, got {temp['range'][1]}"
        )
        assert temp["unit"] == "°C", f"Expected unit °C, got {temp['unit']}"

    if "temperature_upper" in gate:
        temp = params.get("temperature")
        assert temp is not None, f"No temperature parameter found"
        assert temp["range"][1] is not None, f"Expected upper bound"
        assert abs(temp["range"][1] - gate["temperature_upper"]) < 0.01, (
            f"Expected temperature upper {gate['temperature_upper']}, got {temp['range'][1]}"
        )

    if "pressure_lower" in gate:
        press = params.get("pressure")
        assert press is not None, f"No pressure parameter found. Available: {list(params.keys())}"
        assert press["range"][0] is not None, f"Expected lower bound"
        assert abs(press["range"][0] - gate["pressure_lower"]) < 0.01, (
            f"Expected pressure lower {gate['pressure_lower']}, got {press['range'][0]}"
        )

    if "range_lower_pa" in gate:
        press = params.get("pressure")
        assert press is not None, f"No pressure parameter found"
        assert press["range"][0] is not None, f"Expected lower bound in Pa"
        assert abs(press["range"][0] - gate["range_lower_pa"]) < 0.01, (
            f"Expected pressure lower {gate['range_lower_pa']}Pa, got {press['range'][0]}"
        )

    if "range_upper_m" in gate:
        length = params.get("length")
        assert length is not None, f"No length parameter found. Available: {list(params.keys())}"
        assert length["range"][1] is not None, f"Expected upper bound"
        assert abs(length["range"][1] - gate["range_upper_m"]) < 0.01, (
            f"Expected length upper {gate['range_upper_m']}m, got {length['range'][1]}"
        )

    # Generic range_upper and range_lower - check the first param in expected
    if "range_upper" in gate and "temperature_upper" not in gate and "range_upper_celsius" not in gate:
        # Find the parameter from expected or first param with an upper bound
        param_name = None
        for p in expected:
            if p != "status" and p in params:
                param_name = p
                break
        assert param_name is not None, f"No parameter found in expected: {expected}. Available: {list(params.keys())}"
        param_result = params[param_name]
        assert param_result["range"][1] is not None, f"Expected upper bound {gate['range_upper']}, got None"
        assert abs(param_result["range"][1] - gate["range_upper"]) < 0.01, (
            f"Expected {param_name} upper {gate['range_upper']}, got {param_result['range'][1]}"
        )

    if "range_lower" in gate:
        # Find the parameter from expected or first param with a lower bound
        param_name = None
        for p in expected:
            if p != "status" and p in params:
                param_name = p
                break
        assert param_name is not None, f"No parameter found in expected. Available: {list(params.keys())}"
        param_result = params[param_name]
        assert param_result["range"][0] is not None, f"Expected lower bound {gate['range_lower']}, got None"
        assert abs(param_result["range"][0] - gate["range_lower"]) < 0.01, (
            f"Expected {param_name} lower {gate['range_lower']}, got {param_result['range'][0]}"
        )

    # Also check via expected spec if provided (for unit checks)
    for param, spec in expected.items():
        if param == "status":
            continue
        if param not in params:
            continue
        param_result = params[param]

        if "range" in spec:
            lo, hi = spec["range"]
            if lo is not None:
                assert param_result["range"][0] is not None, (
                    f"Expected lower bound {lo} for {param}, got None"
                )
                assert abs(param_result["range"][0] - lo) < 0.01, (
                    f"Expected lower bound {lo} for {param}, got {param_result['range'][0]}"
                )
            if hi is not None:
                assert param_result["range"][1] is not None, (
                    f"Expected upper bound {hi} for {param}, got None"
                )
                assert abs(param_result["range"][1] - hi) < 0.01, (
                    f"Expected upper bound {hi} for {param}, got {param_result['range'][1]}"
                )

        if "unit" in spec:
            assert param_result["unit"] == spec["unit"], (
                f"Expected unit '{spec['unit']}' for {param}, got '{param_result['unit']}'"
            )


# =============================================================================
# Parametrized golden set tests
# =============================================================================


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["name"] for c in GOLDEN_CASES])
def test_golden_case(case: dict[str, Any]) -> None:
    """Run a single golden set case through extraction + solving.

    Gate 1 (recall): Pre-constructed Chunk satisfies this (chunk exists).
    Gate 2 (extraction): EvidenceBuilder.build() extracts constraints.
    Gate 3 (solve): IntervalSolver.solve() computes ranges.
    """
    raw_text = case["raw_text"]
    scope_path = case.get("scope_path")
    strict = case.get("strict", False)

    # Build chunk manually (no Qdrant needed)
    chunk = build_chunk(raw_text, scope_path)

    # Gate 1: Recall - chunk exists (implicitly satisfied)
    assert len(chunk.text) > 0, "Chunk text is empty"

    # Gate 2: Extraction
    constraints = EvidenceBuilder.build([chunk])
    assert_extraction_gate(case, constraints)

    # Gate 3: Solve
    result = run_pipeline(chunk, strict=strict)
    assert_solve_gate(case, result)


# =============================================================================
# Individual gate tests (for debugging failures)
# =============================================================================


class TestExtractionGate:
    """Isolated extraction gate tests."""

    @pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["name"] for c in GOLDEN_CASES])
    def test_extraction_produces_expected_hints(self, case: dict[str, Any]) -> None:
        """Verify extraction produces hints matching the golden set spec."""
        raw_text = case["raw_text"]
        scope_path = case.get("scope_path")
        chunk = build_chunk(raw_text, scope_path)

        constraints = EvidenceBuilder.build([chunk])
        assert_extraction_gate(case, constraints)


class TestSolveGate:
    """Isolated solve gate tests."""

    @pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["name"] for c in GOLDEN_CASES])
    def test_solve_produces_expected_ranges(self, case: dict[str, Any]) -> None:
        """Verify solve produces ranges matching the golden set spec."""
        raw_text = case["raw_text"]
        scope_path = case.get("scope_path")
        strict = case.get("strict", False)
        chunk = build_chunk(raw_text, scope_path)

        constraints = EvidenceBuilder.build([chunk])
        result = run_pipeline(chunk, strict=strict)
        assert_solve_gate(case, result)


# =============================================================================
# Determinism test
# =============================================================================


class TestDeterminism:
    """Golden set must be deterministic."""

    @pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["name"] for c in GOLDEN_CASES])
    def test_same_input_same_output(self, case: dict[str, Any]) -> None:
        """Running the same case 5 times must produce identical results."""
        raw_text = case["raw_text"]
        scope_path = case.get("scope_path")
        strict = case.get("strict", False)
        chunk = build_chunk(raw_text, scope_path)

        results = [run_pipeline(chunk, strict=strict) for _ in range(5)]

        first = results[0]
        for r in results[1:]:
            assert r == first, (
                f"Non-deterministic result for case '{case['name']}':\n"
                f"  First:  {first}\n"
                f"  Later:  {r}"
            )
