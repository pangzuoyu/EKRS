# Phase 6A T14 Close-out — Unresolved Issues

> Per project CLAUDE.md: "每个计划结束时，给我列出尚未解决的问题列表"
> Date: 2026-07-21
> Branch state: master @ f92b724

## Deferred during T14

1. **T14 Steps 4-6 (manual round-trip smoke)** — Steps 4-6 require live infra (`docker-compose up` for Qdrant + Redis + uvicorn). The Critical bug `f92b724` was discovered by static analysis (mypy + invariant check) **before** these steps were attempted, so the manual smoke would now serve as regression confirmation rather than bug discovery. **Decision needed**: run smoke now to verify the fix, or defer to Phase 6A.5 cleanup task.

2. **Phase 6A.5 mypy cleanup** — 5 pre-existing mypy errors in route handlers (`routes/ingestion.py`, `routes/constraints.py`, `routes/calculate.py`, `routes/trace.py`, `main.py:303`). False positives caused by `AuditLogger` annotation on `AuditWriter` instances + `object` annotation on `Mapping` types. Out of Phase 6A scope. Tracked as future cleanup task. **Decision needed**: schedule as Phase 6A.5 or defer indefinitely.

## Leftover from earlier sessions (not mine)

3. **Stale worktree** — `.claude/worktrees/agent-a12ee79058e30417f` at commit `97a7c63`. No active agent references it. **Decision needed**: remove or keep.

4. **Stale `.superpowers/sdd/progress.md` working-tree edit (151 lines)** — left over from a different agent's Phase 4/5 SDD session; the file in HEAD still describes Phase 4 + Phase 5 in progress. Not part of Phase 6A scope. The companion `.gitignore` change (8-line deletion removing the `!*.diff`, `!PHASE-*-COMPLETE.md`, `!progress.md`, `!task-8-report.md`, `!.gitignore` exceptions) makes `.superpowers/sdd/` entirely hidden — this could regress SDD tooling if merged. **Decision needed**: revert both, commit both, or leave alone.

5. **Untracked Phase 6A docs (~5000 lines)**:
   - `docs/solutions/integration-issues/ekrs-coordination-response-2026-07-20.md` (392 lines)
   - `docs/superpowers/plans/2026-07-21-ekrs-coordination-response-.md` (2209 lines)
   - `docs/superpowers/plans/2026-07-21-ekrs-integration-fixes.md` (2365 lines)
   
   These are Phase 6A work products (coordination response + the integration-fixes plan that was executed). The trailing `-` in the second filename looks like a typo (empty ID slot). **Decision needed**: commit all 3 (rename the second to drop the trailing `-`?), or prune.

## Process improvements (for next phase)

6. **TDD gap that let `emit_event` through** — The Critical bug existed because every fixture injects `audit_writer=None`, so emit branches had 0% coverage. The type annotation `object | None` made mypy unable to catch method-name typos. **Recommendation**: lint that flags `is None` checks around injected production-critical dependencies (the runtime path should be covered). For now, the `AuditEmitter(Protocol)` annotation in `f92b724` catches future method renames.

7. **Test fixture coupling** — `test_audit_phase6a_fields.py` and `test_pipeline_audit_emit.py` both import `ekrs_rag.main._EVENT_SCHEMAS` (module-private). Acceptable convention but tight coupling; if main.py is reorganized, these tests break. **Recommendation**: expose a public `get_event_schemas()` if reorganization is anticipated.

## Known future work (not blocking)

8. **`qdrant_write_failed` emit gap** — Per memory `phase6b-final-review-finding-d7-emit.md`, the event is registered in `_EVENT_SCHEMAS` and documented in §16, but no code emits it. Deferred to Phase 6C.

9. **Phase 6C scope** — Not yet defined in `ekrs-handbook.md`. Phase 6A → Phase 6A.5 → Phase 6B (retrieval layer, shipped) → ? → Phase 6C.

---

## Decisions requested

- [ ] Run T14 Steps 4-6 manual smoke? (item 1)
- [ ] Schedule Phase 6A.5 mypy cleanup? (item 2)
- [ ] Remove stale worktree? (item 3)
- [ ] Revert stale SDD `.gitignore` and `progress.md` edits? (item 4)
- [ ] Commit 3 untracked `docs/` artifacts? (item 5)