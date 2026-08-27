# EKRS — Engineering Knowledge Recovery System

> RAG service that extracts structured engineering constraints (temperature, pressure, material limits) from unstructured engineering documents (PDF/Word/DWG), computes parameter feasible ranges via a deterministic solver, and exposes scope-aware conflict detection via HTTP API.

**Status:** 946 tests passing · coverage ≥85% · 14 phase tags shipped (`phase5` … `phase13c-c13`). See `CHANGELOG.md` (top-level) for the phase-by-phase history; this README only summarizes the current state.

---

## Quick Start

**Requires Python 3.11.** FlagEmbedding 1.2.13 + onnxruntime<1.18 wheels
are not consistently available for 3.12+; the bge-m3 ONNX loader fails on
3.12 in CI. All heavy-test runners pin 3.11.

```bash
cp .env.example .env
# edit PARSER_TOKEN to a 32+ char secret

make install   # shared/ + rag/ editable deps
make dev       # docker-compose up: qdrant + redis + rag + prometheus
curl http://localhost:8000/healthz   # readiness probe

# In another shell, simulate a parser notification:
make mock-notify
```

Once the stack is up, browse **http://localhost:8000/docs** for the
auto-generated Swagger UI (recommended for debugging over curl).

See `docs/USAGE.md` for end-to-end curl examples and
`docs/DEPLOYMENT.md` for production (Kubernetes / non-Docker) deployment.

---

## What's inside

```
shared/ekrs_shared/    Pydantic models · unit normalizer (affine temp conversion) · audit base
rag/ekrs_rag/          FastAPI service: ingestion, retrieval (Qdrant), constraint solving, observability
dev_ui/                DEPRECATED by Phase 11 T11-5; coexists 1 quarter as fallback. Use `dev_ui_v2/`.
dev_ui_v2/             React SPA (Phase 11) — Vite 5 + TanStack Query + Zod + MSW; Docker + nginx proxy
deployment/            docker-compose.yml, prometheus.yml, scrape config, GPU profile (Phase 13b)
docs/                  Public-facing documentation (ARCHITECTURE, USAGE, DEPLOYMENT, SECRET-ROTATION)
docs/coordinations/    Cross-team hand-offs (e.g. 2026-08-27 doc-to-md monolithic-tables contract)
docs/superpowers/      Internal design specs & implementation plans
ekrs-handbook.md       Authoritative spec (Iron Rules, schema, audit events)
CONTRIBUTING.md        How to extend the codebase (Hint patterns, Qdrant fields, audit events)
CHANGELOG.md           Canonical phase-by-phase history (top-level; `docs/CHANGELOG.md` is deprecated facade)
```

**Pipeline** (Parser → RAG → Solver):

1. External Parser writes JSONL to `SHARED_STORAGE_PATH`.
2. Parser `POST /v1/ingestion/notify` (with `X-Parser-Token`) tells RAG a new document is ready.
3. RAG chunks → encodes via bge-m3 (dense 1024d + sparse) → upserts to Qdrant.
4. RAG `POST /v1/ingestion/notify/callback` to Parser on completion (or failure).
5. User (or Parser) `POST /v1/constraints` with a query → RAG retrieves → hint extraction → deterministic interval solver → structured multi-branch result.

Full architecture diagram and module layout: `docs/ARCHITECTURE.md`.

---

## Commands

| Command | What it does |
|---------|--------------|
| `make install` | Editable install of `shared/` + `rag[dev]` |
| `make dev` | docker-compose up (qdrant + redis + rag + prometheus) |
| `make dev-down` | Stop the stack |
| `make test` | Run pytest with `-v --tb=short` |
| `make test-cov` | Same with coverage report (gate ≥85%) |
| `make lint` | flake8 + mypy on shared/ and rag/ |
| `make heavy-test` | Run `@pytest.mark.heavy` (real bge-m3 load; requires Python 3.11) |
| `make golden-test` | Run the 50-case golden set from `ekrs-handbook.md` §9.1 (regression gate; extended 42→50 in Phase 8 T8-4) |
| `make test-e2e` | Playwright E2E suite against dev_ui_v2 (Phase 12-A) |
| `make mock-notify` | Trigger a fake parser notification (against running stack) |
| `make run-local` | Run uvicorn without Docker (needs qdrant+redis running locally) |
| `make smoke-ingestion` | End-to-end happy-path smoke (Phase 8 T8-3b) |
| `make build-rag-baseline` | Pin CPU rag image SHA into `deployment/rag-image.baseline.json` |
| `make gpu-up` | Start GPU rag service on :8001 (Phase 13b); drift-checks against baseline |
| `make gpu-down` | Stop GPU + restart CPU rag (precise teardown, Phase 13c-C13 fix) |
| `make gpu-baseline` | Pin GPU rag image + torch + host bge-m3 SHA (Phase 13c-C13) |
| `make gpu-acceptance` | T5.1 28-doc smoke bench inside rag-gpu container |
| `make ingest` | GPU-first bulk corpus load (Phase 13c-C13); `ARGS="--limit N --version V"` |
| `make clean` | Remove `__pycache__`, `*.pyc`, `.egg-info`, `.pytest_cache` |

Heavy tests (real bge-m3 model load) are excluded by default and run only
in nightly CI. They require **Python 3.11** — FlagEmbedding 1.2.13 +
onnxruntime<1.18 wheels are unavailable on 3.12+.

To run them locally:

```bash
make heavy-test       # pytest -m heavy
make golden-test      # the 42-case regression set
```

---

## Phase status

Summary only — for the authoritative phase-by-phase history (with commit hashes,
test counts, and buglog entries), see **[`CHANGELOG.md`](CHANGELOG.md)** at the repo root.

| Tag | Scope |
|-----|-------|
| `phase5-observability` | Prometheus metrics · audit log · @audited / @metered decorators |
| `phase5.5-d-metrics-exporter` | `/metrics` sidecar on :9090 · multiproc mode · docker-compose prometheus |
| `phase5.5-e-retriever-depends` | Module globals → FastAPI `Depends` migration |
| `phase5.5-f-audit-rotation` | `audit.log` 100 MB × 5 gzip backups · `/healthz` audit suppression · index rebuild on rollover |
| `phase6a-spec-closure` | 9 vertical slices (X-Admin-Key, DocumentRepo, /trace, /calculate, soft fallback, golden 13→42, audit 2 fields, ENGINE_URL, 85% CI gate) |
| `phase6b-retrieval-layer` | Vendor bge-m3 ONNX · EmbeddingService facade · QdrantManager rewrite (3 prod bug fixes) |
| `phase6c-audit-emit` | `qdrant_write_failed` audit emit + non-fatal Qdrant init in lifespan |
| `phase6c-minor` | `delete_old_versions` filter fix · narrowed `except` · consolidated pip install |
| `phase7` | Operational hardening: `qdrant_write_failed` integration test · 8 audit-event emits · CompensationHandler · Streamlit dev_ui T5 · LRU+TTL embedding cache + admin flush |
| `phase8` | Production hardening: SlowAPI rate-limit · secret rotation SOP · vendored bge-m3 · smoke canary · chunker 10k perf baseline · golden 42→50 |
| `phase9` | Stress tooling: `live_stress_60.py` 3 modes · 60/60 + 200/200 verified · NOTIFY_HTTP_TIMEOUT_S=60 |
| `phase10` | FTS5 + RRF + short-circuit + MCP adapter (`fts_searched`/`fts_synced` audit, dual-closure pattern) |
| `phase10.1` | T10b-1 chunker `_route_accumulated_group` refactor (do-not-move anchor) |
| `phase11` | dev_ui_v2 React SPA scaffold + typed client + MSW + Docker + nginx + deprecate dev_ui/ |
| `phase11.1` | T11-1 scaffold anchor (do-not-move) |
| `phase12` | doc-to-md integration + E2E-in-CI + FTS DB path + ground-truth + column_header fix + Task C classifier + Task D 745-bundle + row-flush + v10 5-round convergence |
| `phase12.1` | Task C classifier anchor (do-not-move) |
| `phase13a` | GPU-encodable EncodingPool (pebble) + admission control + notification inline-coarse + audit event count 22→24 |
| `phase13b` | torch FP16 bge-m3 GPU encoder + EncodingRouter + 30s probe + channel_switched audit + E2E acceptance |
| `phase13c` | audit pipeline hardening + Literal FAILED fix + dynamic threshold + ops guide |
| `phase13c-c13` | Post-closure corpus re-ingest (3809 bundles, 97.3% success) + GPU-first `make ingest` + GPU baseline pinning (no new tag; absorbed under phase13c) |

Spec gaps between phases are tracked in `docs/superpowers/plans/` and `.superpowers/sdd/progress.md`.

---

## Iron Rules (never violate)

Defined in `ekrs-handbook.md` §Iron Rules. Eight invariants govern ingestion, retrieval, solving, and conflict semantics. Reviewed at every phase boundary.

---

## Documentation map

- `README.md` — this file; project facade
- `CHANGELOG.md` — canonical phase-by-phase history + rollback strategy (top-level)
- `ekrs-handbook.md` — authoritative spec (Iron Rules, schema, audit, deployment flow §7.4)
- `CONTRIBUTING.md` — how to extend Hint patterns, Qdrant fields, audit events; PR check matrix
- `docs/ARCHITECTURE.md` — module layout, data flow, embedded diagrams
- `docs/USAGE.md` — external API reference with curl examples + troubleshooting runbooks
- `docs/DEPLOYMENT.md` — Kubernetes / bare-metal production checklist, Ingress, dim migration, GPU baseline
- `docs/SECRET-ROTATION.md` — PARSER_TOKEN / ADMIN_KEY rotation SOP (Phase 8 T8-2, 90-day cadence)
- `docs/CHANGELOG.md` — DEPRECATED facade (frozen at phase6c-minor 2026-07-21); see top-level `CHANGELOG.md`
- `docs/coordinations/` — cross-team hand-off documents (e.g. `2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md`)
- `docs/superpowers/specs/` — per-phase design specs
- `docs/superpowers/plans/` — per-phase implementation plans
- `golden.md` — DEPRECATED, content merged into `ekrs-handbook.md` §9.1

---

## Post-deploy tech debt

Items deferred past current ship state. Authoritative registry:
[`CHANGELOG.md`](CHANGELOG.md) §"Pending post-deploy" sections (Phase 13a, 13b, 13c).
No full registry is duplicated here — see the changelog for current status of:
Qdrant HNSW tuning, multi-region replication, embedding batch concurrency at GPU scale,
mTLS / JWT service-to-service authn, `audit.log` remote archival, cross-process audit
wiring, GPU real-infra equivalence runs, and GT JSON fill.

---

## Configuration

See `.env.example` for the full variable list. Minimum required to start:

- `PARSER_TOKEN` (≥32 chars; auth header `X-Parser-Token`)
- `SHARED_STORAGE_PATH` (where Parser writes JSONL, RAG reads)
- `QDRANT_HOST` + `QDRANT_GRPC_PORT`
- `REDIS_URL`

Optional: `ADMIN_KEY` (enables admin endpoints, requires `X-Admin-Key`), `EKRS_DEBUG` (verbose logging + /dev-ui), `AUTO_REINDEX` (rebuild Qdrant collection on dim mismatch — true for dev, false in production).

---

## License

Internal project; no public license declared.
