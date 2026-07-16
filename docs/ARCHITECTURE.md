# Architecture

> Internal layout, module map, and data flows. Authoritative behavioral spec lives in [`ekrs-handbook.md`](../ekrs-handbook.md); this document explains the **shape** of the system.

---

## High-level

```
                                ┌──────────────────────────────────┐
                                │     External Parser (out of      │
                                │     process; writes JSONL)       │
                                └────────────┬─────────────────────┘
                                             │ JSONL files
                                             ▼
   ┌────────────┐  POST /v1/ingestion/notify    ┌──────────────────────────────────────────┐
   │            │ ────────────────────────────▶ │  RAG service (FastAPI :8000)              │
   │   Parser   │                               │                                          │
   │            │ ◀──── callback /status ────── │  ┌─────────────┐    ┌──────────────────┐  │
   └────────────┘                               │  │ Ingestion   │───▶│ Qdrant :6333     │  │
                                                │  │ Pipeline    │    │ (bge-m3 1024d    │  │
   ┌────────────┐  POST /v1/constraints         │  └─────────────┘    │  dense + sparse) │  │
   │   User /   │ ────────────────────────────▶ │            │        └──────────────────┘  │
   │   Frontend │ ◀──── multi-branch JSON ────  │            ▼                                │
   └────────────┘                               │  ┌─────────────┐    ┌──────────────────┐  │
                                                │  │ Retriever   │───▶│ Constraint       │  │
                                                │  │             │    │ Engine (IR V2)   │  │
                                                │  └─────────────┘    └──────────────────┘  │
                                                │            │                                │
                                                │            ▼                                │
                                                │  ┌──────────────────────────────────────┐  │
                                                │  │  Observability                       │  │
                                                │  │  · audit.log (16-event schema)       │  │
                                                │  │  · /metrics sidecar :9090            │  │
                                                │  │  · AuditIndex for replay             │  │
                                                │  └──────────────────────────────────────┘  │
                                                │            │                                │
                                                │            ▼                                │
                                                │  ┌─────────────┐    ┌──────────────────┐  │
                                                │  │ Redis :6379 │    │ SQLite task/doc  │  │
                                                │  │ (locks,     │    │ metadata DBs     │  │
                                                │  │  replay)    │    │                  │  │
                                                │  └─────────────┘    └──────────────────┘  │
                                                └──────────────────────────────────────────┘
```

The Parser is **out of process**; it owns ingestion of source PDFs/Word/DWG. RAG only sees JSONL rows.

---

## Repository layout

```
ekrs/
├── shared/ekrs_shared/          # Cross-package primitives
│   ├── models.py                # Pydantic IR models (Chunk, Hint, NumericHint, IngestionStatus, ...)
│   ├── normalizer.py            # Affine unit conversion (F→C, MPa→Pa, K→C) using portion.Interval
│   ├── audit.py                 # AuditLog base + base event schemas
│   ├── idempotency.py           # request_id dedup helper
│   └── utils.py
├── rag/ekrs_rag/                # RAG service (FastAPI app)
│   ├── main.py                  # app factory + lifespan (Qdrant init, Redis, audit writer, scan compensation)
│   ├── cli.py                   # python -m ekrs_rag entry helper
│   ├── security.py              # X-Parser-Token + X-Admin-Key checks
│   ├── api/
│   │   ├── middleware/observability.py   # Audit + metric timers per request
│   │   ├── decorators.py                 # @audited (async), @metered
│   │   └── routes/
│   │       ├── ingestion.py    # /v1/ingestion/{notify, status/{hash}, replay}
│   │       ├── constraints.py  # /v1/constraints, /v1/constraints/trace
│   │       ├── calculate.py    # /v1/calculate
│   │       └── trace.py        # /v1/trace/{replay, query_history}
│   ├── ingestion/               # Pipeline: read JSONL → chunk (scope-aware) → encode → upsert
│   ├── retrieval/
│   │   ├── embedding_service.py # bge-m3 ONNX via FlagEmbedding (dense 1024d + sparse)
│   │   ├── qdrant_client.py    # QdrantManager: ensure/upsert/search/delete_old_versions
│   │   └── retriever.py        # scope-priority composite scoring, multi-branch output
│   ├── constraint_engine/       # IR V2 (deterministic solver)
│   │   ├── parser.py           # hint extraction from chunk text
│   │   ├── evidence_builder.py # merges hints into Evidence per parameter
│   │   ├── solver.py           # interval solver (portion) — pure function
│   │   └── normalizer.py       # unit-affine conversion at IR level
│   ├── observability/
│   │   ├── audit.py            # AuditWriter (async, rotation, schema registry) — extends base
│   │   ├── audit_index.py      # AuditIndex for replay offsets
│   │   └── metrics.py          # route counters + latency histograms
│   ├── concurrency/
│   │   ├── redis_lock.py       # Redis SET-NX-PX distributed lock (with watchdog)
│   │   └── compensation.py     # CompensationScanner (retries orphan-ingestion tasks)
│   ├── core/                   # config, structured logging
│   ├── session/                # session-scoped state
│   └── storage/
│       ├── task_repo.py        # SQLite task state (aiosqlite)
│       └── documents.py        # DocumentRepo (Phase 6A spec §4 metadata)
├── dev_ui/                      # Streamlit debug UI (placeholder)
├── deployment/
│   ├── docker-compose.yml      # qdrant + redis + rag + prometheus
│   └── prometheus.yml          # scrape config (rag :9090 sidecar)
├── docs/                        # Public-facing docs (this directory)
└── docs/superpowers/            # Internal specs & plans
```

---

## Two data flows

### Flow 1 — Ingestion (Parser → RAG → Qdrant)

```
Parser writes chunks JSONL
  ↓
Parser POST /v1/ingestion/notify { doc_hash, chunk_count }
  ↓ (X-Parser-Token verified)
IngestionRoute.notify (v1/ingestion.py)
  ├─ idempotency check by request_id → replay short-circuit if seen
  ├─ acquire RedisLock(INGESTION_LOCK_TIMEOUT) on doc_hash
  ├─ enqueue row in task_repo (status=pending)
  ├─ BackgroundTasks: pipeline.ingest(doc_hash)
  └─ 202 Accepted
  ↓
IngestionPipeline.ingest(doc_hash)
  ├─ read JSONL from SHARED_STORAGE_PATH
  ├─ chunker (scope-aware: scope_path injected into Chunk.scope_path)
  ├─ EmbeddingService.encode(texts) → dense 1024d + sparse indices/values
  ├─ QdrantManager.upsert_chunks(chunks)
  │    └─ write payload {text, scope_path, doc_hash, version, ...} (R1/R7)
  ├─ task_repo.mark_complete
  ├─ audit("ingestion_completed")
  └─ POST ENGINE_URL notify-completion callback (Parser side handler)
```

Failure branches emit audit events (`ingestion_failed`, `qdrant_write_failed`, `document_metadata_failed`, `compensation_retry`).

### Flow 2 — Constraint query (User → Retriever → Solver)

```
User POST /v1/constraints { query, parameters?, scope_path?, strict? }
  ↓ (X-Parser-Token OR X-Admin-Key)
ConstraintsRoute.query_constraints
  ├─ audit("constraint_solve_started")
  ├─ Retriever.retrieve(query) → top-k chunks (hybrid dense+sparse via Qdrant)
  ├─ parser.extract_hints(chunks) → list[Hint]
  ├─ evidence_builder.build(hints, parameters) → dict[param, Evidence]
  ├─ solver.solve(evidences, scope_path, strict) → list[Branch]   [PURE function, R2]
  │   ├─ normalizer: convert all units to canonical; F→C affine (not scalar)
  │   ├─ interval arithmetic via portion.closedopen / openclosed
  │   ├─ priority dedup by (param, op, value, unit); scope_path dedupes later
  │   ├─ strict mode rejects inferred (R6 → 400 missing_context)
  │   └─ hard conflict returns branch with conflict_details (R4)
  ├─ audit("constraint_solved" or "constraint_solve_failed")
  └─ 200 with multi-branch response (high-temp / general-condition branches)
```

Pure solver (R2) means replay by `trace_id` is deterministic — re-running the same evidence produces identical output (`/v1/constraints/trace`).

---

## Cross-cutting concerns

- **Observability middleware** wraps every request: emits `endpoint_started`/`endpoint_completed`, tracks latency histogram.
- **Audit** events: 16 schemas frozen (see `ekrs-handbook.md` §Audit). Phase 6B broadened `qdrant_write_failed` semantic to include any Qdrant op (`operation: read|write|delete`).
- **Replay**: `AuditIndex` byte-offset index into `audit.log` enables re-running a past trace without re-ingesting.
- **Compensation**: at startup, `CompensationScanner` finds task rows older than 60s that haven't moved; retries or marks `compensation_retry` if `handler_is_wired` (currently `False` — stub logs warning).

---

## Dependency wiring (Phase 5.5 E)

`main.py` lifespan attaches singletons via FastAPI `Depends`. Routes never reach into module globals.

```python
def get_retriever(request: Request) -> EKRSRetriever       # constraints
def get_pipeline(request: Request) -> IngestionPipeline    # ingestion
def get_redis_lock(request: Request) -> RedisLock          # ingestion
def get_task_repo(request: Request) -> TaskRepo            # compensation
def get_audit_index(request: Request) -> AuditIndex        # trace / replay
def get_document_repo(request: Request) -> DocumentRepo    # X-Admin-Key routes
```

If Qdrant init fails (Phase 6C lifespan), `app.state.{qdrant, retriever, pipeline}` stay `None`; route-level `Depends` helpers return 503. The metrics sidecar keeps serving so Prometheus can still scrape.

---

## Storage summary

| Store | Holds | Used by |
|-------|-------|---------|
| **Qdrant** (vector DB) | Chunk embeddings + payload (text, scope_path, doc_hash, version) | retrieval |
| **Redis** | Distributed lock keys; idempotency dedup set | ingestion, replay |
| **`tasks.db`** (SQLite, aiosqlite) | Task rows: request_id, doc_hash, status, attempts, last_heartbeat | compensation scanner |
| **`documents.db`** (SQLite) | Document metadata (lifecycle, lineage) | DocumentRepo / X-Admin-Key |
| **`audit.log`** (JSONL) | Append-only event stream | AuditIndex, replay, /healthz readiness |

---

## Deployment

`make dev` brings up `deployment/docker-compose.yml`:

| Service | Port | Notes |
|---------|------|-------|
| qdrant | 6333 (REST), 6334 (gRPC) | persisted volume `qdrant_data` |
| redis | 6379 | no persistence |
| rag | 8000 (API), 9090 (sidecar `/metrics`) | depends_on qdrant+redis healthy |
| prometheus | 19090 → 9090 (host:container) | scrapes rag :9090 every 15s |

First deployment with a new embedding dim (e.g., bge-small 384d → bge-m3 1024d): see `ekrs-handbook.md` §7.4. Set `AUTO_REINDEX=true` to auto-rebuild; set `false` in production to require manual delete+recreate.
