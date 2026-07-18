# EKRS — Engineering Knowledge Recovery System

> RAG service that extracts structured engineering constraints (temperature, pressure, material limits) from unstructured engineering documents (PDF/Word/DWG), computes parameter feasible ranges via a deterministic solver, and exposes scope-aware conflict detection via HTTP API.

**Status:** 545 tests passing · coverage 86.75% · 8 phase tags shipped (`phase5` … `phase6c-minor`).

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
shared/ekrs_shared/   Pydantic models · unit normalizer (affine temp conversion) · audit base
rag/ekrs_rag/         FastAPI service: ingestion, retrieval (Qdrant), constraint solving, observability
dev_ui/               Streamlit debug UI — placeholder only; **not enabled in Phase 6** (planned Phase 7). Do not rely on it.
deployment/           docker-compose.yml, prometheus.yml, scrape config
docs/                 Public-facing documentation (ARCHITECTURE, USAGE, CHANGELOG, DEPLOYMENT)
docs/superpowers/     Internal design specs & implementation plans
ekrs-handbook.md      Authoritative spec (Iron Rules, schema, audit events)
CONTRIBUTING.md       How to extend the codebase (Hint patterns, Qdrant fields, audit events)
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
| `make golden-test` | Run the 42-case golden set from `ekrs-handbook.md` §9.1 (regression gate) |
| `make mock-notify` | Trigger a fake parser notification (against running stack) |
| `make run-local` | Run uvicorn without Docker (needs qdrant+redis running locally) |
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

| Tag | Commit | Scope |
|-----|--------|-------|
| `phase5-observability` | — | Prometheus metrics · audit log · @audited / @metered decorators |
| `phase5.5-d-metrics-exporter` | — | `/metrics` sidecar on :9090 · multiproc mode · docker-compose prometheus |
| `phase5.5-e-retriever-depends` | — | Module globals → FastAPI `Depends` migration |
| `phase5.5-f-audit-rotation` | — | `audit.log` 100 MB × 5 gzip backups · `/healthz` audit suppression · index rebuild on rollover |
| `phase6a-spec-closure` | — | 9 vertical slices (X-Admin-Key, DocumentRepo, /trace, /calculate, soft fallback, golden 13→42, audit 2 fields, ENGINE_URL, 85% CI gate) |
| `phase6b-retrieval-layer` | f692c42 | Vendor bge-m3 ONNX · EmbeddingService facade · QdrantManager rewrite (3 prod bug fixes B1/B2/B3) |
| `phase6c-audit-emit` | c61e982 | `qdrant_write_failed` audit emit + non-fatal Qdrant init in lifespan |
| `phase6c-minor` | e320717 | `delete_old_versions` filter fix · narrowed `except` · consolidated pip install |

Spec gaps between phases are tracked in `docs/superpowers/plans/` and `.superpowers/sdd/progress.md`.

---

## Iron Rules (never violate)

Defined in `ekrs-handbook.md` §Iron Rules. Eight invariants govern ingestion, retrieval, solving, and conflict semantics. Reviewed at every phase boundary.

---

## Documentation map

- `README.md` — this file; project facade
- `ekrs-handbook.md` — authoritative spec (Iron Rules, schema, audit, deployment flow §7.4)
- `CONTRIBUTING.md` — how to extend Hint patterns, Qdrant fields, audit events; PR check matrix
- `docs/ARCHITECTURE.md` — module layout, data flow, embedded diagrams
- `docs/USAGE.md` — external API reference with curl examples + troubleshooting runbooks
- `docs/DEPLOYMENT.md` — Kubernetes / bare-metal production checklist, Ingress, dim migration
- `docs/CHANGELOG.md` — phase-by-phase summary + rollback strategy
- `docs/superpowers/specs/` — per-phase design specs
- `docs/superpowers/plans/` — per-phase implementation plans
- `golden.md` — DEPRECATED, content merged into `ekrs-handbook.md` §9.1

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
