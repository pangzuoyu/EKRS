# Phase 13c Ops Guide — GPU 通道 + Audit/Stale Cleanup Production Runbook

> Phase 13b GPU bge-m3 (closed 43e81d9) + Phase 13c production-readiness
> (T1 cross-process AuditWriter, T2 mark_process_dead + stale cleanup,
> T3 get_status FAILED bug fix, T4 dynamic bench threshold).
>
> 目标读者: ops on-call / SRE。30min 内 0-疑问启停 GPU 服务。

---

## 1. 前置条件 (Pre-conditions)

### 1.1 硬件 / 驱动

| 项 | 要求 | 验证 |
|---|---|---|
| GPU | NVIDIA RTX 30/40 系列, ≥8GB VRAM | `nvidia-smi` 看 device 0 |
| Driver | ≥ 535 (CUDA 13 兼容) | `nvidia-smi` 顶部 CUDA Version |
| CUDA | 13.0 (host runtime 即可, 不需 toolkit) | `nvcc --version` 可选 |
| Container toolkit | nvidia-container-toolkit ≥ 1.14 | `docker info \| grep -i nvidia` |
| RAM | ≥32GB (host bge-m3 加载 ~3GB + worker × N) | `free -h` |

### 1.2 软件

| 项 | 要求 | 验证 |
|---|---|---|
| Docker Engine | ≥ 24.0 | `docker --version` |
| Docker Compose | v2.20+ (支持 `profiles`) | `docker compose version` |
| NVIDIA 容器运行时 | enabled | `docker run --rm --runtime=nvidia nvidia/cuda:13.0-base nvidia-smi` |
| host bge-m3 权重 | `/home/pangzy/code_project/bge-m3/` (FP32 pytorch_model.bin) | `ls -la /home/pangzy/code_project/bge-m3` |

### 1.3 环境变量

`.env` 必须含:
- `PARSER_TOKEN` ≥32 chars (Phase 13a T3 hard gate, 启动时拒绝 placeholder)
- `ADMIN_KEY` ≥16 chars
- `SHARED_STORAGE_PATH=/app/storage`
- `QDRANT_HOST=qdrant`, `QDRANT_GRANT_GRPC_PORT=6334`
- `REDIS_URL=redis://redis:6379/0`

`.env` GPU 覆盖 (写在 `docker-compose.override.yml`, **不要 commit 真密钥**):
- `BGE_M3_GPU_ENABLED=true`
- `BGE_M3_GPU_PROBE_ENABLED=true`
- `BGE_M3_MODEL_DIR=/opt/ekrs/models/bge-m3-torch` (bind-mount 路径)
- `BGE_M3_GPU_PROBE_INTERVAL_S=5` (T5.3 failover 加速)

---

## 2. 构建 (Build)

### 2.1 GPU image (~3GB, 3-5min)

```bash
cd deployment
docker compose --profile gpu build rag-gpu
```

预期日志:
- `#10 DONE 5.4kB` (shared install)
- `#15 DONE 1.2GB` (rag + onnx vendor)
- `#20 DONE 1.8GB` (torch CUDA wheel)
- `#25 DONE 0.5MB` (sentencepiece — Phase 13b 修复, 否则 `_self_check` fallback to dummy)

**Gotcha**: 若 host 离线 (无外网 pypi/torch index), 用 `docker-compose.override.yml` 配 `PYTHON_BASE_IMAGE` / `PIP_INDEX_URL` / `TORCH_INDEX_URL` 指向内网 mirror (daocloud.io / 清华 / 阿里)。

### 2.2 host 权重 precheck (Makefile 帮你做)

```bash
make gpu-up
# 头部自动 `test -d /home/pangzy/code_project/bge-m3`, 缺失立即 exit 1。
# bind-mount 路径错误不会在 build 时炸, 在 container start 时炸 — precheck 省时间。
```

---

## 3. 启动 (Startup)

### 3.1 一键起 (含 CPU rag 自动停)

```bash
make gpu-up
```

实际执行:
1. `test -d /home/pangzy/code_project/bge-m3` (前置)
2. `docker compose stop rag` (停 CPU, 避免 Qdrant wipe 冲突)
3. `docker compose --profile gpu up -d rag-gpu`
4. 30 次轮询 `/healthz`, 命中 `status: ok` 退出

预期日志:
```
[+] Running 2/2
 ✔ Container deployment-rag-gpu-1  Started
Waiting for rag-gpu healthz...
rag-gpu healthy
```

### 3.2 验证 GPU + audit bridge + stale cleanup 都在跑

```bash
# CUDA 可用性
docker exec deployment-rag-gpu-1 python -c "import torch; print(torch.cuda.is_available(), torch.__version__)"
# → True 2.11.0+cu130

# GPU 内存 (首次 idle, 都 0)
curl -s -X POST http://localhost:8001/v1/admin/gpu/memory-stats \
  -H "X-Admin-Key: $ADMIN_KEY" | jq .
# → {"peak_bytes": 0, "current_bytes": 0, "device_id": 0}

# Audit bridge 已起 + EKRS_AUDIT_QUEUE_ADDR 已 export
docker exec deployment-rag-gpu-1 env | grep EKRS_AUDIT
# → EKRS_AUDIT_QUEUE_ADDR=/tmp/...  (Manager address)

# Stale cleanup 后台任务在跑
docker exec deployment-rag-gpu-1 ps -ef | grep -E "python.*main" | head -3
# (single process; cleanup 走 asyncio.create_task 在事件循环内)
```

---

## 4. 验收 (Acceptance)

### 4.1 Phase 13b T5.1 28-doc bench (Phase 13c T4 dynamic threshold)

```bash
make gpu-acceptance
# → 容器内跑 phase13b_poc_bench.py --phase full
```

**关键输出解读**:

| 指标 | ship 阈值 | 解读 |
|---|---|---|
| `failure_rate` | == 0 | GPU 路径 OK |
| `chunks_indexed` (Phase B total) | ≥ `corpus_total * 0.9` (默认 warning, exit 0) | 阈值动态化 (T4) |
| `gpu_memory_peak_bytes` | < 6GB | 健康 |
| `largest_doc_ms` | < 30s | 单篇 doc 延迟 OK |
| `audit.log` 含 `channel_switched` | ≥ 1 条 | **T1 闭环** (worker 路径通了) |
| `/metrics` 含 `gpu_memory_peak_bytes` | 非 stale 大值 | **T2 通了** (mark_process_dead 生效) |

**退出码**:
- `pass` → exit 0 (一切正常)
- `warn` → exit 0 (chunk 低于阈值, 非 STRICT 模式)
- `fail` → exit 1 (largest doc 超时 / n_failed > 0 / STRICT 模式低于阈值)

**Phase 13c T4 STRICT mode** (生产预发布 gate):
```bash
T5_PHASE_B_MIN_CHUNKS_STRICT=1 make gpu-acceptance
# 低于阈值 → hard fail, exit 1
```

### 4.2 端到端 ingestion smoke (官方推荐)

提交一个真 bundle → 看 audit.log + /metrics:

```bash
# 提交 (走容器内 RAG_URL=http://localhost:8000 = GPU 服务)
make mock-notify

# 看 audit.log
tail -f deployment/rag_audit.log | grep -E "channel_switched|fts_synced|fts_searched"
# 预期:
#   {"event": "channel_switched", "from_channel": "unknown", "to_channel": "gpu", ...}
#   {"event": "fts_synced", "doc_hash": "...", "chunks_written": 42, ...}

# 看 /metrics 聚合
curl -s http://localhost:9090/metrics | grep -E "gpu_memory|encode_latency" | head -10
# 预期: gpu_memory_used_bytes{device_id="0"} < 6e9, gpu_encode_latency_seconds_count > 0
```

### 4.3 Phase 13c T3 get_status FAILED 验证 (回归测试)

提交故意失败的文档 (空 JSONL):
```bash
# /v1/ingestion/notify with empty blocks → chunker no_chunks → FAILED
curl -X POST http://localhost:8001/v1/ingestion/notify \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"doc_hash": "test_empty_001", "version": 1, "output_path": "/tmp/empty/"}'
# → 202 (notify accepted, drain 异步)

# /v1/ingestion/status
curl -s http://localhost:8001/v1/ingestion/status/test_empty_001 \
  -H "X-Parser-Token: $PARSER_TOKEN" | jq .
# 预期 (Phase 13c T3 fix): {"status": "failed", "error": "no_chunks", ...}
# ❌ pre-13c bug: {"status": "pending", ...}  (FAILED 被误报 pending)
```

---

## 5. 常见故障 (Troubleshooting)

### 5.1 `sentencepiece not installed` (Phase 13b 已修)

**症状**: `transformers.AutoTokenizer.from_pretrained` 抛 `You need to have sentencepiece or tiktoken installed`, EncodingRouter 看不到 GPU encoder → 路由到 ONNX CPU。

**修法**: 重建 image (`make gpu-up` 自动 rebuild)。验证 `Dockerfile.gpu:45-47` 有 `pip install sentencepiece>=0.2.0`。

### 5.2 `min cosine below threshold`

**症状**: `_self_check` 失败, GPU channel 不注册, router fallback to CPU。

**原因**:
1. probes fixture 缺失 → 检查 `rag/tests/fixtures/bge_m3_self_check_probes.jsonl` 在 image 里
2. FP16 vs FP32 噪点 → `torch_bge_m3.py:102` `_SELF_CHECK_COSINE_THRESHOLD = 0.99` (Phase 13b 修复, 原 0.999 太严)
3. CPU baseline 是 dummy (无 ONNX) → 验证 `rag/models/bge-m3/` 已 vendor 进 image

**验证**:
```bash
docker exec deployment-rag-gpu-1 ls /opt/ekrs/models/bge-m3/
# 预期: pytorch_model.bin + sparse_linear.pt + tokenizer.json + onnx/ (Phase 13b)
```

### 5.3 `gpu_memory_used 一直 0`

**症状**: nvidia-smi 显示 GPU util 0%, 但 /v1/admin/gpu/memory-stats 一直 0。

**根因**: 5s nvidia-smi polling 错过 sub-second spike (FP16 batch=32 ≈ 50ms × 13 batches ≈ 650ms)。用 1s polling:

```bash
watch -n 1 nvidia-smi
# 等 5-10s, 看 util spike 到 80-100%
```

**Phase 13c T4 /metrics**: `gpu_memory_used_bytes` 来自 `torch.cuda.memory_allocated()` (in-process) — 比 nvidia-smi 准。

### 5.4 `notify duplicate` (Phase 13b 已修)

**症状**: 同一 doc_hash 第二次 notify 返 `{"status": "duplicate"}` 但实际 chunks 还在 ingesting。

**根因**: pre-T5.1 bench `build_notify_payload` 用固定 `version=1`, 但第一次 bench 写 Qdrant 后, 第二次 reuse 同 (doc_hash, version) → Qdrant skip。

**修法**: `scripts/_phase13b_common.py:build_notify_payload` `version: int | None = None` default `int(time.time())` — 每次 fresh bench 跑唯一 version。验证 bench log `payload version` 不再 const 1。

### 5.5 `audit.log 没有 channel_switched`

**症状**: worker 路径跑了 encode, 但 audit.log 没事件。

**根因** (pre-T1): worker 无 AuditWriter, `_emit_channel_switched` 走 `get_writer()` 返 None → silent drop。

**Phase 13c T1 修法验证**:
```bash
# 1. Bridge started?
docker exec deployment-rag-gpu-1 env | grep EKRS_AUDIT_QUEUE_ADDR
# 预期: 非空 (Manager address)

# 2. 触发 channel_switched (force_re_register_gpu)
curl -s -X POST http://localhost:8001/v1/admin/gpu/invalidate \
  -H "X-Admin-Key: $ADMIN_KEY" | jq .

# 3. 看 audit.log
tail -f deployment/rag_audit.log | grep channel_switched
# 预期: {"event": "channel_switched", "from_channel": "gpu", "to_channel": "cpu", ...}
```

### 5.6 `/metrics` 显示 stale 巨大 GPU peak

**症状**: worker 早就死, 但 `/metrics` 还显示 `gpu_memory_peak_bytes = 5000000000` (5GB)。

**根因** (pre-T2): worker SIGKILL 后 .db 文件留尸, sidecar exporter 读 stale。

**Phase 13c T2 验证**:
```bash
# 1. Stale cleanup 跑过?
docker exec deployment-rag-gpu-1 du -sh /app/prometheus_multiproc/
# 预期: 持续 ≤ 50MB (worker × 4 × ~10MB)

# 2. 强制一次清理 (debug)
docker exec deployment-rag-gpu-1 python -c "
from ekrs_rag.services.stale_cleanup import cleanup_stale_prometheus_files
from pathlib import Path
cleaned = cleanup_stale_prometheus_files('/app/prometheus_multiproc')
print(f'Cleaned {len(cleaned)} stale files: {[p.name for p in cleaned]}')
"
```

---

## 6. 回滚 (Rollback)

### 6.1 GPU → CPU 一键切回

```bash
make gpu-down
# 实际执行:
#   docker compose --profile gpu down (rag-gpu 停, profile 不启)
#   docker compose up -d rag (CPU rag 自动启)
```

预期:
- CPU rag 服务 :8000 健康 (Phase 11 healthcheck fix)
- Qdrant volume 不删 (GPU/CPU 共用)
- audit.log 保留 (CPU rag 接管)

### 6.2 完整回滚到 pre-13c (Phase 13b 旧 commit)

```bash
git log --oneline deployment/phase13c-ops-guide.md  # 找 13c 之前的 commit
git revert <commit_sha_13c>  # 单独 revert 13c commits, 保留 13b

# 重启 CPU rag
make dev-down
make dev
```

**注意**: Phase 13b T1-T6 已经 shipped (commit 4d9523d), 13c 是 incremental。回滚 13c 不影响 13b 的 GPU 通道能力 — 只是回到 "GPU 通道能跑但 audit gap + stale counter 已知风险"。

### 6.3 紧急停 GPU (debug 时)

```bash
# 不删 container, 仅停服务
docker exec deployment-rag-gpu-1 kill -TERM 1
# → lifespan 走 teardown, audit_bridge stop + stale_cleanup cancel 优雅退出
```

---

## 7. 升级路径 (Upgrade Path)

| Phase | 关键变更 | Commit | ops 影响 |
|---|---|---|---|
| 13b | GPU 通道 + EncodingRouter + bge-m3 torch FP16 | 4d9523d | 启 GPU 服务 (本指南 §3) |
| 13b-T5.1 | T5.1 bench + 5 uncommitted fixes | 43e81d9 | `make gpu-acceptance` |
| **13c** | **T1 audit bridge + T2 mark_process_dead + T3 FAILED fix + T4 dynamic threshold** | **(current)** | **本指南** |
| 13d (planned) | streamlit admin UI for GPU diag + audit explorer | TBD | 后续 sprint |

### 7.1 13c 验收 checklist

启动 GPU 服务后 30min 内必跑:

- [ ] `/healthz` 200 + `status: ok`
- [ ] `EKRS_AUDIT_QUEUE_ADDR` env 已 export (T1)
- [ ] `ps -ef` 看 stale_cleanup task 在事件循环里 (T2)
- [ ] `make gpu-acceptance` exit 0 (T4)
- [ ] audit.log 含 ≥1 条 `channel_switched` (T1 闭环)
- [ ] `/metrics` `gpu_memory_peak_bytes` < 6GB 且非 stale (T2 闭环)
- [ ] empty JSONL notify → `/v1/ingestion/status` 返 `"status": "failed"` (T3 闭环)

### 7.2 13d 预告 (out of scope)

- GPU 通道 admin UI (force-register / self_check 状态展示)
- audit.log explorer (filter by event_name + doc_hash)
- prometheus alert rules (GPU peak > 6GB for 5min, audit_dropped_rate > 100/s)
- channel_switched 抑制 (P2 deferred, T6 — 噪声真产生再评估)

---

## 8. 快速参考卡 (Quick Reference)

```bash
# 启动
make gpu-up                  # 起 GPU 服务 (auto-stop CPU)

# 验收
make gpu-acceptance          # T5.1 28-doc bench
make mock-notify             # 模拟 parser notification

# Debug
docker logs deployment-rag-gpu-1 -f --tail 50
tail -f deployment/rag_audit.log
curl -s http://localhost:8001/healthz | jq .
curl -s http://localhost:8001/v1/admin/gpu/memory-stats -H "X-Admin-Key: $ADMIN_KEY" | jq .

# 停
make gpu-down                # 停 GPU + 自动启回 CPU

# STRICT mode (生产预发布 gate)
T5_PHASE_B_MIN_CHUNKS_STRICT=1 make gpu-acceptance
```

---

**维护**: Phase 13c closure 后, 本文档纳入 `version 0.5.0 → 0.6.0` release notes。
**反馈**: ops issue → Slack #ekrs-ops 或 GitHub issue label `phase13c-ops`。