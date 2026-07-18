# Usage — external API

> End-to-end curl examples for the public HTTP surface. Hostname `http://localhost:8000` assumes `make dev` (docker-compose stack). Adjust for your environment.

---

## Interactive API explorer (Swagger UI)

> **Recommended for debugging:** once the stack is up, browse
> **http://localhost:8000/docs** for the auto-generated Swagger UI. Every
> route below is documented there with a "Try it out" panel that builds
> the request, applies the auth header, and shows the response — much
> faster than typing curl by hand. The OpenAPI schema is at
> `http://localhost:8000/openapi.json`.

The curl examples below remain the authoritative reference for scripted
use, CI, and production examples.

---

## Authentication

Two token headers, mutually distinct:

| Header | Env var | Used by |
|--------|---------|---------|
| `X-Parser-Token: <PARSER_TOKEN>` | `PARSER_TOKEN` (≥32 chars) | `/v1/ingestion/*`, `/v1/constraints` |
| `X-Admin-Key: <ADMIN_KEY>` | `ADMIN_KEY` (≥32 chars) | Admin routes (if `ADMIN_KEY` non-empty); otherwise 503 |

When `ADMIN_KEY` is empty, admin endpoints return 503. Always set in production.

---

## Health checks

### `GET /health` — plain liveness

```bash
curl -s http://localhost:8000/health
# → "ok"
```

### `GET /healthz` — structured readiness

```bash
curl -s http://localhost:8000/healthz | jq
```

```json
{
  "audit_log_writable": true,
  "audit_index_loaded": true,
  "audit_index_size": 1234,
  "audit_index_load_seconds": 0.42,
  "task_repo_initialized": true
}
```

Returns **200** when audit log is writable AND index is loaded; otherwise **503**.

---

## Ingestion (Parser → RAG)

### `POST /v1/ingestion/notify` — queue ingestion

The Parser calls this after writing JSONL chunks to `SHARED_STORAGE_PATH`.

**Request body** (`IngestionNotification` in `shared/ekrs_shared/models.py`):

```json
{
  "trace_id": "parser-trace-abc123",
  "doc_hash": "sha256:abcd...123",
  "version": 2,
  "output_path": "/parsed_lib/abcd.../v2/chunks.jsonl",
  "callback_url": "http://parser:7000/callbacks/ingestion",
  "metadata": {
    "doc_type": "standard",
    "lifecycle_status": "active"
  }
}
```

```bash
curl -s -X POST http://localhost:8000/v1/ingestion/notify \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "trace_id": "parser-trace-abc123",
    "doc_hash": "sha256:abcd1234",
    "version": 2,
    "output_path": "/parsed_lib/abcd1234/v2/chunks.jsonl"
  }'
# → 202 Accepted
```

**Behavior**:

- Same `(trace_id, doc_hash, version)` → 202 **duplicate** (idempotent).
- Another in-flight ingest for the same `doc_hash` → 202 **in_flight** (locked).
- Otherwise: enqueue a `pending` row in `tasks.db`, kick off `pipeline.ingest(...)` in a background task, audit `ingestion_received`.

On completion, RAG POSTs to the notification's `callback_url` (or falls back to `ENGINE_URL` from server config).

### `GET /v1/ingestion/status/{doc_hash}`

```bash
curl -s -H "X-Parser-Token: $PARSER_TOKEN" \
  http://localhost:8000/v1/ingestion/status/sha256:abcd1234 | jq
```

```json
{
  "status": "success",
  "chunks_indexed": 137,
  "version": 2
}
```

Other status values: `pending`, `failed` (with `error` field populated).

### `POST /v1/ingestion/replay` — re-run ingestion

Re-process an existing JSONL by `(doc_hash, version)`:

```bash
curl -s -X POST http://localhost:8000/v1/ingestion/replay \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "doc_hash": "sha256:abcd1234", "version": 2 }'
```

Emits `ingestion_replay_started` / `_completed` / `_sha256_mismatch` audit events.

---

## Constraint queries

### `POST /v1/constraints` — solve constraints

**Request:**

```json
{
  "query": "What is the maximum operating temperature?",
  "parameters": ["temperature", "pressure"],
  "scope_path": ["national", "GB"],
  "strict": false,
  "top_k": 40,
  "score_threshold": null
}
```

**Fields:**

- `query` (str, required) — natural-language question.
- `parameters` (list[str], optional) — restrict to specific parameter names; null/empty means all.
- `scope_path` (list[str], optional) — priority hierarchy filter; `["national", "GB"]` means prefer chunks whose `scope_path` matches.
- `strict` (bool) — when `true`, rejects `inferred` constraints and returns **400 `missing_context`** rather than guessing (Iron Rule R6).
- `top_k` (int, default 40) — chunks to recall from Qdrant.
- `score_threshold` (float|null) — minimum score; null disables cutoff.

**Example:**

```bash
curl -s -X POST http://localhost:8000/v1/constraints \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Maximum temperature for high-pressure pipes",
    "parameters": ["temperature"],
    "scope_path": ["industry", "petrochemical"],
    "strict": false
  }' | jq
```

**Success response (200):**

```json
{
  "trace_id": "abc-123",
  "branches": [
    {
      "branch_id": "high-temp",
      "conditions": [{"field": "environment", "operator": "=", "value": "高温"}],
      "parameters": [
        {
          "name": "temperature",
          "interval": {"lower": 50.0, "upper": 80.0, "lower_inclusive": true, "upper_inclusive": true},
          "unit": "C",
          "source": "Explicit_Doc",
          "evidence": [
            {"provision_id": "GB150-2011/5.3.2", "text": "...", "scope_path": ["industry", "petrochemical"]}
          ]
        }
      ],
      "conflict_details": null
    }
  ]
}
```

**Failure responses:**

| Code | When |
|------|------|
| 400 `missing_context` | `strict=true` and no Explicit_Doc constraint available (R6) |
| 404 `no_constraints_extracted` | No matching hints in any retrieved chunk |
| 404 `insufficient_recall` | Recall returned fewer chunks than `MIN_RECALL_CHUNKS` |
| 409 `conflict` | Hard conflict between two binding constraints; body contains `conflict_details` with both `provision_id`s |
| 503 | Qdrant/embedding init failed in lifespan — service running degraded (Phase 6C) |

### `POST /v1/constraints/trace` — trace a past solve

Fetch the audit record for a previous `trace_id`:

```bash
curl -s -X POST http://localhost:8000/v1/constraints/trace \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "trace_id": "abc-123" }' | jq
```

Returns the audit event + (replayable) evidence. Use `?replay=true` to re-run with the recorded inputs and compare to historical output (`constraint_solve_failed` if mismatch).

---

## Observability

### `GET /metrics` — Prometheus exposition

The metrics endpoint lives on a **separate port** (the sidecar exporter, not the main RAG port):

```bash
curl -s http://localhost:9090/metrics | head -20
```

`METRICS_PORT` (default `9090`) and `METRICS_HOST` (default `127.0.0.1` for local, `0.0.0.0` in docker-compose) configure it. In multi-worker mode, set `PROMETHEUS_MULTIPROC_DIR` to a writable directory before starting.

### Audit log

- Permanent: `AUDIT_LOG_PATH` (default `audit.log` in CWD). 100 MB × 5 gzip rotations. Structured JSONL.
- Debug: `DEBUG_LOG_PATH` only when `EKRS_DEBUG=true`. Rotatable, max 100 MB × 5.

Replay any past `trace_id` via `/v1/constraints/trace` to see what the solver saw.

---

## First deployment (bge-small → bge-m3 dim migration)

See `ekrs-handbook.md` §7.4 for the full flow. Short version:

```bash
# 1. Set new model in .env
EMBEDDING_MODEL=bge-m3   # or set in deployment config

# 2. Decide reindex behavior
AUTO_REINDEX=true        # dev: collection auto-rebuilds on dim mismatch
# AUTO_REINDEX=false     # prod: refuse to start; requires manual Qdrant rebuild

# 3. Start stack
make dev
# Lifespan checks existing collection dim; if 384 (old) != 1024 (new) and
# AUTO_REINDEX=true, it deletes and recreates, then upserts on next ingest.
```

Production setting recommendation: `AUTO_REINDEX=false`. Run a controlled migration per §7.4.

---

## Common error patterns

| Symptom | Likely cause |
|---------|--------------|
| 401 on every call | `X-Parser-Token` missing or mismatched; check `PARSER_TOKEN` env |
| 503 from `/v1/constraints` | Qdrant unreachable; see docker-compose logs and `/healthz` |
| `ConstraintSolveFailed audit` | `qdrant_write_failed` in `audit.log` with `operation: read`; check Qdrant logs |
| `EMBEDDING_MODEL` change has no effect | Restart rag container; bge-m3 is loaded once at startup |
| `EmbeddingUnavailableError` upsert | Model files missing (check `rag/models/bge-m3/`); service enters dummy mode (no upsert) |

---

## Troubleshooting runbooks

> For on-call engineers. Assumes the production deployment shape from
> `docs/DEPLOYMENT.md`. All paths assume `AUDIT_LOG_PATH=/var/log/ekrs/audit.log`
> and the `tasks.db` SQLite under `rag/storage/`.

### Runbook 1 — Investigating `qdrant_write_failed` events

When `/healthz` reports healthy but constraint queries return 503, or
when the audit log shows `qdrant_write_failed` spikes:

```bash
# 1. Count recent failures by operation type
grep '"event":"qdrant_write_failed"' /var/log/ekrs/audit.log | \
  tail -1000 | jq -r '.payload.operation' | sort | uniq -c

# 2. Pull the most recent failure with full context
grep '"event":"qdrant_write_failed"' /var/log/ekrs/audit.log | tail -1 | jq

# 3. Cross-reference with Qdrant container logs
docker logs qdrant --since 10m | grep -i "error\|refused\|timeout"

# 4. Check Qdrant health from inside the rag pod
kubectl exec -it deploy/rag -- curl -sf http://qdrant:6333/healthz
```

If the failures correlate with `operation: read`, the issue is in
`QdrantManager.search()` — usually a stale gRPC connection after a Qdrant
restart. Restart the rag pod to clear the connection pool.

If `operation: write`, check Qdrant disk usage (`/qdrant/storage`); a full
disk presents as `qdrant_write_failed` with `collection: <name>` and no
exception message.

### Runbook 2 — Recovering from `compensation_retry`

`compensation_retry` fires when `CompensationScanner` finds task rows
older than 60s without progress (see `rag/ekrs_rag/concurrency/compensation.py`).
This is normal at startup if the rag pod was OOM-killed mid-ingest, but
sustained occurrences indicate a deeper problem.

```bash
# 1. Check task state distribution
sqlite3 /var/lib/ekrs/rag/storage/tasks.db \
  "SELECT status, COUNT(*) FROM tasks GROUP BY status;"

# 2. Find tasks stuck in 'pending' or 'in_progress' for >10 minutes
sqlite3 /var/lib/ekrs/rag/storage/tasks.db \
  "SELECT request_id, doc_hash, status, attempts, last_heartbeat
   FROM tasks
   WHERE status IN ('pending','in_progress')
     AND last_heartbeat < strftime('%s','now') - 600;"

# 3. Manual reset: move them back to 'pending' for the scanner to retry
sqlite3 /var/lib/ekrs/rag/storage/tasks.db \
  "UPDATE tasks SET status='pending', last_heartbeat=strftime('%s','now')
   WHERE request_id IN ('<id1>','<id2>');"

# 4. The scanner picks them up on its next tick (default 30s). Watch logs:
kubectl logs deploy/rag -f | grep compensation
```

If the same `doc_hash` keeps cycling through compensation, the JSONL at
`SHARED_STORAGE_PATH/<doc_hash>/` is malformed — restore from parser
backup and re-trigger via `POST /v1/ingestion/replay`.

### Runbook 3 — Replay a past constraint solve

Every successful `/v1/constraints` call writes a `constraint_solved` event
with the original request payload. To re-run a past query (useful when
regression-hunting a behavior change):

```bash
# Pull the event by trace_id
grep '"trace_id":"<id>"' /var/log/ekrs/audit.log | \
  jq 'select(.event=="constraint_solved") | .payload.request'

# Replay via the trace endpoint (deterministic per R2)
curl -s -X POST http://localhost:8000/v1/constraints/trace \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "trace_id": "<id>", "replay": true }' | jq
```

A mismatch between the replay output and the original
`constraint_solved` payload means the solver is non-deterministic — that
is an Iron Rule R2 violation and should be filed as a P0 bug.

### Runbook 4 — `audit.log` is full / not rotating

Phase 5.5 F rotates at 100 MB × 5 gzip backups via
`RebuildingRotatingFileHandler`. If rotation stops:

```bash
# 1. Check disk space and inode count on the audit PVC
df -h /var/log/ekrs/
df -i /var/log/ekrs/

# 2. Verify the writer process is running and has write permission
ls -la /var/log/ekrs/audit.log
kubectl exec deploy/rag -- ls -la /var/log/ekrs/

# 3. Force a rotation by sending SIGHUP to the rag process
# (handler is configured for SIGUSR1; verify via signals(7))
kubectl exec deploy/rag -- kill -USR1 1

# 4. If the AuditIndex is stale after rotation, rebuild via:
curl -s -X POST http://localhost:8000/v1/admin/audit/rebuild-index \
  -H "X-Admin-Key: $ADMIN_KEY"
```

If rotation keeps failing, the most common cause is the audit PVC being
mounted read-only after a kubelet hiccup. Check `kubectl describe pvc rag-audit`.
