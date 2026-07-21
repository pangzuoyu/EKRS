---
title: Phase 6C T4 manual smoke runbook — T14 regression confirmation
date: 2026-07-21
category: docs/superpowers/plans/
module: EKRS/rag
problem_type: runbook
component: integration_smoke
severity: medium
applies_when:
  - Verifying Phase 6A integration-fixes (commit f92b724) end-to-end
  - Pre-merge smoke before promoting to release branch
tags: [ekrs, phase6c, smoke-test, t14, runbook]
---

# Phase 6C T4 Manual Smoke Runbook

## Status (2026-07-21)

**Deferred** — blocked by docker registry unreachable (IPv6 dial timeout to `registry-1.docker.io`). No local `qdrant/qdrant:latest` or `redis:7-alpine` images cached. Requires human to run when network is restored.

## What this runbook verifies

Phase 6A T14 final review (`docs/superpowers/plans/2026-07-21-ekrs-integration-fixes-review.md`) identified 1 Critical bug (emit_event → write rename — fixed in commit `f92e8e1`) and several Important findings. This smoke confirms the *runtime* behavior, not just unit-test coverage, since several Phase 6A fixes touch Qdrant client 1.17.1 API paths, redis lock semantics, and the callback retry path.

## Prerequisites

- Docker daemon running with IPv4 network access to `registry-1.docker.io` (or local mirror configured)
- Ports free: 6333 (Qdrant HTTP), 6334 (Qdrant gRPC), 6379 (Redis), 8000 (RAG)
- `PARSER_TOKEN` env set (≥32 chars) — generate with `openssl rand -hex 32`
- `SHARED_STORAGE_PATH` exists and is writable
- `rag/models/bge-m3/` populated (for embedding; without it, dummy-mode is acceptable for smoke)

## Steps

### 1. Start infrastructure

```bash
cd deployment
docker compose up -d qdrant redis
# Wait for healthchecks
docker compose ps  # both should show "(healthy)"
```

### 2. Start RAG service (locally, not via docker)

```bash
cd ../rag
# In another terminal, or background:
PARSER_TOKEN=$(openssl rand -hex 32) \
SHARED_STORAGE_PATH=/tmp/ekrs-shared \
QDRANT_HOST=localhost \
REDIS_URL=redis://localhost:6379 \
EKRS_DEBUG=true \
uvicorn ekrs_rag.main:app --host 0.0.0.0 --port 8000
```

Wait for `Uvicorn running on http://0.0.0.0:8000`.

### 3. Health probes

```bash
# /healthz should return JSON with qdrant+redis status
curl -s http://localhost:8000/healthz | jq .

# /metrics sidecar (Phase 5.5 D exporter on :9090)
curl -s http://localhost:9090/metrics | head -20
```

Expected: `status: ok`, `qdrant: reachable`, `redis: reachable`, no errors.

### 4. Ingestion smoke (A1 path)

```bash
# Use mock-notify script (or write a tiny JSONL fixture)
make mock-notify
# Check task status
curl -s -H "X-Parser-Token: $PARSER_TOKEN" \
     "http://localhost:8000/v1/ingestion/status?doc_hash=..." | jq .
```

Expected: `status: completed`, `chunks_indexed: N`.

### 5. Constraints query smoke (V2 IR path)

```bash
curl -s -X POST http://localhost:8000/v1/constraints \
     -H "Content-Type: application/json" \
     -d '{"query": "高温环境温度上限", "scope_path": ["national"], "strict": false}' | jq .
```

Expected: branches with feasible ranges, source citations, no 500s.

### 6. Audit log inspection

```bash
# Phase 5.5 F: audit.log rotation 100MB x 5 gzip
ls -lh audit.log audit.index
# Tail a few events
tail -5 audit.log | jq .
```

Expected: events for `query_received`, `retrieval_completed`, `constraint_solved`, `ingestion_completed` (at minimum). Phase 6A fixes ensure `qdrant_write_failed` emits on Qdrant errors (T8 fix at d21e6d4).

### 7. Cleanup

```bash
cd ../deployment
docker compose down
```

## Pass criteria

- All 6 steps complete without 5xx errors
- `/healthz` returns `qdrant: reachable, redis: reachable`
- At least 4 audit event types observed in audit.log (covers Phase 6A emit fixes)
- Constraints query returns branches (not empty, not error) when scope_path matches indexed docs

## Failure handling

If a step fails, capture:

1. Full curl output + `-v` headers
2. Last 50 lines of uvicorn stderr
3. Last 50 lines of audit.log (if it exists)
4. `docker compose ps` (if containers exited)

Then file a T14 follow-up finding against `docs/superpowers/plans/2026-07-21-ekrs-integration-fixes-review.md` so it can be triaged in the next planning round.

## Why this was deferred in this session

The Phase 6C scope approved all 4 tasks. T1+T2 (mypy cleanup) shipped at commit `f2fe79e`. T3 (fixture convention doc) shipped at commit `bff9bda`. T4 requires docker-compose to start Qdrant + Redis + RAG, which needs to pull `qdrant/qdrant:latest` and `redis:7-alpine` from docker.io. The current network condition is an IPv6 dial timeout to `registry-1.docker.io`, and no local images are cached. T4 is therefore a no-op for the automated session and needs a human with working docker network access.

**Estimated wall-clock for a human**: ~15 minutes once network is restored (image pulls + container startup + uvicorn startup + smoke curls).
