# gstack-plan-eng-review — Phase 6A Spec Closure

## Verdict: APPROVE WITH 12 ISSUES (1 P1 FIXED, 0 P1 REMAINING)

## Scope

- 1 spec under review: `docs/superpowers/specs/2026-07-14-phase6a-design.md` (commit `e15e958`, 14.3 KB)
- 9 spec gaps in 6A scope, 5 deferred to 6B
- 7 architecture decisions (D1-D7), 3 user-confirmed via AskUserQuestion
- 11 new files + 10 modified files, 3 new classes
- 9 vertical slices, each ≤ 4 files, single-commit-per-slice

## Step 0 — Scope Challenge

| Check | Result |
|-------|--------|
| 8-file / 2-class threshold | NOT triggered per slice (each slice 2-4 files) |
| Cumulative files (21) | Above threshold but distributed across 9 independent slices |
| Scope reducible? | No — 9 items are spec-mandated |
| Distribution check | N/A — no new artifact type |
| Completeness | Full — 23 unit + 7 golden + 2 integration, no "make it work first" shortcuts |
| Lake score | 8/10 — complete version chosen throughout |

**Scope accepted as-is.**

## Issues Found

### Architecture (6 issues)

| # | Sev | Conf | Issue | Resolution |
|---|-----|------|-------|------------|
| A1 | P1 | 7/10 | §7 issue 4 (parser doc-table writer) actually blocks #1 implementation, but spec marked "non-blocking" | FIXED: User chose "RAG self-writes, parser pushes metadata" — §2 #1 reason + §4 data flow updated, §7 issue 4 marked resolved |
| A2 | P2 | 6/10 | /v1/constraints/trace returns `lineage_snapshot` but field is 6A-new; old audit entries have no value | FIXED: §4 data flow notes "old trace → null" |
| A3 | P2 | 5/10 | §5 D6 "no padding tests" vs "末态薄覆盖文件兜底" wording contradiction | NOTED — implementation should clarify "兜底 = 补真实缺测, not padding" |
| A4 | P2 | 6/10 | §6 missing CI gate `pytest --cov-fail-under=85` | TODO 6B — add CI enforcement |
| A5 | P3 | 5/10 | `scope_filter` semantics undefined (prefix/exact/set?) | TODO 实施期 — issue #5 |
| A6 | P3 | 4/10 | `shared/ekrs_shared/audit.py` change impact on dev_ui unstated | Inferred no-impact (optional fields), should be confirmed at #7+#8 commit |

### Code Quality (4 issues)

| # | Sev | Conf | Issue | Resolution |
|---|-----|------|-------|------------|
| CQ1 | P2 | 8/10 | §4 error handling "503 Qdrant down (/trace 不受影响, 不算)" — /trace doesn't use Qdrant, line is contradictory | FIXED: Replaced with "注: /trace 读 audit.log 不依赖 Qdrant, 无 Qdrant 不可达分支" |
| CQ2 | P2 | 7/10 | §8 step 6 golden set 7 cases single commit may exceed 500 LOC | CARVE-OUT: User chose "keep 1 commit" — golden set is static data, exempt from 500 LOC cap. §8 step 6 annotated |
| CQ3 | P3 | 6/10 | test_documents_repo 4 cases missing edges (duplicate insert, invalid scope_path) | TODO 实施期 |
| CQ4 | P3 | 5/10 | /calculate straight-IR vs /v1/constraints Qdrant-extracted IR — need shared `solve_request_to_ir()` helper | TODO 实施期 |

### Test (9 gaps, all P3)

| Gap | Resolution |
|-----|------------|
| Duplicate doc_id insert conflict strategy | TODO 实施期 |
| scope_filter prefix match semantics | Spec issue #5 待解 |
| Old trace → lineage_snapshot=null golden case | TODO 实施期 (1 golden case) |
| A1 path E2E (parser notify → DocumentRepo.insert) | TODO 实施期 |
| DocumentRepo.close() lifespan graceful shutdown | TODO 实施期 |
| 0006 migration idempotent re-run | TODO 实施期 |
| /trace, /calculate router registration | Implicit via E2E |
| DocumentRepo list empty case | implicit in CRUD test |
| scope_filter 边界 case | TODO 实施期 |

### Performance (2 issues, both P3)

| # | Sev | Conf | Issue | Resolution |
|---|-----|------|-------|------------|
| PF1 | P3 | 5/10 | /v1/constraints/trace linear audit.log scan;万级 trace p95 > 100ms | TODO 6B — add secondary index |
| PF2 | P3 | 4/10 | DocumentRepo.insert no batch;10k docs → 10k INSERT, no tx | TODO 6B — add batch + tx wrapper |

## Iron Rules

- ✅ R1-R8 unchanged
- ✅ 15 audit event names/schemas unchanged (only 2 optional fields appended)
- ✅ D3 strict-priority preserves R6
- ✅ /calculate admin gate consistent with R6 strict behavior

## Test coverage delta

- 23 new unit tests (4 admin_key + 4 documents_repo + 4 trace + 5 calculate + 6 fallback)
- 7 new golden cases (existing 13 → 20)
- 2 new integration tests (parser→/calculate, IR consistency)
- Coverage path: solver (+3-4%) + audit (+1%) + new files 100% (+2-3%) → 78% → ~85%

## Architecture (positive)

- D1-D7 all internally consistent after A1 + CQ1 + CQ2 resolutions
- D3 strict-priority well-reasoned (R6 dominance over soft fallback)
- §4 data flow splits /trace (read-only, audit log) vs /calculate (write, solver + audit) — clean boundary
- §4 ingestion data flow added per A1 — RAG-side extraction keeps parser/RAG loosely coupled
- 9 vertical slices allow per-slice review gates (subagent-driven-friendly)
- D5 backward compat: optional fields, default values — no API breaking change

## Failure modes flagged

| Mode | Coverage | Error handling | User-visible |
|------|----------|----------------|--------------|
| Old trace_id lineage_snapshot=null | GAP (1 golden case needed) | nullable field | Silent null — document |
| /calculate strict + empty hard | ★★★ | 400 strict_violation | Explicit ✓ |
| ADMIN_KEY unset | ★★★ | 503 + startup warn | Explicit ✓ |
| Duplicate doc_id insert | GAP | Undefined | Implementation-defined |
| scope_filter invalid prefix | GAP | Undefined | TBD issue #5 |
| audit.log seek offset stale | Phase 5.5 F handles | RebuildingRotatingFileHandler | Upstream known |
| DocumentRepo write fail (disk full) | GAP | Undefined | 500 |

**Critical gap**: 1 — old trace lineage_snapshot=null user-visible behavior (golden set needs 1 case).

## TODOS proposed (7 items, all non-blocking)

| Item | When | Priority |
|------|------|----------|
| AuditIndex secondary index for /trace perf | 6B | P3 |
| DocumentRepo batch + tx | 6B | P3 |
| Golden set 7 cases concrete values | 实施期 | - |
| scope_filter prefix match semantics | 实施期 | - |
| DocumentRepo.close() lifespan test | 实施期 | - |
| A1 path E2E | 实施期 | - |
| 0006 migration idempotent re-run test | 实施期 | - |

## Parallelization (3 lanes)

- **Lane A**: D1+#10, #1, #2, #7+#8 — `rag/ekrs_rag/security.py, db/, api/v1/trace.py, observability/` (sequential, shared modules)
- **Lane B**: #3+#4 — `rag/ekrs_rag/api/v1/calculate.py, constraint_engine/solver.py` (independent)
- **Lane C**: #5, #6, handbook sync, tag — `tests/fixtures/, docs/` (after A+B merge)

**Lanes A and B can run in parallel worktrees.** No shared module directories.

## Outside Voice

- codex NOT AVAILABLE
- Claude subagent outside voice skipped (spec is mechanical spec closure, no strategic blind spots)
- Cross-model tension: N/A

## Status

- 1 P1 fixed (A1) by user decision
- 0 P1 remaining
- 4 P2 fixed (A2, CQ1) or carve-out (CQ2 by user)
- 4 P3 (CQ3, CQ4, A5, A6) deferred to implementation
- 12 total issues, 0 unresolved decisions
- 1 critical gap (old trace lineage_snapshot=null behavior) — to address in 实施期 via 1 golden case

## Recommended actions

1. **Approve spec for planning** — no blockers
2. Implementation plan (writing-plans skill) should:
   - Add 1 golden case for old trace lineage_snapshot=null
   - Resolve scope_filter semantics in #2 task brief
   - Confirm dev_ui compat for shared/ekrs_shared/audit.py change
   - Add lifespan tests for 0006 migration + DocumentRepo close()

## Completion Summary

- Step 0: scope accepted
- Architecture: 6 issues (1 P1 fixed, 1 P2 fixed, 4 deferred)
- Code Quality: 4 issues (1 P2 fixed, 1 carve-out, 2 deferred)
- Test: 9 gaps (all P3, deferred to implementation or issue resolution)
- Performance: 2 issues (P3, deferred to 6B)
- NOT in scope: written (6 items deferred to 6B)
- What already exists: written (6 components reused)
- TODOS: 7 proposed (all non-blocking)
- Failure modes: 1 critical gap (golden case for null lineage_snapshot)
- Outside voice: skipped (codex unavailable, scope mechanical)
- Parallelization: 3 lanes (A+B parallel, C after)
- Lake score: 8/10 — complete version chosen throughout
