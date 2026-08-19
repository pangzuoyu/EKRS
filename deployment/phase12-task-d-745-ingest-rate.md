---
name: phase12-task-d-745-ingest-rate
description: 745-bundle doc-to-md ingest via task_d_mvp_reingest.py runs at ~50s/bundle avg (0.02 docs/s) due to bge-m3 encoding 30+ chunks per 1-block doc. PIDs accumulate slowly (1/bundle). 12h ETA, no wedge when pace=5s.
metadata:
  node_type: memory
  type: project
  originSessionId: eaa03379-f5da-4ae9-bb43-7dfef06f6ef2
  modified: 2026-08-19T10:14:04.503Z
---

# Phase 12 Task D 745-bundle ingest — observed rate

**Run started**: 2026-08-19 17:30 (background, ID buivz1ezs after restart from first wedge)
**Resumed via checkpoint**: 15 completed + 4 failed from initial run (730 pending)

## Throughput finding

Per-bundle latency range 0.1s–90s, **avg ~50s**. Distribution:
- ~30% under 5s (1-chunk docs)
- ~40% 5–30s (5–10 chunk docs)
- ~30% 30–90s (30+ chunk docs)

## Root cause of slow rate

Doc-to-md bundles have 1 raw block each (8–10 KB text). The chunker splits these into **30+ fine-grained chunks** (~222 chars each) due to Chinese-text boundary detection. bge-m3 encodes each chunk independently — 30 chunks × ~2s = 60s per bundle.

This is intrinsic to the workload (1 block → 33 chunks), NOT a pipeline wedge.

## PIDs (process accumulation) — stable

- After 20 bundles: 119 PIDs
- After 30 bundles: 129 PIDs (+1 PID/bundle avg)
- Memory steady at 3.8 GB (well below 12.3 GB wedge peak)
- CPU pegged at 100% (bge-m3 ONNX bound)
- Healthz 200 OK throughout — NOT wedged

## Comparison vs MVP smoke (5 bundles)

- MVP smoke: 5 bundles in 150s (avg 30s, but mostly the 30s pacing delay)
- Real workload: pace=5s, but processing dominates (not pacing)

## Recovery from initial wedge (commit 15cf1ee run)

- Initial run with pace=2s hit NOTIFY FAIL HTTP=0 cascade at bundle 14
- Container went unhealthy, curl healthz returned 000
- Cause: uvicorn accept queue blocked by bge-m3 processing
- Recovery: `docker compose restart rag` → CPU 0.33%, mem 1.7 GB, PIDs 44
- Resumed with pace=5, retry=2, status-timeout=180 → stable

## Pre-filter saved significant time

`_bundle_has_content()` in pick_bundles skips 206/3809 all-empty bundles (5.4% of corpus). Without filter: 206 × 600s = 34h wasted polling.

## Open follow-ups

- 12h ETA — Task #37 completion expected ~05:30 next morning
- Task #38 (verify Qdrant payloads + count + write report) runs after Task #37 completes
- Consider lowering max-chunks-per-bundle filter to skip >50-chunk docs (would accelerate by ~30% but loses data)

## Optimization opportunity — bge-m3 batch encoding (Task D+, post-completion)

**Observation**: Per-bundle latency correlates with chunk count. 1-chunk bundles = 0.1s, 30-chunk bundles = 50–90s. Suggests current code path is per-chunk sequential encode, not batch.

**Hypothesis**: bge-m3 ONNX typically supports batch inference with 3–5× per-item speedup (single-item overhead dominates at small batch). If pipeline currently calls `for chunk in chunks: embedding_service.encode(chunk.text)`, switching to `embedding_service.encode([c.text for c in chunks])` (batch) could compress 30-chunk bundle from ~50s to ~12s.

**Total ETA impact**: 10h → 2–3h for full 745.

**Plan** (after Task #37 completes):
1. Pick 1 representative 30-chunk bundle from corpus
2. Write micro-bench: batch vs per-chunk wall time + memory peak
3. If batch wins (>2×): design patch to embedding_service.py + pipeline.py
4. If patch viable: commit + new ingest runs benefit
5. If not: document findings + recommendation

**Status**: Pending Task #37 completion. Tracked as Task #40 in task list.

**DO NOT** execute or touch `embedding_service.py` while Task #37 is in flight — risk of corrupting current 745-ingest state.