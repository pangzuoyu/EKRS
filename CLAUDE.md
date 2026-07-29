# CLAUDE.md

Engineering Knowledge Recovery System (EKRS) — extracts structured engineering constraints (temperature, pressure, material limits) from unstructured documents (PDF/Word/DWG), computes parameter feasible ranges via a deterministic solver, and provides scope-aware conflict detection. Full specification: `ekrs-handbook.md`.

## Quick Commands

```bash
make install      # Install dependencies (shared + rag)
make dev          # Start docker-compose + uvicorn + streamlit
make dev-down     # Stop docker
make test         # pytest rag/tests/ -v --tb=short
make test-cov     # With coverage report
make lint         # flake8 + mypy on shared/ and rag/
make mock-notify  # Simulate parser notification for testing

# Run single test
cd rag && pytest tests/unit/test_solver.py -k "test_name" -v

# Run RAG service locally (without Docker)
make run-local
```

## Architecture

Monorepo with three deployable units:

```
shared/ekrs_shared/   → Pydantic models, normalizer (affine temp conversion), audit base
rag/ekrs_rag/         → FastAPI service: ingestion, retrieval (Qdrant), constraint solving
dev_ui/               → Streamlit debug UI (dev only)
deployment/           → docker-compose, k8s manifests
```

Data flow: Parser (external) → `POST /v1/ingestion/notify` → RAG reads JSONL, vectorizes into Qdrant → callback to Parser. Queries: `POST /v1/constraints` → semantic retrieval → NumericHint extraction → interval solver → structured result.

## Seven Iron Rules (must never be violated)

| ID | Rule | Enforcement |
|----|------|-------------|
| R1 | Every numeric_hint must have source_span, block_id, context_window | Validate on ingestion |
| R2 | Solver is a pure function — no I/O, no state, no side effects | Unit test determinism |
| R3 | Three-gate pipeline: recall → extract → solve; any failure blocks the result | Golden set tests |
| R4 | Context priority: User > Explicit_Doc > Inferred_Doc > Default | Show source in output |
| R5 | Only entity-overlap scoring for KG — no graph DB, no multi-hop | No graph DB dependency |
| R6 | strict=true forbids inference; missing context returns 400 | API test |
| R7 | Every hint carries scope_path; queries can filter by scope | Multi-branch tests |
| R8 | Index layer only filters illegal status; never trims authority | Qdrant payload check |

## Key Dependencies

- **Python 3.11+**, FastAPI 0.115, Pydantic 2.8, Qdrant client 1.11
- **portion** — interval arithmetic library (critical for solver, uses factory functions NOT `Interval(left=, right=)` kwargs)
- **bge-m3** ONNX for embeddings (dense 1024d + sparse)
- **Redis** for distributed locks and replay cache
- **aiosqlite** for task state

## Environment Variables

Minimal set in `.env.example`:
- `PARSER_TOKEN` — shared secret for parser↔RAG auth (≥32 chars)
- `SHARED_STORAGE_PATH` — where parser writes JSONL, RAG reads
- `EKRS_DEBUG` — enables debug UI at `/dev-ui` and verbose logging
- `QDRANT_HOST`, `QDRANT_GRPC_PORT`, `REDIS_URL`

## Code Conventions

- All logs: structured JSON via `python-json-logger` (spec §12)
- Audit log (`audit.log`): permanent, size-bounded by rotation (100MB × 5 gzip), records every solve with evidence
- Debug log: only when `EKRS_DEBUG=true`, rotatable, max 100MB x 5 backups
- `shared/` installed as editable dep from both `rag/` and `dev_ui/`

## Current State (as of 2026-07-29)

Phases 1-9 complete; Phase 10 in progress.

- **Phase 1 — Foundation**: shared/ekrs_shared/ (Pydantic models, normalizer, audit base); rag/ekrs_rag/ingestion/ (IR parser, scope-aware chunker, pipeline); rag/ekrs_rag/retrieval/ (Qdrant client); notify/status routes
- **Phase 2 — Solver core (V2)**: hint extractor, evidence builder, interval solver (`portion`), context manager, IR V2 multi-branch, golden set
- **Phase 3 — Scope-aware retrieval**: scope-priority composite scoring, multi-branch output (high-temp / general-condition branches)
- **Phase 4 — System integration**: callback idempotency, TaskRepo (aiosqlite), RedisLock, CompensationScanner, main.py lifespan wiring
- **Phase 5 — Observability**: AuditLogger base + AuditWriter + AuditIndex, Prometheus metrics (route counters, latency, failures), @audited / @metered decorators, query & ingestion replay, debug.log rotation, /healthz JSON endpoint
- **Phase 5.5 D** — `/metrics` sidecar exporter (`prometheus_client` multiproc mode on :9090), docker-compose prometheus service, dropped in-process `/metrics` route
- **Phase 5.5 E** — Module globals → FastAPI `Depends` migration (`get_retriever`, `get_audit_index`, `get_pipeline`, `get_redis_lock`, `get_task_repo`); removed 5 setters
- **Phase 5.5 F** — `audit.log` rotation 100 MB × 5 gzip backups via `RebuildingRotatingFileHandler`; `/healthz` audit suppression via `ContextVar` skip flag; on-rollover callback rebuilds `AuditIndex` so replay offsets stay valid
- **Phase 8 — Production hardening**: /v1/* rate-limit (60 req/min/IP, token bucket), secret rotation SOP + offline validator, vendored bge-m3 ONNX into Docker, ingestion smoke canary, golden set 42→50, 10k chunker bench baseline p99=279µs
- **Phase 9 — Stress tooling**: scripts/live_stress_60.py 3 modes (offline / retry-failed / stress), 60/60 + 200/200 verified, NOTIFY_HTTP_TIMEOUT_S 60, sequential pacing
- **Phase 10 T10b-1 — Chunker refactor**: `_route_accumulated_group` helper unifies Boundary 2 (scope-change) + Boundary 3 (token-overflow); 8 unit tests + 60-doc stress + 10k bench p99=155µs (44% faster than Phase 8 baseline). Tag `phase10.1` locked at commit `1c44eee`.
- **Phase 10 T10a-1 — FTSManager**: SQLite FTS5 BM25 keyword retrieval; `rag/ekrs_rag/retrieval/fts_manager.py` with `generate_chunk_id` + `delete_by_chunk_id` + R7/R8/H2/T10a-5 invariants; 23 unit + 8 integration tests; mypy clean.
- **Phase 10 T10a-2 — Pipeline FTS sync + drift detection**: `IngestionPipeline.ingest()` Step 5.6 paired Qdrant+FTS write; `FTSManager.replace_doc()` (atomic delete-then-upsert for re-ingest idempotency) + `count_active()` (excludes illegal); `QdrantManager.count_points()`; `ConcurrencyChecker` 5min background task compares counts → `fts_consistency_drift` audit + `ekrs_index_consistency_drift_total` counter on drift. Detect-only — never auto-repairs (parent plan §T10a-2 mandate). 10 unit + 5 integration tests; mypy clean; pipeline `fts=None` kwarg keeps Phase 9 baseline byte-level.
- **Phase 10 T10a-3 — RRF pure function + FusionStats**: `rag/ekrs_rag/retrieval/rank_fusion.py` ships `reciprocal_rank_fusion(ranked_lists, key_fn, k=60) -> (fused_results, FusionStats)` (pure R2 function, deterministic) + `FusionStats` frozen dataclass (vector_hits/fts_hits/both_hits) for T10a-7 audit consumption. 17 unit tests cover empty/single/dual/N=3 lists, k parameter, dedup-within-sublist, key_fn exception propagation, set-arithmetic invariant, frozen enforcement. mypy clean. No new tag (phase10.1 stays locked at 1c44eee T10b-1; phase10 reserved for T10a-7 closure).

Tags: `phase5.5-d-metrics-exporter`, `phase5.5-e-retriever-depends`, `phase5.5-f-audit-rotation`, `phase5-observability`, `phase8`, `phase9`, `phase10.1` (T10b-1 do-not-move anchor at `1c44eee`).

## Development Phases (from spec §6)

1. Foundation: DB, versioning, heartbeat, callback server
2. Deterministic solver core: hint extractor, evidence builder, interval solver, context manager
3. Scope-aware retrieval & multi-branch output
4. System integration: idempotent callbacks, distributed locks, reconciliation
5. Observability: Prometheus metrics, audit log, CI gate, Replay mode

All five phases shipped. Phase 5.5 D/E/F were Phase-5 retrofits (sidecar exporter, Depends migration, audit rotation). Next scope (Phase 6) not yet defined in `ekrs-handbook.md`.

## Important Code Patterns

- **portion.Interval**: Use factory functions (`portion.closedopen`, `portion.openclosed`, `portion.open`) NOT `Interval(left=, right=)` kwargs
- **Priority dedup**: Dedup key = (parameter, operator, value, unit) — excludes scope_path. Priority from scope_path prefix: national(100) > industry(80) > enterprise(60) > project(40) > reference(20)
- **Temperature conversion**: Affine (F→C uses (F-32)*5/9, not scalar)
