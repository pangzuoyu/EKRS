# gstack Review Report — EKRS V3.0 Impact Assessment

**Branch:** master
**Date:** 2026-04-22
**Skill:** /gstack-review
**Type:** Specification change impact assessment (not PR review)

---

## Scope Check

- **Intent:** Evaluate V3.0 spec changes impact on Phase 2 solver core implementation
- **Delivered:** Impact assessment document with Q&A answers, migration scope, 7 new test cases, function specs
- **结论:** CLEAN — specification assessment, not code review

---

## Review Against Checklist

Since no code has been written yet, this review evaluates the **assessment quality** against the problem space.

### IR V2 Migration Scope — Correctly Identified

The assessment correctly identifies the dependency chain:
```
shared/ekrs_shared/models.py (Constraint V1)
  → parser.py → evidence_builder.py → solver.py → constraints.py API
  → 20+ test files
```

This is a **Big Bang migration** — all files must update simultaneously or tests fail. The assessment correctly rules out incremental migration.

**Confidence:** 9/10 — the chain is traceable and the conclusion is correct.

---

### MissingContextError Location — Correctly Resolved

Q2 answer is correct: `MissingContextError` belongs at API layer (`constraints.py`), not in solver.

Evidence from assessment:
- R2: Solver is pure function with no I/O or state
- R6: Strict mode is API-level behavior, not solver-internal

The assessment correctly diagrams the flow:
```python
# constraints.py — API layer
if strict:
    for c in constraints:
        if c.inferred:
            raise HTTPException(400, "missing_context: inferred constraint not allowed")
result = IntervalSolver.solve(constraints, ...)
```

**Confidence:** 9/10 — matches the existing code architecture.

---

### TC_STRICT_01 Test Type — Correctly Identified

Q3 answer is correct: TC_STRICT_01 needs integration test (FastAPI TestClient), not unit test.

Reason: `strict=true + inferred=true → 400` is an API-layer behavior. The solver itself is pure and has no concept of strict mode.

Current gap: `rag/tests/integration/` only has `test_ingestion.py`, missing `test_constraints.py`.

**Confidence:** 8/10 — the conclusion follows from R2/R6, but the gap (no existing integration test for constraints API) should be flagged explicitly.

---

## Spec Quality Issues

### 1. `infer_lifecycle()` Implementation Incomplete (Medium)

**Section 5.1** of the assessment lists 5 lifecycle scenarios, but the spec table has ambiguities:

| Trigger | text has "征求意见稿" | doc_type == "review" | text has "过渡期" | Default | Deprecated |
|---------|---------------------|---------------------|-----------------|---------|-----------|
| `lifecycle.status` | draft | review | transitional | active | deprecated |
| `is_binding` | false | false | true | true | false |

**Issues:**
- "征求意见稿" and `doc_type == "review"` are potentially overlapping — what if both are true?
- `doc_type` values not enumerated — what are valid values?
- "已被替代" (deprecated) has no clear trigger condition — just "文档被新版本替代"?

**Impact:** MEDIUM — this affects data integrity when parsing real documents.

**Suggested fix:** Add priority rules (e.g., explicit `doc_type` overrides text keywords), enumerate valid `doc_type` values, specify deprecated trigger (e.g., `superseded_by` field present).

---

### 2. `parse_interval()` Open Interval Spec Lacking (Medium)

Section 5.2 says:
- `>` / `大于` / `高于` → `lower_inclusive = false`
- `<` / `小于` / `低于` → `upper_inclusive = false`

But **missing:**
- What about `>=` and `<=`? (likely inclusive, but not stated)
- English `<` operator maps to `upper_inclusive` — confirmed?
- What about `≥` / `≤` Unicode symbols?
- Compound expressions like `50℃ < temperature ≤ 80℃` — how is `≤` parsed differently from `<`?

**Impact:** MEDIUM — missing cases will cause silent parsing failures or wrong interval bounds.

---

### 3. Unit Normalization Table Incomplete (Low)

Section 5.3 references `normalize_temperature` but only lists:
- `°C`/`℃`/`Celsius` → `C`
- `K` → `C` (affine: `K - 273.15`)
- `MPa`/`Pa` → pressure (multiplicative)
- `psi` → `Pa` (×6894.76)

**Missing:**
- `°F` → `C`? (affine: `(F-32)*5/9`)
- `K` vs `°K` (Kelvin vs degree Kelvin — same thing?)
- `bar` pressure unit (common in engineering specs, ≈ 0.1 MPa)
- `atm` (atmospheres)
- Temperature units in Kelvin: `K` vs `°R` (Rankine)

**Impact:** LOW — currently covered by existing implementation, but gaps will surface when real documents contain these units.

---

## Checklist Categories — Not Applicable (No Code Written)

| Category | Status |
|----------|--------|
| SQL & Data Safety | N/A — no code written |
| Race Conditions | N/A — no code written |
| LLM Output Trust Boundary | N/A — no code written |
| Shell Injection | N/A — no code written |
| Enum & Value Completeness | N/A — no code written |
| Async/Sync Mixing | N/A — no code written |
| Column/Field Name Safety | N/A — no code written |
| LLM Prompt Issues | N/A — no code written |
| Time Window Safety | N/A — no code written |
| Type Coercion | N/A — no code written |
| View/Frontend | N/A — no code written |
| Distribution & CI/CD | N/A — no code written |

---

## Recommendations

### Must Fix Before Migration Branch

1. **`infer_lifecycle()` spec** — Add priority rules for overlapping triggers, enumerate `doc_type` values, specify deprecated trigger condition.

2. **`parse_interval()` spec** — Cover `>=`, `<=`, Unicode symbols `≥`/`≤`, and compound expressions.

### Should Fix Before Migration Branch

3. **Unit normalization table** — Add `°F`, `bar`, `atm` to the spec.

### Nice to Have

4. **TC_STRICT_01 integration test scaffolding** — The assessment mentions `rag/tests/integration/test_constraints.py` needs creation. Consider adding a stub file in the migration branch upfront so it's not forgotten.

---

## Summary

| Dimension | Status |
|-----------|--------|
| Migration scope | Correctly identified as Big Bang |
| MissingContextError location | Correct (API layer) |
| TC_STRICT_01 test type | Correct (integration test) |
| High-impact items (4) | All valid |
| Medium-impact items (4) | All valid, 2 need spec fixes |
| Low-impact items (8) | Valid, unit table incomplete |
| New test cases (7) | All correctly identified |
| Spec quality | 2 medium gaps in `infer_lifecycle()` and `parse_interval()` |

**Overall:** Assessment is well-structured. The 2 medium spec gaps should be resolved before creating `feature/v2-migration` branch to avoid mid-migration spec revisions.

---

## Next Steps

1. Resolve `infer_lifecycle()` and `parse_interval()` spec ambiguities (ASK → user)
2. Create `feature/v2-migration` branch
3. Execute 9-step migration plan from Section VII of the impact assessment

**STATUS: DONE**