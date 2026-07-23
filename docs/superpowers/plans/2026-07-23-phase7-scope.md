# Phase 7 — Scope

> Status: planning
> Date: 2026-07-23
> Author: Claude (Sonnet)
> Predecessor: Phase 6C (`phase6c-closure` tag) + Phase 7 T1/T2 already shipped

---

## Discovery

Phase 7 already has 2 of 7 tasks shipped:

```
f50b5e9 test(integration): qdrant_write_failed end-to-end audit pipeline (Phase 7 T1)
41c2d54 feat(audit): emit 8 schema-registered events (Phase 7 T2)
```

Tags `phase7` (at T1) and `phase7.1` (at T2) already exist on origin.

Since `phase7.1` (T2), 4 follow-up commits landed:
- `57187d3` feat(embedding): replace FlagEmbedding with vanilla onnxruntime bge-m3 loader
- `afbf4a6` fix(observability): resolve 4 Phase 7 audit-emission gaps
- `419006d` test(eval): pseudo-sparse recall@K eval script (Phase 7 T2 follow-up)
- `cda45fe` feat(embedding): integrate BAAI learned sparse head via sparse_linear.pt

These should be folded into the `phase7.1` closure or acknowledged in `phase7` tag.

---

## Remaining candidates

### Candidate C1 — CompensationHandler real retry (high value)

`rag/ekrs_rag/main.py:41` — `COMPENSATION_HANDLER_IMPLEMENTED = False`.
`main.py:44-47` — `_stub_compensation_handler` only logs a warning; never re-triggers ingestion.
`main.py:46` — explicit `TODO: wire to IngestionPipeline.ingest via callback_url (Task 7)`.

**Impact today:**
- `compensation_retry` audit events fire correctly (closed in T2).
- BUT the handler is a no-op → orphan PENDING/RUNNING tasks accumulate in aiosqlite.
- Operator must manually delete or re-submit via `/v1/ingestion/replay`.
- The whole point of `CompensationScanner` (Phase 4) is undermined by the unwired handler.

**Effort:** medium (~200 LOC + tests). Need to:
1. Refactor `IngestionPipeline` so compensation handler can call `ingest()` with the original notification payload (currently `ingest()` requires `IngestionNotification`, but compensation has only the task repo row).
2. Add `task.source_payload` column (or re-derive from `source_path` JSONL re-parse).
3. Pass `handler` as `pipeline.ingest` callable rather than just emitting `compensation_retry`.
4. Tests: integration test that simulates stuck task → handler re-ingests → status flips to COMPLETED.

**Risk:** medium. Re-running ingest has side effects (Redis lock, Qdrant upsert, callback). Must preserve idempotency.

### Candidate C2 — FastAPI `/docs` enablement (trivial)

`main.py` `create_app()` constructs `FastAPI(...)` without `docs_url` / `redoc_url` (defaults disabled).
Currently only `/health` and `/healthz` are HTTP-visible — operators can't browse the API surface.

**Effort:** trivial (~10 LOC).
```python
app = FastAPI(
    title="EKRS RAG Service",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "ingestion", "description": "Parser callback + replay"},
        {"name": "constraints", "description": "Constraint solving API"},
        {"name": "calculate", "description": "Numerical calc endpoint"},
        {"name": "trace", "description": "Trace replay endpoint"},
        {"name": "admin", "description": "Operator recovery (X-Admin-Key)"},
    ],
)
```

**Risk:** none. Pure documentation.

### Candidate C3 — `dev_ui/` Streamlit skeleton (large, but longstanding placeholder)

`dev_ui/` has only `.gitkeep`. Handbook:419 marks it "Streamlit 调试界面（占位，Phase 6 实施）" — but Phase 6 came and went without implementation.

Three planned tabs (per handbook:397):
- **文档入库** — trigger mock notification, view task repo status
- **约束查询** — POST /v1/constraints against the live API, see multi-branch output
- **黄金集验证** — run `tests/golden_set/golden_set.json` against live API, report pass/fail

**Effort:** large (~400-600 LOC across `dev_ui/app.py` + helpers).
- Install `streamlit>=1.30` in `dev_ui/pyproject.toml` (separate from `rag/`).
- Wire to RAG API via `httpx` (already a transitive dep).
- Stub the golden-set tab with a `Pass/Fail/Skip` panel against the existing JSON.

**Risk:** low. Dev-only — gated by `EKRS_DEBUG=true` like the existing `/dev-ui` route. (Note: `/dev-ui` HTTP route also doesn't exist; see C2.)

### Candidate C4 — Handbook §6 add Phase 7 entry (small, process)

`ekrs-handbook.md` §6 lists Phases 1-5 + retrofits 5.5 D/E/F; CLAUDE.md:95 says "Next scope (Phase 6) not yet defined in `ekrs-handbook.md`."

This is symptomatic — Phase 6 was never added either, only retro-fitted (6A/6B/6C). Phase 7 plan needs an entry here.

**Effort:** small (~30 LOC edit). Append §6 Phase 7 subsection + freeze Phase 6+ deferral list.

**Risk:** none. Documentation only.

### Candidate C5 — Embedding cache / LRU (deferred from Phase 6C)

From Phase 6C scope doc §"Out of scope":
> Embedding cache / LRU eviction policy

`EmbeddingService.encode()` re-encodes every chunk on every ingest. With 30 chunks × N re-ingestions, this is wasted CPU. Cache: `dict[(text_hash, model_version) → EncodedVector]` with LRU cap (e.g., 10k entries).

**Effort:** small (~80 LOC + tests).
**Risk:** medium — staleness on model reload. Mitigation: include `model_sha256` (from `bge-m3.sha256`) in cache key; invalidate on mismatch.

---

## Recommended Phase 7 structure

| Task | Title | Effort | Risk | Dependencies |
|------|-------|--------|------|--------------|
| T3 | CompensationHandler real retry (closes Phase 4 gap) | medium | medium | none |
| T4 | FastAPI `/docs` + `/redoc` + openapi_tags | trivial | none | none |
| T5 | `dev_ui/` Streamlit skeleton (3 tabs) | large | low | none |
| T6 | Handbook §6 add Phase 7 + freeze deferral list | small | none | none |
| T7 | Embedding LRU cache (deferred from 6C) | small | medium | none |

**Already shipped** (fold into `phase7` tag):
- T1 (`f50b5e9`) — qdrant_write_failed 集成测试
- T2 (`41c2d54`) — emit 8 audit events

**T2 follow-ups** (acknowledge in `phase7` tag, do NOT re-tag):
- `57187d3` FlagEmbedding → onnxruntime
- `afbf4a6` 4 audit-emission gap fixes
- `419006d` pseudo-sparse recall eval
- `cda45fe` BAAI learned sparse head

---

## Out of scope (defer to Phase 8+)

From Phase 6C deferral list (still deferred):
- Qdrant index optimization (HNSW params, scalar/int8 quantization) — speculative without load profile
- Multi-region / replication
- Production hardening (rate limiting via SlowAPI, service-to-service authn)

New deferrals identified in audit:
- `/dev-ui` HTTP debug route (was referenced in `CLAUDE.md` "Current State" but never implemented; superseded by Streamlit `dev_ui/`)
- Module-level `_qdrant`/`_pipeline` global setters removed in Phase 5.5 E — back-compat with any future ones

---

## Dependencies

- T3 + T7 can run in parallel.
- T4 is independent (FastAPI-only change).
- T5 + T6 are independent of others but T6 should ship *after* T3/T5 names are stable (so handbook reflects what was actually built).
- T6 is the last task; it freezes the scope.

---

## Decisions (locked 2026-07-23)

| # | Item | Decision | Reason |
|---|------|----------|--------|
| 1 | **T3 handler re-ingest mechanism** | (c) **Universal re-run** via `ekrs reparse --doc-id <ID> [--force]` CLI | Re-runs full workflow (JSONL → IR → chunks → embeddings → Qdrant) with reconstructed notification. `--force` bypasses content_hash check; default skip when hash matches. Preserves idempotency contract; no fabricated notifications; auditable via existing `pipeline.ingest()` audit trail. |
| 2 | **T5 Streamlit stack** | (b) **Folded into `rag/pyproject.toml` as dev-only extra** | Streamlit is dev/debug tooling — not production. Adding to `[project.optional-dependencies] dev = [...]` (PEP 621, not Poetry `[tool.poetry.extras]` — different syntax for the same intent) keeps production Docker images slim. `dev_ui/` shares rag's models + DB without cross-project config. |
| 3 | **Tag strategy** | (c) **Force-move `phase7` to current HEAD** | `phase7` represents *delivered state*, not *snapshot time*. `git tag -f phase7 HEAD && git push --tags -f`. Document the move in `CHANGELOG.md` with date + feature list so the diff from `phase6` is readable. Keep `phase7.1` as a historical anchor for T2 closure. |
| 4 | **T7 cache invalidation** | **(a+b)**: `bge-m3.sha256` auto-check on startup + LRU TTL=24h + `POST /v1/admin/embedding-cache/flush` manual endpoint | Auto-check prevents silent stale-cache after model swap; 24h TTL bounds staleness regardless. Manual endpoint provides ops escape hatch without service restart. Cache flush is invisible to Qdrant / RAG — only affects subsequent `EmbeddingService.encode()` calls. TTL configurable via `settings`. |
| 5 | **`compensation_retry` schema** | **Add `reingest_outcome: "success"\|"failed"\|"duplicate"\|"skipped"` + `reingest_duration_ms: int` as REQUIRED fields** | Closes the T3 "black box" risk: ops needs to verify compensation actually succeeded, and detect performance regressions via duration. Old audit log entries without these fields are read-defensively (default `outcome=None`, `duration_ms=0`). Schema change ships WITH T3 — not before, or current emissions will fail validation. |

### Resolved questions

- **Was Task 7 ever written as a spec?** ❌ No. `main.py:46` references "Task 7" but no document exists under `docs/superpowers/plans/`. The reference is purely internal/legacy.
  - ✅ **Spec written 2026-07-23**: [`docs/superpowers/plans/2026-07-23-task7-compensation-retry.md`](2026-07-23-task7-compensation-retry.md) (TDD test list + design + files touched). T3 implementation follows this spec.