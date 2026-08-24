# Phase 13a Rollout Plan — CPU-only production + GPU 10%→100% gate

## 1. Scope

Phase 13a ships production-ready CPU-only ingestion with the new
pebble subprocess dispatch, admission gates, audit/metrics surface,
and encode-backend seam for Phase 13b/c GPU replacement.

This document covers:
- **§2** CPU-only production rollout (immediate, post-13a-closure)
- **§3** GPU 10%→100% rollout (gated on Phase 13b/c completion)
- **§4** Rollback procedures for both paths

Operations-team owns §2/§3 traffic-split actions; this doc is the
gate criteria + sequencing reference. The 13a code/config itself
is production-ready (T10.1 + T10.2 acceptance verified, see plan
`docs/superpowers/plans/2026-08-23-phase13a-production-readiness.md`).

## 2. CPU-only production rollout (post-13a)

### 2.1 Pre-flight checks

```bash
# Stack healthy
docker compose ps   # rag + redis + qdrant all "healthy"
curl -fsS http://localhost:8000/healthz | jq .
curl -fsS http://localhost:8000/ready | jq .

# 13a acceptance re-run (one-liner regression)
PARSER_TOKEN=$PARSER_TOKEN \
  /home/pangzy/miniconda3/bin/python scripts/phase13a_t10_e2e.py
# Expect: ALL T10.1 CHECKS PASS ✓

PARSER_TOKEN=$PARSER_TOKEN \
  /home/pangzy/miniconda3/bin/python scripts/phase13a_t10_2_drift.py
# Expect: DRIFT CHECK PASS ✓
```

### 2.2 Canaries to watch (first 24h)

Prometheus metrics (Phase 5.5 D sidecar :9090):

| Metric | Expected baseline | Action threshold |
|--------|-------------------|------------------|
| `ekrs_ingestion_queue_depth` | <5 | >20 sustained 5min |
| `ekrs_task_duration_seconds` p99 | <120s | >300s |
| `ekrs_admission_rejected_total` | 0 | spike >10/min |
| `ekrs_task_timeout_killed_total` | 0 | >0 (pebble subprocess bug) |
| `ekrs_callback_failure_total` | 0 | >5/min |
| `ekrs_index_consistency_drift_total` | 0 | >0 (paired-write bug) |
| `/healthz` P99 latency | <10ms | >50ms |
| `/ready` P99 latency | <200ms | >500ms |

### 2.3 Rollback (CPU path)

If a 13a-introduced regression surfaces:

```bash
# 1. Stop traffic (point parser at last-known-good 13a-pre tag)
docker compose down
git checkout <phase12-or-earlier-tag> -- rag/ shared/ deployment/
docker compose build rag && docker compose up -d
# 2. Replay-mode catch-up: the old code path is preserved for replay
#    via pipeline.ingest (Phase 13a Pre-Task A kept both code paths
#    consuming the same _prepare_step5 + _run_step5 helper).
```

The `_prepare_step5` + `_run_step5` helper (Phase 13a Pre-Task A)
means replay mode runs the SAME logic the new subprocess runs, so
no semantic drift on rollback — only the dispatch mechanism changes
(sync in-process → pebble subprocess).

## 3. GPU 10%→100% rollout (gated on 13b/c)

### 3.1 Pre-conditions

- Phase 13b (GPU container, torch FP16) shipped with all 10 GPU
  spec §8 acceptance criteria green
- Phase 13c (encode backend rebinding) shipped with T9 seam wired
- CPU 100% baseline stable for ≥7 days
- Feature flag `GPU_CHANNEL_ENABLED` (added in 13c) defaults OFF

### 3.2 10% canary procedure

```bash
# 1. Enable GPU channel for the canary RAG instance only
#    (traffic split managed by upstream load balancer / service mesh)
GPU_CHANNEL_ENABLED=true docker compose up -d rag-canary

# 2. Route 10% of parser notifications to rag-canary:
#    The parser's /v1/ingestion/notify target URL is per-cluster.
#    Use the LB to point 10% of canary traffic at the GPU instance.

# 3. Watch GPU-canary metrics (first 1h, then relax to 5min polling):
#    - GPU encoding latency p99 vs CPU baseline (expect ≤0.5x)
#    - GPU encode error rate (must stay <0.1%)
#    - Retrieval equivalence (golden set 50 + recall@10 script)
```

### 3.3 10% → 100% gates (must ALL hold for 24h)

| Gate | Criterion | Tool |
|------|-----------|------|
| G1 | GPU encode p99 latency ≤ 0.5x CPU baseline | `ekrs_task_duration_seconds` |
| G2 | GPU encode error rate <0.1% | `ekrs_task_timeout_killed_total` |
| G3 | Retrieval equivalence (golden 50 = CPU baseline) | `tests/golden_set/` |
| G4 | FTS↔Qdrant paired writes (drift stays 0) | `ekrs_index_consistency_drift_total` |
| G5 | /healthz P99 <10ms (encode offloaded, /healthz even faster) | `/healthz` probe loop |
| G6 | Audit parity (all 24 events emit on GPU path) | audit.log grep |
| G7 | No GPU OOM in 24h | pod logs |
| G8 | Callback success rate = 100% | `ekrs_callback_failure_total` |
| G9 | Cluster resource headroom (GPU mem <80%) | node exporter |
| G10 | Rollback drill (set flag false, verify CPU path takes over) | manual |

### 3.4 Promotion procedure

```bash
# Step-by-step 25% → 50% → 100%, holding each step for ≥24h
# 1. Bump traffic share to 25% (LB weight adjustment)
# 2. Hold 24h; re-run all 10 gates; if all green, continue
# 3. Bump to 50%; hold 24h; re-run gates
# 4. Bump to 100%; hold 24h; re-run gates
# 5. Flip default: GPU_CHANNEL_ENABLED=true in .env.example
# 6. Tag GPU-promotion commit
```

### 3.5 Rollback (GPU → CPU)

```bash
# Instant rollback: set flag false, restart rag
GPU_CHANNEL_ENABLED=false docker compose restart rag

# Verify CPU path took over (audit.log should show CPU metrics)
# No data migration needed — vectors are deterministic dense floats
# from bge-m3 ONNX (same model) vs torch FP16 (numerically equivalent
# within bge-m3 tolerance; recall gate G3 confirms semantic equivalence)
```

## 4. Open questions (deferred to 13b/c execution)

- Q1: GPU instance type and per-pod memory budget (depends on 13b
  container image size — torch FP16 bge-m3 is ~600MB vs ONNX ~300MB).
- Q2: Whether `GPU_CHANNEL_ENABLED` should be per-document (skip GPU
  for small docs to avoid warmup latency tax) or global. Current
  plan assumes global; 13b may surface a size-based heuristic.
- Q3: LB-level vs RAG-level traffic split. Current plan assumes LB
  splits at the parser→RAG boundary (parser-side decision).

---

**Status**: Phase 13a T10.3 written 2026-08-24, post-T10.1/T10.2
acceptance. Authoritative procedure lives here; operations team
should treat this as the gate criteria document for both rollouts.