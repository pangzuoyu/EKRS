# Phase 13c-C13 — Re-ingest corpus via GPU

## Context

Phase 13c closed (commit 7c1865a). dev_ui_v2 X-Parser-Token patch shipped (commit 735c2fa). UI now correctly attaches the header; backend accepts and solver runs. But solver returns 409 with empty corpus — Phase 12 745-bundle corpus wiped when rag container was recreated during audit_bridge fix.

User wants to re-ingest corpus through GPU path. Corpus now lives at `/media/pangzy/F8A6CB1CA6CAD9F0/text` (3809 bundles total, includes 745 from Phase 12 + historical/test). Old script path `/home/pangzy/code_project/doc-to-md/output/text` is gone (only 22 bundles remain in adjacent `doc-to-md/text`).

GPU host: RTX 4070 + CUDA 13.0 + Driver 580.173, ready.
bge-m3 model: pytorch_model.bin + sparse_linear.pt + 1_Pooling + colbert_linear.pt all present at `/home/pangzy/code_project/bge-m3/`. No code-side blocker for GPU path.

## Scope (3 步)

### 1. Script patch — add `--corpus` flag (5 LOC additive)

`scripts/task_d_mvp_reingest.py`:
- `ap.add_argument("--corpus", type=Path, default=CORPUS_ROOT, ...)` (line 202 area)
- Change line 232 `pick_bundles(CORPUS_ROOT, ...)` → `pick_bundles(args.corpus, ...)`
- Default = `CORPUS_ROOT` constant (back-compat for Phase 12 reruns)

Reason: script hardcoded path gone. Additive flag preserves existing default + adds override. No risk.

### 2. GPU service up + re-ingest

```bash
cd /home/pangzy/code_project/EKRS
make gpu-up                    # stops CPU rag, starts rag-gpu (port 8001)
# verify healthy
curl -s http://localhost:8001/healthz | jq .
# nvidia-smi inside container
docker exec deployment-rag-gpu-1 python -c "import torch; print(torch.cuda.is_available(), torch.__version__)"

# run re-ingest (size N = user choice, see UQ-1)
python scripts/task_d_mvp_reingest.py \
  --corpus /media/pangzy/F8A6CB1CA6CAD9F0/text \
  --token-env PARSER_TOKEN \
  --version 2 \
  --limit N \
  --reset-checkpoint
```

### 3. Verify + restore CPU

```bash
# verify Qdrant count
curl -s -X POST http://localhost:8001/v1/admin/embedding-cache/flush -H "X-Admin-Key: $ADMIN_KEY"
docker exec deployment-qdrant-1 python -c "
from qdrant_client import QdrantClient
c = QdrantClient(host='localhost', port=6333)
print('points:', c.count('ekrs_documents').count)
"
# spot-check recall
curl -s -X POST http://localhost:8001/v1/constraints \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"高温环境温度限制","context":{},"strict":false,"top_k":40}' | jq '.detail.primary_branch, (.detail.conflicts|length)'

make gpu-down                  # stops rag-gpu, restarts CPU rag for normal ops
```

## Time estimate (sequential, GPU p99 ~8s per bundle)

| N | ETA |
|---|---|
| 745 (Phase 12 number) | ~100 min |
| 1500 (half of disk) | ~3.3 hours |
| 3809 (full disk) | ~8.5 hours |

## Unresolved Questions

| UQ | Question | Default if no answer |
|---|---|---|
| **UQ-1** | Re-ingest scope: 745 / 1500 / 3809? | **3809** = full disk (user explicitly said "完整 corpus") |
| **UQ-2** | Run in foreground (terminal-blocking) or background (bash run_in_background)? | foreground for 745 (≤2h OK); background for 3809 |
| **UQ-3** | Save re-ingest report to deployment/? | yes (deployment/phase13c-c13-reingest-{timestamp}.log) |
| **UQ-4** | After re-ingest, leave GPU service up or restore CPU? | restore CPU (gpudown at end) |
| **UQ-5** | Spot-check criteria for "success": (a) Qdrant count == ingest count, (b) /v1/constraints returns non-409 with ≥1 branch | both required |

## Files

- Modified: `scripts/task_d_mvp_reingest.py` (+5 LOC)
- New: `deployment/phase13c-c13-reingest-{timestamp}.log` (run output)
- Memory: `phase13c-c13-corpus-reingest-gpu.md` after success

## Out of scope

- Don't vendor bge-m3 into image (Phase 13b D1-A PoC stays as-is)
- Don't migrate corpus from external drive to local disk (one-shot re-ingest)
- Don't modify Phase 13a/13b/13c baselines (absorb under phase13c incremental)
- Don't run T5.2 equiv / T5.3 failover (independent follow-ups)