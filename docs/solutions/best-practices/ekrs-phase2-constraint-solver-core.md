---
title: Building the EKRS Phase 2 deterministic constraint solver core
date: 2026-04-10
category: docs/solutions/best-practices/
module: EKRS/rag
problem_type: best_practice
component: development_workflow
severity: medium
applies_when:
  - Implementing Phase 2 of EKRS (deterministic solver core)
  - Building TDD pure functions with interval arithmetic
  - Integrating numeric hint extraction with constraint solving
tags: [ekrs, constraint-solver, interval-arithmetic, tdd, pure-functions, portion]
---

# Building the EKRS Phase 2 Deterministic Constraint Solver Core

## Context

Phase 2 of EKRS implements the deterministic constraint solver core — extracting numeric constraints (temperature, pressure, etc.) from engineering documents and computing feasible parameter ranges. The key design constraint from the spec: the solver must be a pure function with no I/O, enabling deterministic unit tests. Phase 1 had established the ingestion pipeline, Qdrant retrieval (dummy vectors), and shared models.

The implementation broke into two sub-phases: 2a (pure functions: solver, normalizer, parser, evidence builder, hint extractor) and 2b (retrieval + API + golden set).

## Guidance

### 1. Interval Arithmetic with `portion.Interval`

Use the `portion` library for interval arithmetic. **Do not use `Interval(a, b)` constructor** — it does not accept `left`/`right` keyword arguments and the direct constructor with infinity does not work as expected.

```python
import portion as P

# Correct factory functions
unbounded = P.open(-P.inf, P.inf)  # (-∞, ∞)
le_80 = P.closedopen(-P.inf, 80) # (-∞, 80] — <= 80
ge_60 = P.openclosed(60, P.inf)   # [60, ∞) — >= 60
range_10_80 = P.closed(10, 80)   # [10, 80]
```

**Key rule**: `<=` operator → `closedopen(-inf, val)` (right boundary is CLOSED). `>=` operator → `openclosed(val, inf)` (left boundary is CLOSED). Range → `closed(lo, hi)`.

### 2. Affine Temperature Conversion

Fahrenheit to Celsius requires affine conversion, not scalar. The formula is `(F-32)*5/9`. Do not use the simpler `(F-32)*0.5556` approximation in engineering contexts — the spec mandates affine.

```python
def normalize_temperature(value: float, from_unit: str) -> tuple[float, str]:
    if from_unit in ("°F", "F"):
        celsius = (value - 32) * 5 / 9
        return celsius, "°C"
    elif from_unit in ("K",):
        return value - 273.15, "°C"
    return value, from_unit  # passthrough
```

### 3. Pure Function Solver Structure

The solver accepts `List[Constraint]` and `Optional[List[str]] active_scope`, returns a structured dict. No I/O. Determinism is verified by running the same input 10x and asserting identical output.

```python
@dataclass
class _ParameterResult:
    interval: P.Interval
    unit: str
    confidence: float
    evidence: list[Evidence]
    trace: list[_TraceEntry]
    had_conflict: bool = False

def solve(constraints, active_scope=None):
    # Priority sort → group by parameter → intersect intervals
    # Track had_conflict to distinguish EMPTY vs CONFLICT
```

### 4. Dedup Key Excludes `scope_path` — Priority Wins Across Scopes

When deduplicating constraints from different scope paths (national vs reference), include `scope_path` in the **priority inference step**, not in the deduplication key. This lets constraints from different scopes compete on priority.

```python
# Dedup key: (parameter, operator, value, unit) — no scope_path
key = (c.parameter, c.operator, str(c.value), c.unit)
# Priority inferred from scope_path prefix
_SCOPE_PRIORITY_MAP = {
    "national": 100, "industry": 80, "enterprise": 60,
    "project": 40, "reference": 20,
}
```

If two constraints have identical (parameter, operator, value, unit) but different scope paths, the higher-priority scope wins. If `scope_path` were in the dedup key, they would never compete.

### 5. Priority Hardcoded in Parser — Override in Evidence Builder

The regex parser (`ConstraintParser`) has no scope context and hardcodes `Priority.PROJECT`. The `EvidenceBuilder` is responsible for inferring the correct priority from each chunk's `scope_path` and overwriting `constraint.priority` before adding to the deduplication loop.

```python
for c in all_constraints:
    c.priority = _priority_from_scope_path(c.scope_path)
    key = (c.parameter, c.operator, str(c.value), c.unit)
    # ...deduplication logic
```

### 6. Measurement Verb Heuristic in Parser

The Chinese character "为" (=, equals) is ambiguous — it can mean a constraint ("直径为25mm") or a measurement reading ("测量温度为80°C"). Distinguish by checking the text before "为": if it starts with a measurement verb ("测量", "检测", "记录", "观测", "显示", "表明", "发现") followed by a short parameter name (≤3 chars), skip it.

**Critical bug to avoid**: the inner `for verb in _MEASUREMENT_VERBS` loop's `continue` only breaks the inner loop. Use a boolean `is_measurement` flag to propagate the decision to the outer operator loop.

```python
is_measurement = False
for verb in _MEASUREMENT_VERBS:
    if before.startswith(verb):
        remaining = before[len(verb):]
        if len(remaining) <= 3:
            is_measurement = True
            break
if is_measurement:
    continue  # skip to next operator pattern, not just next verb
```

### 7. Hint Span Anchoring for Regex Extraction

When extracting numeric hints from raw text, store `span=(start, end)` relative to `chunk.text`. The parser uses these spans (±50 char context window) as anchors to find operator keywords near the numeric value. Without span anchoring, operator context is lost.

```python
hint = NumericHint(
    parameter_hint=parameter_hint,
    value=value,
    unit=unit,
    span=(m.start(), m.end()),  # critical for parser anchoring
    source_text=m.group(0),
)
```

### 8. Three-Gate Enforcement at API Level

The constraint query API enforces three gates at the service level — not inside the pure functions:

```
Gate 1 (Recall):    len(chunks) < MIN_RECALL_CHUNKS → 404
Gate 2 (Extract):  EvidenceBuilder.build([]) → 404
Gate 3 (Solve):     solver.status == "CONFLICT" → 200 + conflicts in response
```

Gate 3 returns 200 (partial success) rather than error — the solver correctly computed that constraints conflict, which is valuable signal.

### 9. Qdrant Named Vector Format

Qdrant's `search()` method requires named vector format when using named vectors (not raw positional). Phase 1 used positional vectors. Phase 2 switched to named:

```python
# Wrong (positional):
client.search(collection, vector=query_vector, ...)

# Correct (named):
client.search(collection, query_vector=("dense", query_vector), ...)
```

Also, `ensure_collection()` must detect vector size mismatch (Phase 1: 1024d, Phase 2: 384d for bge-small) and recreate the collection.

## Why This Matters

Engineering constraint solving is inherently deterministic — the same document must produce the same constraints regardless of system state. Enforcing pure functions in the solver makes this testable without database or network dependencies. The interval arithmetic approach using `portion.Interval` handles open/closed boundary ambiguity that float comparisons cannot.

The scope-aware priority system (national > industry > enterprise > project > reference) reflects real engineering standards hierarchy — a national code always overrides a project spec. Including scope in the dedup key would prevent this competition.

## When to Apply

- Building Phase 2 or later of EKRS
- Adding new constraint parameters (temperature, pressure, length, etc.)
- Extending the parser with new operator patterns
- Implementing any pure-function component with interval arithmetic

## Examples

**Solver output structure:**
```python
{
  "status": "OK",  # or "CONFLICT" or "EMPTY"
  "parameters": {
    "temperature": {
      "range": [10.0, 80.0],  # None = -inf, None = +inf
      "unit": "°C",
      "confidence": 0.95,
      "evidence": [...],
    }
  },
  "conflicts": [],  # only when CONFLICT
  "trace": [...],    # every intersection step
}
```

**Golden set test case (14 cases, 52 tests):**
```python
{
  "name": "fahrenheit_to_celsius",
  "query": "最高工作温度不得超过176°F",
  "gates": {
    "extraction": {"must_have_hint": {"value": 80, "unit": "°C", "operator": "<="}},
    "solve": {"range_upper": 80}
  }
}
```

## Related

- `rag/ekrs_rag/constraint_engine/solver.py` — pure interval solver
- `rag/ekrs_rag/constraint_engine/evidence_builder.py` — chunks→constraints pipeline
- `rag/ekrs_rag/ingestion/numeric_hint_extractor.py` — regex extraction
- `rag/ekrs_rag/constraint_engine/parser.py` — operator pattern matching
- `rag/tests/golden_set/golden_set.json` — 14 test cases
