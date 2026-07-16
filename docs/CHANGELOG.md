# Changelog

> Phase-by-phase summary. Authoritative commit history is `git log`; this file explains the **shape** of each shipped phase for external collaborators.

Format inspired by [Keep a Changelog](https://keepachangelog.com). Versions here are **phase identifiers**, not SemVer.

---

## [phase6c-minor] — 2026-07-16

Tag: `phase6c-minor` @ `e320717`

Three T8 Minor cleanups from Phase 6B's final review:

**Fixed**

- `QdrantManager.delete_old_versions` — pre-existing Phase 6A bug: filter selected `version == keep_version` (kept the version it claimed to delete). Now uses `must=[doc_hash]` + `must_not=[version=keep_version]`. Test added.
- `QdrantManager.ensure_collection` inner `except Exception` narrowed to `(UnexpectedResponse, ApiException, ValueError, KeyError, AttributeError)` — avoids swallowing programming errors. Outer `except Exception` (the `qdrant_write_failed` audit emit point) intentionally kept broad to preserve observability for transient network errors.
- `.github/workflows/heavy-tests.yml` — two `pip install` steps merged into one.

**Skipped** — orphan `rag/models/bge-m3/Constant_7_attr__value` kept per T1 upstream snapshot fidelity decision.

**Stats:** 545 tests passing · coverage 86.75% · +50 LOC net.

---

## [phase6c-audit-emit] — 2026-07-16

Tag: `phase6c-audit-emit` @ `c61e982`

Closes the Important gap left by Phase 6B T8 final review:

- `qdrant_write_failed` audit event — schema registered but no code emitted. Added helper `_emit_qdrant_failure(operation, collection, exc)` wrapping `ensure_collection` / `upsert_chunks` (excluding `EmbeddingUnavailableError`) / `search` / `delete_old_versions` in try/except + `writer.write(...)`.
- `main.py` lifespan init made non-fatal for Qdrant/Embedding. Routes depending on Qdrant already return 503 via `get_retriever()` dependency helpers. Pre-existing 5 `test_metrics_exporter` failures fixed as a side effect (lifespan no longer re-raises on Qdrant init failure).

**Stats:** 544 tests passing · coverage 86.74% · +349/-142 net.

---

## [phase6b-retrieval-layer] — 2026-07-15

Tag: `phase6b-retrieval-layer` @ `f692c42`

Embedding migration from bge-small-en-v1.5 (384d) to BAAI/bge-m3 (1024d dense + sparse via BGE-M3 ONNX):

**Production bug fixes** (QdrantManager rewrite):

- B1: `search()` uses `query_points()` with hybrid `Prefetch` (dense + sparse) + `FusionQuery(RRF)` on qdrant-client 1.17.1. Old `search()` was completely broken on the new client API.
- B2: `ensure_collection` reads `config.params.vectors["dense"].size` (1.17.1 path).
- B3: `upsert_chunks` uses `EmbeddingService.encode(...)` for real dense+sparse vectors. Old code wrote zeros.

**New components**:

- `EmbeddingService` facade — wraps `FlagEmbedding==1.2.13` + ONNX runtime, exposes `encode()` + `to_qdrant_sparse()`. Dummy-mode fallback when model files missing.
- `QdrantManager` — constructor takes `EmbeddingService`. `auto_reindex` flag controls dim-migration behavior (Phase 6B D4).
- Vendor `rag/models/bge-m3/` — bge-m3 ONNX model + SHA256 manifest.

**Tests:**

- 14 unit tests for new QdrantManager behavior.
- Heavy integration tests (real bge-m3 model load), marked `@pytest.mark.heavy` — run in nightly CI on Python 3.11.
- `AUTO_REINDEX` env controls dim migration; `asyncio.to_thread` for blocking `ensure_collection` call.

**Audit changes**:

- `qdrant_write_failed` semantic broadened (16-event registry unchanged). New `operation: str` field distinguishes read | write | delete. Back-compat note in handbook §16 for pre-6B events that lack the field.

**Lifespan:**

- `ensure_collection` runs at startup before serve, with `AUTO_REINDEX=true` default for dev.

---

## [phase6a-spec-closure] — 2026-07-14

Tag: `phase6a-spec-closure`

9 vertical slices to close spec gaps from the original Phase 1-5 implementation:

1. `X-Admin-Key` authentication header (handbook §16).
2. `DocumentRepo` / SQLite metadata store (separate `documents.db`; not the task repo).
3. `POST /v1/calculate` route (constraint calculation proxy).
4. `POST /v1/constraints/trace` and `/v1/trace/query_history` routes.
5. Soft-fallback in retrieval (graceful degradation when Qdrant unhealthy).
6. Golden test set expanded 13 → 42 cases (28 new cases per `ekrs-handbook.md` §9.1).
7. Audit schema: 2 optional fields (`lineage_snapshot`, `conflict_details`) added without schema change; passed via `_PHASE6A_OPTIONAL` spread in shared audit base.
8. New audit event `document_metadata_failed` (registered → 15 → 16 events total).
9. `ENGINE_URL` parser callback env var (Phase 4 ENGINE_URL rollout).

**Stats:** 531 tests passing · coverage 86.63% · CI gate green (≥85% required).

---

## [phase5.5-f-audit-rotation] — 2026-07-08

`audit.log` rotation 100 MB × 5 gzip backups via `RebuildingRotatingFileHandler`. `/healthz` audit suppression via `ContextVar` skip flag. On-rollover callback rebuilds `AuditIndex` so replay offsets stay valid.

---

## [phase5.5-e-retriever-depends] — 2026-07-05

Module globals (`_qdrant`, `_pipeline`, `_retriever`, `_audit_writer`, `_audit_index`, `_task_repo`, `_doc_repo`) replaced with FastAPI `Depends` migration: `get_retriever`, `get_audit_index`, `get_pipeline`, `get_redis_lock`, `get_task_repo`. Five setters removed.

---

## [phase5.5-d-metrics-exporter] — 2026-07-01

`/metrics` route removed from main app (kept on uvicorn-mounted main port); replaced with sidecar exporter on `:9090` via `prometheus_client` multiproc mode. `deployment/docker-compose.yml` gained a `prometheus` service that scrapes the sidecar.

---

## [phase5-observability] — 2026-06-25

Audit infrastructure (`AuditLogger` base + `AuditWriter` + `AuditIndex`), Prometheus metrics (route counters, latency histograms, failure counts), `@audited` and `@metered` decorators, query and ingestion replay modes, debug.log rotation, `/healthz` JSON readiness endpoint.

---

## Iron Rules (invariants preserved across all phases)

From `ekrs-handbook.md`. Review at every phase boundary, never relaxed:

| ID | Rule |
|----|------|
| R1 | Every `numeric_hint` carries `source_span`, `block_id`, `context_window`. |
| R2 | Solver is a pure function — no I/O, no state, no side effects. |
| R3 | Three-gate pipeline: recall → extract → solve; any failure blocks the result. |
| R4 | Context priority: User > Explicit_Doc > Inferred_Doc > Default. |
| R5 | Only entity-overlap scoring for KG; no graph DB, no multi-hop. |
| R6 | `strict=true` forbids inference; missing context returns 400. |
| R7 | Every hint carries `scope_path`; queries can filter by scope. |
| R8 | Index layer only filters illegal status; never trims authority. |

Audit event count is also frozen (16 schemas) — broadening semantics is allowed, adding a new event name is not.

[phase6c-minor]: https://github.com/REPO/compare/phase6c-audit-emit...phase6c-minor
[phase6c-audit-emit]: https://github.com/REPO/compare/phase6b-retrieval-layer...phase6c-audit-emit
[phase6b-retrieval-layer]: https://github.com/REPO/compare/phase6a-spec-closure...phase6b-retrieval-layer
