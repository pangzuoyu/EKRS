# Phase 6C — Scope

> Status: planning
> Date: 2026-07-21
> Author: Claude (Sonnet)
> Predecessor: Phase 6A (`f92b724`) + 6A.5 (`e7e25f1`)

---

## Discovery

Before defining new scope: **Phase 6C T8 already shipped** at commit `d21e6d4` (2026-07-16).

```
d21e6d4 fix(6C): qdrant_write_failed audit emit + non-fatal Qdrant init (T8 fixes)
```

Covers:
- `qdrant_write_failed` audit emit on 4 Qdrant ops (`read`/`write`/`delete` — schema broadened in Phase 6B, code emits now)
- Non-fatal Qdrant init in `main.py` lifespan (regression fix for `test_metrics_exporter.py`)
- Tests in `rag/tests/unit/test_qdrant_client.py:391-498`

Followed by `05b6572 chore(sdd): restore .gitignore allowlist + log Phase 6C-minor closure` — Phase 6C-minor was logged closed.

So **`qdrant_write_failed` is no longer a gap**. The unresolved.md item 8 is stale; close it.

---

## Remaining candidates

### Candidate C1 — Pre-existing mypy cleanup (29 errors)

After Phase 6A.5 (`e7e25f1`): 29 errors remain across 8 files. Breakdown:

| File | Errors | Pattern | Risk |
|------|--------|---------|------|
| `qdrant_client.py` | 4 | `dict[Any, Any]` variance, `Optional` narrowing | low (annotation only) |
| `constraint_engine/parser.py` | 2 | Optional narrowing | low |
| `constraint_engine/solver.py` | 2 | `dict[Unknown]` + Condition narrowing | low |
| `constraint_engine/evidence_builder.py` | 2 | Literal narrowing | medium |
| `routes/constraints.py:191` | 1 | `retrieval_result` redefinition | low (rename) |
| `routes/calculate.py` | ? | ConstraintV2 invariance | low |
| `routes/trace.py` | ? | object narrowing | low |
| `storage/` | ? | aiosqlite row types | low |

TDD-able, mechanical, low-risk. Split into T1 (annotations only, no behavior change) + T2 (the `retrieval_result` rename).

### Candidate C2 — T14 manual smoke

`f92b724` Critical bug fix (emit_event → write) was caught by static analysis before manual smoke would have run. Live infra (Qdrant + Redis + uvicorn) needed; ~15 min. Regression confirmation only, not bug discovery.

Should it ship in Phase 6C, or stay deferred? **Decision needed.**

### Candidate C3 — TDD fixture convention

Root cause of bug-135 (`emit_event` → `write`): every fixture injects `audit_writer=None`, so emit branches had 0% coverage. Fix at `f92b724` added `AuditEmitter(Protocol)` annotation to catch future method renames — but the deeper problem (fixture-coverage of injected production-critical deps) is unaddressed.

Possible conventions:
- (a) **Lint rule**: flag `if X is None` guards around injected dependencies that have a production path
- (b) **Test helper**: factory that injects a real writer unless explicitly opted out
- (c) **Convention doc**: in CLAUDE.md / TESTING.md, require real-instance injection for production-critical deps

Lowest cost: (c). Highest leverage: (a). Recommend (c) + (a).

### Candidate C4 — `routes/constraints.py:191` redefinition

`retrieval_result: RetrievalResult = ...` on line 145 (replay branch) and line 191 (main branch) — different code paths but mypy can't tell. 1-line rename to `replay_retrieval_result` on line 145. Could fold into C1 T1.

### Candidate C5 — Admin cleanup (deferred)

- `.superpowers/ssd/progress.md` stale 151-line edit (other agent's)
- 23 SDD `review-*.diff` files (auto-generated, untracked)
- 2 review docs (`*-review.md`, `*-unresolved.md`) — these DID land at `54515c6` (auto-commit)
- `.superpowers/ssd/.gitignore` already restored at `c589a14`

Out of product scope. Admin task.

---

## Recommended Phase 6C structure

| Task | Title | Effort | Risk | Ship-blocking? |
|------|-------|--------|------|----------------|
| T1 | Pre-existing mypy cleanup — annotations only | small | low | yes |
| T2 | `constraints.py:191` rename (folds into T1) | trivial | none | yes |
| T3 | TDD fixture convention doc | small | none | no (process) |
| T4 | T14 manual smoke | small | none | no (regression) |
| T5 | Admin cleanup of stale SDD files | trivial | low | no |

**Skip** (already shipped at `d21e6d4`): `qdrant_write_failed` emit — close as done.

**Drop** from Phase 6C: scope creep into Phase 7 (storage optimization, embedding-cache, etc.) — those need their own phase planning.

---

## Out of scope (defer to Phase 7+)

- Embedding cache / LRU eviction policy
- Qdrant index optimization (HNSW params, quantization)
- Multi-region / replication
- Production hardening (rate limiting, authn)
- Cross-system Phase 6A follow-up coordination (`EKRS-RAG-AI_intergration.md` items beyond 6A scope)

---

## Dependencies

- C1 T1 must precede any work that introduces new type-narrowed APIs (no current pending work)
- C3 should land **before** next phase starts (preventive)
- C4 depends on C1 T1 (logical grouping)

---

## Decision points for user

1. **C2 (T14 manual smoke)**: include in Phase 6C or keep deferred? Needs live infra.
2. **C3 (TDD fixture convention)**: (a) lint / (b) helper / (c) doc / (a+c)?
3. **C5 (admin cleanup)**: include in Phase 6C or separate?

---

## Open questions

- Are there unstated Phase 6C candidates in `ekrs-handbook.md` §6 development phases that haven't been surfaced? (next phase there is undefined per unresolved item 9)
- Should the `Phase 6A.5 → Phase 6C → Phase 7` boundary be a hard reset (rebase) or additive (continue forward)?