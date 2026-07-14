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

## Current State (as of 2026-07-14)

Phases 1-5 all complete. 346 tests passing, 1 skipped.

- **Phase 1 — Foundation**: shared/ekrs_shared/ (Pydantic models, normalizer, audit base); rag/ekrs_rag/ingestion/ (IR parser, scope-aware chunker, pipeline); rag/ekrs_rag/retrieval/ (Qdrant client); notify/status routes
- **Phase 2 — Solver core (V2)**: hint extractor, evidence builder, interval solver (`portion`), context manager, IR V2 multi-branch, golden set
- **Phase 3 — Scope-aware retrieval**: scope-priority composite scoring, multi-branch output (high-temp / general-condition branches)
- **Phase 4 — System integration**: callback idempotency, TaskRepo (aiosqlite), RedisLock, CompensationScanner, main.py lifespan wiring
- **Phase 5 — Observability**: AuditLogger base + AuditWriter + AuditIndex, Prometheus metrics (route counters, latency, failures), @audited / @metered decorators, query & ingestion replay, debug.log rotation, /healthz JSON endpoint
- **Phase 5.5 D** — `/metrics` sidecar exporter (`prometheus_client` multiproc mode on :9090), docker-compose prometheus service, dropped in-process `/metrics` route
- **Phase 5.5 E** — Module globals → FastAPI `Depends` migration (`get_retriever`, `get_audit_index`, `get_pipeline`, `get_redis_lock`, `get_task_repo`); removed 5 setters
- **Phase 5.5 F** — `audit.log` rotation 100 MB × 5 gzip backups via `RebuildingRotatingFileHandler`; `/healthz` audit suppression via `ContextVar` skip flag; on-rollover callback rebuilds `AuditIndex` so replay offsets stay valid

Tags: `phase5.5-d-metrics-exporter`, `phase5.5-e-retriever-depends`, `phase5.5-f-audit-rotation`, `phase5-observability`.

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
