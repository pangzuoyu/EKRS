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

## P0 fix sequence (2026-08-19 21:24, applied)

User authorized "立即接受修复方案 · 清空损坏数据 · 从 Checkpoint 重跑" after diagnosing that 49 of 50 chunks silently overwrote each other due to point_id collision in `qdrant_client.py:203-206`. Fix added `chunk_id` to UUID5 input so multi-chunk docs get N unique point_ids.

### What was done (sequence took ~10 minutes wall-clock)

1. **`docker cp` to correct editable-install path**: Container loads from `/app/rag/ekrs_rag/...` (editable pip install), NOT `/usr/local/lib/python3.11/site-packages/...`. Previous cp attempt went to wrong path. Verified via `inspect.getsource()` showing the new fix string.
2. **Cleared stale .pyc**: `rm /app/rag/ekrs_rag/retrieval/__pycache__/qdrant_client.cpython-311.pyc` (Python 3.11 cache; 3.13 cache exists separately and is unused).
3. **Restart rag**: `cd deployment && docker compose restart rag` — picked up new module.
4. **Verified fix loaded**: `inspect.getsource(QdrantManager.upsert_chunks)` now contains "P0 fix" comment.
5. **Deleted v=2 corrupted data**: 2 v=2 points (collision artifacts, 30–37 char text each) deleted via `models.FilterSelector`. v=1 data (3625 points) preserved as Phase 9 baseline.
6. **Cleared TaskRepo**: 134 COMPLETED + 7 FAILED records deleted from `/var/lib/ekrs/tasks.db` (column `doc_id` not `doc_hash`; `version` not in tasks table — version lives in payload/callback).
7. **Reset checkpoint**: `rm /tmp/task_d_full.json` so script re-processes from 0.
8. **Re-launched**: `/tmp/run_task_d.sh` (wrapper that loads `PARSER_TOKEN` from `.env` without leaking to bash history) + `--reset-checkpoint --limit 745 --version 2 --pace 5 --retry 2 --status-timeout 600`.

### Verification (first 3 bundles re-ingested)

- Bundle `000150f86cdbc3c1` (the multi-chunk table doc): **48 unique v=2 points** in Qdrant (was 1 before fix — silent data loss).
- Bundle `0056214d13631114` (single-chunk doc): 1 point (unchanged, correct).
- Qdrant v=2 total after 3 bundles: 50 points (= 48 + 1 + 1).
- Script logs: `[1/745] 000150f86cdb... HTTP=202 → success chunks=48 (69.2s)` — chunks_indexed now matches actual chunk count.

### Carry-forward lessons

- **Editable-install paths inside containers**: Always verify with `python -c "import X; print(X.__file__)"` before docker cp. `pip install -e .` creates an editable install where Python loads from the source dir, not site-packages. The site-packages path has a `__init__.py` stub but Python follows the editable pointer.
- **`.pyc` caches**: Both py3.11 AND py3.13 caches may exist (`.cpython-311.pyc`, `.cpython-313.pyc`). Only the one matching the runtime Python is used; clearing the wrong one is a no-op.
- **Qdrant FilterSelector dict vs model**: `delete(points_selector={"filter": ...})` raises "Unsupported points selector type: dict". Must use `models.FilterSelector(filter=models.Filter(...))` explicitly.
- **Auto-mode classifier + PARSER_TOKEN**: Use a wrapper script (`/tmp/run_task_d.sh` with `set -a; . .env; set +a`) to load the token into the python script's env without materializing it in interactive bash.
- **"unhealthy" but processing is normal**: docker healthcheck `/dev/tcp` probe can return 000 transiently during bge-m3 encoding saturation. CPU 100% + memory steady = NOT a wedge. Use script log progress as the load-bearing monitor, not container healthcheck status.

## Morning verification checklist (Task #38, post-Task #37)

Expected completion: ~07:00–09:00 next day. Run from host shell.

```bash
# 1. Confirm run state (expect ~745 completed, low single-digit failed)
tail -10 /tmp/task_d_run.log
cat /tmp/task_d_full.json | python3 -c "import sys, json; d=json.load(sys.stdin); print(f'completed={len(d[\"completed\"])} failed={len(d[\"failed\"])}')"

# 2. Qdrant total v=2 points (expect ~745×30 ≈ 22k, NOT just 745)
docker exec deployment-rag-1 python -c "
from qdrant_client import QdrantClient
c = QdrantClient(host='qdrant', port=6333)
v2 = c.count(collection_name='rag_documents', count_filter={'must':[{'key':'version','match':{'value':2}}]}).count
v1 = c.count(collection_name='rag_documents', count_filter={'must':[{'key':'version','match':{'value':1}}]}).count
total = c.count(collection_name='rag_documents').count
print(f'total={total}  v1={v1}  v2={v2}')
"
# Note: collection name is 'rag_documents', NOT 'rag_collection_v2'

# 3. Sample a multi-chunk doc — expect N points, where N = chunks_indexed from logs
docker exec deployment-rag-1 python -c "
from qdrant_client import QdrantClient
c = QdrantClient(host='qdrant', port=6333)
n = c.count(collection_name='rag_documents', count_filter={'must':[{'key':'doc_hash','match':{'value':'000150f86cdbc3c1'}},{'key':'version','match':{'value':2}}]}).count
print(f'000150f86cdbc3c1 v=2 points: {n}  (expect 48)')
"

# 4. Verify metadata fields on a sample point
docker exec deployment-rag-1 python -c "
from qdrant_client import QdrantClient
c = QdrantClient(host='qdrant', port=6333)
res, _ = c.scroll(collection_name='rag_documents', scroll_filter={'must':[{'key':'doc_hash','match':{'value':'000150f86cdbc3c1'}},{'key':'version','match':{'value':2}}]}, limit=1, with_payload=True, with_vectors=False)
p = res[0].payload
for k in ['doc_type','chunk_id','scope_path','source_block_ids','form_fields','column_headers']:
    v = p.get(k, '<MISSING>')
    print(f'  {k}: {v}')
"

# 5. Write verification report to deployment/phase12-task-d-verification.md
```

Success criteria:
- v=2 ≈ 22k points (not 745)
- multi-chunk doc count matches its chunks_indexed from log
- doc_type/chunk_id/scope_path/form_fields/column_headers all present
- heading_path is NOT a Qdrant payload field by design — it translates to scope_path via R4/R7 spec

## Optimization: bge-m3 intra_op_num_threads 1 → 4 (2026-08-19 22:36)

User confirmed single-core saturation observation. Diagnosed root cause: `onnx_bge_m3.py:88` hardcoded `intra_op_num_threads=1` (matched BGEM3FlagModel default). Batch encoding was already in place via `OnnxBgeM3.encode(list(texts))` — bottleneck was per-op thread pool, not batching.

### Change
- `sess_opts.intra_op_num_threads = 4` (was 1)
- 1-line change + docker cp to `/app/rag/...` + clear .pyc + `docker compose restart rag`
- Commit: `1865168 perf(retrieval): bge-m3 intra_op_num_threads 1→4`

### Live verification (10 bundles sampled post-restart)

| Metric | Pre-change (1 thread) | Post-change (4 threads) | Δ |
|---|---|---|---|
| CPU during inference | 100% (1 core) | 402% (4 cores) | +4x |
| Memory | 3.8–11.7GB growing | 4.75GB steady | flat ✅ |
| 30-chunk bundle avg | 50s | ~20s | 2.5x |
| 41-chunk bundle | ~70s | 18.8s | 3.7x |
| chunks/s | ~0.6 | ~1.85 | 3x |
| ETA (745 bundles) | ~9.5h | ~4h | 2.4x |

Memory headroom healthy: 4.75GB used / 20GB limit. Pre-change peak was 11.7GB so +1GB scaling for 4-thread ops is well within budget.

### Caveats
- Aspirated 3–5x speedup from bench was not fully realized; intra-op parallelism only helps matmul/attention ops, not tokenization or batch-padding overhead.
- If memory exceeds 14GB during long runs, scale back to 2 or revert to 1.
- Comment in code references this verification; future operators see the rationale.

### Why user authorized mid-run change
- Single-line blast radius, restart is auto-recovered by script `--retry 2`
- Memory risk low (+1–2GB on +15GB headroom)
- 9.5h ETA → ~4h halves overnight run window
- Pre-authorized for "明天 Task #40 benchmark"; mid-run A/B validated the same hypothesis with real data