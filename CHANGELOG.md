# Changelog

All notable changes to EKRS are documented here by release tag. The
canonical implementation timeline lives in `ekrs-handbook.md §6`; this
changelog focuses on **what was delivered per phase tag** so the diff
from the previous phase is readable without consulting the handbook.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) —
`Added`, `Changed`, `Fixed`, `Removed` per release.

## [phase7] - 2026-07-23

**Tag moved**: `phase7` f50b5e9 (T1) → 99c77f5 (T6 / Phase 7 closure).
`phase7` represents *delivered state*, not *snapshot time* — see
Phase 7 plan doc §"Decisions (locked 2026-07-23)" row #3.
`phase7.1` remains at 41c2d54 (T2 closure, historical anchor).

### Added

- **`qdrant_write_failed` audit pipeline** (T1, commit `f50b5e9`):
  Integration test exercising real `AuditWriter` + `AuditIndex` +
  Qdrant unreachable (port 1) to verify the event emits end-to-end.
  9 cases pass + 1 heavy skip. CI runs default job; nightly runs heavy.
- **Audit event emissions** (T2, commit `41c2d54`): 8 schema-registered
  events written at all required sites (was 0/8 → 8/8). Closes the
  Phase 6C T8 review finding (D7 emit gap).
- **CompensationHandler real retry** (T3, commits `6d5c054` + `57b3b3c`):
  Handler returns `bool`; `IngestionPipeline.reparse()` runs the
  universal re-ingest. `compensation_retry` schema gains required
  fields `reingest_outcome` (`"success"`|`"failed"`|`"duplicate"`|
  `"skipped"`) + `reingest_duration_ms` (`int`). Closes the Phase 4
  "black box" gap — orphan PENDING/RUNNING tasks now auto-recover
  instead of accumulating in aiosqlite.
- **FastAPI `/docs` + `/redoc`** (T4, commit `7e3d46d`): `docs_url`,
  `redoc_url`, `openapi_tags` (5 tags) enabled on `create_app()`.
  Operators can browse the API surface.
- **Streamlit `dev_ui`** (T5, commit `79b04fc`): 3-tab dev UI at
  `dev_ui/app.py` — 文档入库 (ingest trigger + status), 约束查询
  (POST /v1/constraints with multi-branch display), 黄金集验证
  (golden set regression). Dev-only extra (`rag[dev]`); not in
  production Docker images. Replaces the `/dev-ui` HTTP route that
  was referenced in `CLAUDE.md` but never built.
- **Embedding LRU+TTL cache** (T7, commit `b8ff559`):
  In-process cache keyed on `sha256(text) | model_version` where
  `model_version` is the joined SHA256 prefixes of `model.onnx` +
  `sparse_linear.pt`. Cache misses invoke the model; hits return
  immediately. Defaults: 10k entries / 24h TTL.
- **`POST /v1/admin/embedding-cache/flush`** (T7): X-Admin-Key gated;
  returns `{cleared, model_version, cache_size_after}`. 503 if
  EmbeddingService is not initialized.
- **Handbook §6 timeline** (T6, commit `99c77f5`): Phase 6B / 6C / 7
  rows added; §6.1 freezes Phase 6+ deferral list (5 categories).
- **Phase 7 plan doc** (`docs/superpowers/plans/2026-07-23-phase7-scope.md`):
  captures the scope + 5 locked decisions; closed.

### Changed

- **`compensation_retry` audit schema** (T3): adds two required fields
  (`reingest_outcome`, `reingest_duration_ms`). Old entries without
  them read defensively (default `outcome=None`, `duration_ms=0`).
- **`EmbeddingService.encode()`** (T7): splits inputs into
  cached/missing before calling the model. Single batched call per
  `encode()`; behavior unchanged from the caller's perspective.

### Follow-ups shipped in this range but not Phase 7 tasks

These four commits landed between T2 and T4 and are acknowledged in
the `phase7` tag, but they are **not** re-tagged into `phase7.1`:

- `57187d3` FlagEmbedding → onnxruntime + transformers bge-m3 loader.
- `afbf4a6` 4 audit-emission gap fixes from Phase 7 review.
- `419006d` pseudo-sparse recall@K eval script.
- `cda45fe` BAAI learned sparse head via `sparse_linear.pt`.

Plus two pre-Phase-7 maintenance commits that happen to fall in the
`phase7` range (not Phase 7 work; documented here for completeness):

- `95475b4` constraint_engine mypy cleanup.
- `991814c` QDRANT_PORT for REST client + runbook port clarification.

## [phase6c-minor] - 2026-07-15

Tag: `phase6c-minor` → `7a87ce0`.

- Three Phase 6C T8 Minor cleanups: `delete_old_versions` filter
  shape (`Range(lt=keep_version)`), narrow exception types, pip
  dependency consolidation. See `phase6c-closure` for the broader
  T1+T2 mypy + T3 fixture doc + T4 smoke runbook + T5 sdd cleanup.

## [phase6c-closure] - 2026-07-22

Tag: `phase6c-closure` → `280ce4f`. Phase 6C retrofitted 5 leftover
items from Phase 6A T14 review (mypy clean across 49 rag/ files,
TDD fixture convention, manual smoke runbook executed via
`docker.m.daocloud.io` mirror, admin cleanup shrinking
`.superpowers/sdd/` from 39 MB to 3.4 MB). 601 passed, 3 skipped
at closure.

## [phase6c-audit-emit] - 2026-07-19

Tag: `phase6c-audit-emit` → `d21e6d4`. `qdrant_write_failed` audit
emit + non-fatal Qdrant init (T8 fixes the Phase 6B D7 review
finding).

## [phase6b-retrieval-layer] - 2026-04-XX

Tag: `phase6b-retrieval-layer` → `bd00849`. Embedding migration
from bge-small-en (384d) to bge-m3 (1024d + sparse). QdrantManager
rewrite fixed 3 production bugs. `AUTO_REINDEX` auto-rebuilds the
collection on dim mismatch. Heavy integration tests run on nightly
CI only (the bge-m3 ONNX model is vendored but not loaded by default
runners).

## [phase6a-spec-closure] - 2026-04-XX

Tag: `phase6a-spec-closure` → `c7f1138`. 9 vertical slices closing
the spec gaps from Phase 5.5: X-Admin-Key, DocumentRepo/A1, /trace,
/calculate, soft fallback, golden set 13 → 42 cases, audit 2
optional fields, ENGINE_URL, 85% CI gate. 531 tests pass,
86.63% coverage, CI gate green at closure.

---

## Cross-references

- **Implementation timeline**: `ekrs-handbook.md` §6
- **Phase 7 scope + decisions**: `docs/superpowers/plans/2026-07-23-phase7-scope.md`
- **Tag force-move rationale** (Decision §3): phase 7 plan doc row #3
- **Deferral freeze**: `ekrs-handbook.md` §6.1