# Changelog

All notable changes to EKRS are documented here by release tag. The
canonical implementation timeline lives in `ekrs-handbook.md §6`; this
changelog focuses on **what was delivered per phase tag** so the diff
from the previous phase is readable without consulting the handbook.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) —
`Added`, `Changed`, `Fixed`, `Removed` per release.

## [phase8] - 2026-07-24

**Tag moved**: `phase8` created at `193b0db` (HEAD at Phase 8 closure).
`phase8` represents *delivered state*, not snapshot time — see Phase 8
plan doc §"Tag strategy" + Phase 7 Decision §3 precedent.
`phase8.1` placed at `7151f13` (T8-3a, bge-m3 vendoring milestone) as
a historical anchor — **do not move**.
`phase7` stays at `99c77f5` and `phase7.1` stays at `41c2d54` (both
unchanged).

12 commits span the gap from `phase7` to `phase8`: 7 task commits
(T8-1..T8-5 + T8-3a baseline-pin sub-commit), 1 cross-phase debt
cleanup (IngestionOutcome Literal widening), and 4 planning/docs
commits that landed between Phase 7 closure and the first Phase 8
task. Listed below by category.

### Added

- **Per-IP rate limiting on `/v1/*`** (T8-1, commit `c9bcd70`): hand-
  rolled sliding-window token bucket (60 req/min default, override
  via `EKRS_RATE_LIMIT`). Exempt routes: `/healthz`, `/health`,
  `/metrics`, `/docs`, `/redoc`, `/openapi.json`. Returns `429` with
  `Retry-After` header. 13 unit tests.
- **Secret rotation SOP + offline validator** (T8-2, commit
  `028b2ed`): `docs/SECRET-ROTATION.md` (zero-downtime procedure for
  `PARSER_TOKEN` + `ADMIN_KEY` via comma-separated token acceptance)
  + `scripts/validate_rotation.py` (typo-grade similarity check,
  LCP ≥ 0.80 rejects). 24 unit tests.
- **bge-m3 ONNX vendored in Docker image** (T8-3a, commit `7151f13`):
  `rag/Dockerfile` builds with the model baked into
  `/opt/ekrs/models/bge-m3`. `embedding_service.py` resolves model
  dir via `EMBEDDING_MODEL_DIR` env var (default = vendored). Build
  context = repo root; ARG-overridable `PYTHON_BASE_IMAGE` + `PIP_INDEX_URL`
  for restricted networks. 4/4 heavy + 21/21 unit tests pass.
- **T8-3a image baseline pinning** (commit `681c253`): `make build-rag-baseline`
  rebuilds the reference image and writes SHA256 manifest at
  `deployment/rag-image.baseline.json`. Idempotent rebuilder script
  + restricted-network ARG overrides.
- **Ingestion smoke canary** (T8-3b, commit `6f4d9eb`):
  `scripts/smoke_ingestion.sh` (7-step bash wrapper) +
  `scripts/lib_smoke.py` (pure-stdlib helpers). Exits non-zero on
  any of 5 contract violations: preflight, notify, status, audit
  (`qdrant_write_failed`), callback. 19 unit tests + 484/1 suite.
  Used post-deploy, not in PR CI.
- **Golden set extension 42 → 50** (T8-4, commit `5a11824`): 5
  chunk-level cases in `golden_set.json` (cryogenic Kelvin, scope
  priority, % elongation, multi-condition T+P, strict-mode happy
  path) + 3 API-level cases in new `test_api_validation.py`
  (TestClient + `dependency_overrides` pattern: empty query 4xx,
  invalid scope 4xx, concurrent replay deterministic). Handbook
  §9.1 grew 8 TC-* rows + implementation-location note. 191 golden
  entries pass; 675 unit + golden suite pass.
- **Chunker perf baseline at 10k docs** (T8-5, commit `763535b`):
  `benchmarks/test_chunker_10k.py` (`@pytest.mark.heavy`, excluded
  from PR CI). Runs deterministic synthetic corpus (seed=42,
  mean 20 blocks/doc) through `chunk_blocks()`, reports p50/p95/p99
  per-doc latency + chunks/sec + peak RSS. Writes atomic JSON to
  `benchmarks/results/chunker-10k-<ts>.json`. p99 default threshold
  5.0s/document (env-var tunable).

### Fixed

- **`IngestionOutcome.rag_status` Literal widened** (commit `193b0db`):
  Phase 7 T3's `reparse()` added `"duplicate"` (SHA256 idempotent
  skip) and `"business_failure"` (ops-level error), but the type
  annotation was still `Literal["success", "failed"]`. Three pre-
  existing mypy errors at pipeline.py:303/317/340 resolved. Single
  source of truth via `_VALID_STATUSES` tuple shared between
  annotation + `__post_init__` validator. +2 outcome tests.

### Planning / docs (in the `phase8` range, not Phase 8 *tasks*)

- `435ae58`: docs — split deferral list (Phase 6+ frozen §6.1 vs
  Post-deploy registry §6.2).
- `adbb942`: Phase 8 scope doc — 5 deployment-readiness tasks + 3
  locked decisions.
- `097adeb`: Phase 8 acceptance gates tightened per Step 0 review.
- `ad8c21e`: Phase 7 CHANGELOG entry + Phase 7 plan doc closing
  (this commit predates Phase 8 by 8 hours but lands in the `phase8`
  range because `phase7` had already been force-moved to `99c77f5`
  before it).

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