# Phase 13b GPU bge-m3 部署 — PoC 验收

## Context

Phase 13b 已闭 (commit 4d9523d, version 0.5.0)。T1-T6 全 ship, 但 GPU 通道只有 stub-测试。`make t5-acceptance` 真实基础设施验证 (`scripts/phase13b_poc_bench.py` / `phase13b_equiv_check.py` / `phase13b_failover_test.py`) 阻塞于: 容器内无 torch + 无 CUDA + 无 `pytorch_model.bin`。本次目标 = 部署一个可跑真实 GPU 验收的 `rag-gpu` 服务 (host `nvidia-smi` 已确认 RTX 4070 + CUDA 13.0 + torch 2.11+cu130)。PoC 模式 (D1-A 选定): bind-mount host 权重, 不内嵌 (~3GB image, 快速迭代)。T5.1 smoke bench 是本次阻塞项, T5.2/T5.3 留 follow-up。

## 关键发现 (来自 explore agent)

- `torch_bge_m3.py:130` `AutoModel.from_pretrained(..., torch_dtype=torch.float16)` 自动 downcast — 不需要预转换 FP16 权重
- `_self_check()` 需要 ONNX 模型 + torch 模型并存 (line 362-368 `EmbeddingService.is_dummy` check)
- `BGE_M3_MODEL_DIR` 默认值是 dev-laptop 路径, 容器必须 env override
- `encoding_router.py:58` 顶层 import `torch_bge_m3`, 但 `torch` import 全 lazy (函数内) — 模块本身在无 torch 环境可 import
- Probe daemon 在 `_init_child` 内启动, 30s tick
- Compose v3.8 + Docker Engine 不支持 Swarm `deploy.resources`, 用 `runtime: nvidia` 兼容路径

## Files

**新增 2:**
- `rag/Dockerfile.gpu` (~25 LOC, sibling of `rag/Dockerfile`)
- (修改) `deployment/docker-compose.override.yml` — 加 `services.rag-gpu` (已有 rag/dev_ui_v2/prometheus 三块保留)

**改 1:**
- `Makefile` — 加 `gpu-up` / `gpu-down` / `gpu-acceptance` 三个 target

**不动:**
- `rag/Dockerfile` (CPU 路径, byte-level 不变)
- `docker-compose.yml` 主文件 (override 已存在, 同样模式扩展)
- Settings (Phase 13b T1 已 ship `BGE_M3_GPU_*` env vars, 全在 `config.py:142-153`)

## Steps

### 1. `rag/Dockerfile.gpu`

Sibling of `rag/Dockerfile`, 但多 `pip install torch` from pytorch.org wheel index。镜像 rag/Dockerfile:1-60 (apt + pip + shared + rag COPY + ONNX vendor), 加 torch install + 默认 env。

```dockerfile
# Phase 13b GPU 部署 — PoC. Sibling of rag/Dockerfile.
# 复用 base 层 (PYTHON_BASE_IMAGE / shared install / rag install / ONNX vendor),
# 加 torch CUDA wheel install. Bind-mount host weights via compose override
# (PoC 模式, 不内嵌 pytorch_model.bin).
#
# Build context (set by deployment/docker-compose.yml) = REPO ROOT.
ARG PYTHON_BASE_IMAGE=python:3.11-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130

FROM ${PYTHON_BASE_IMAGE}

# ARGs declared before FROM not visible inside stages unless redeclared
ARG PIP_INDEX_URL
ARG TORCH_INDEX_URL

WORKDIR /app

# Install shared package first (cache layer) — 跟 rag/Dockerfile 一致
COPY shared /app/shared
RUN pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        /app/shared

# Install RAG package — 跟 rag/Dockerfile 一致 (含 [gpu] extra 是 host-side dev-only,
# 容器内直接 install torch 绕过 extras 解析, 锁 cu130 wheel)
COPY rag /app/rag
RUN pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        /app/rag

# GPU 关键差异 (Phase 13b 部署):
# torch==2.11.* 允许 patch 上游 bug fix (UQ-1 决议).
# transformers 已在 rag deps 里, 无需重复 install.
RUN pip install --no-cache-dir \
        --index-url "${TORCH_INDEX_URL}" \
        "torch==2.11.*" 2>&1 | tail -5

# Vendor ONNX model (跟 rag/Dockerfile:54 一致 — _self_check 需要它并存)
COPY rag/models/bge-m3/ /opt/ekrs/models/bge-m3/

ENV EMBEDDING_MODEL_DIR=/opt/ekrs/models/bge-m3 \
    EMBEDDING_MODEL=bge-m3 \
    # 默认 GPU off, 容器启动时 compose env override 到 true
    BGE_M3_GPU_ENABLED=false \
    # bind-mount host /home/pangzy/code_project/bge-m3 在 compose override 里
    BGE_M3_MODEL_DIR=/opt/ekrs/models/bge-m3-torch

WORKDIR /app/rag

EXPOSE 8000

CMD ["python", "-m", "ekrs_rag.main"]
```

**UQ-1 决议**: `torch==2.11.*` (允许 patch 上游 bug fix, 无需手动 bump)。

**Build context**: REPO ROOT (跟 `rag/Dockerfile` 一致; COPY `rag/models/bge-m3/` 才能找到 ONNX)。`BGE_M3_MODEL_DIR` 容器启动时 override 到 bind-mount 路径。

### 2. `deployment/docker-compose.override.yml` — 加 `rag-gpu` service

模式镜像已有的 `rag` build-args 块 + Phase 11 `dev_ui_v2` 模式 + Phase 5.5 prometheus `profiles: ["never"]`:

```yaml
services:
  rag:
    build:
      args:
        PYTHON_BASE_IMAGE: docker.m.daocloud.io/library/python:3.11-slim
        PIP_INDEX_URL: https://mirrors.aliyun.com/pypi/simple/
  dev_ui_v2:
    build:
      args:
        NODE_BASE_IMAGE: docker.m.daocloud.io/library/node:20-alpine
        NGINX_BASE_IMAGE: docker.m.daocloud.io/library/nginx:1.27-alpine
  prometheus:
    profiles: ["never"]

  # Phase 13b GPU 部署 — rag-gpu service.
  # profiles: ["gpu"] 默认不启, docker compose --profile gpu up -d 拉起.
  # runtime: nvidia + deploy.resources 双保险 (UQ-3 决议).
  # bind-mount host /home/pangzy/code_project/bge-m3 提供 torch pytorch_model.bin
  # (transformers runtime auto-downcast FP32 → FP16, 不需要预转换权重).
  rag-gpu:
    build:
      context: ..
      dockerfile: rag/Dockerfile.gpu
      args:
        # 同 rag 块, 用 daocloud.io 镜像加速
        PYTHON_BASE_IMAGE: docker.m.daocloud.io/library/python:3.11-slim
        PIP_INDEX_URL: https://mirrors.aliyun.com/pypi/simple/
        # torch 走 pytorch.org 官方 wheel index (无国内镜像, 直连 ~5min build)
        TORCH_INDEX_URL: https://download.pytorch.org/whl/cu130
    runtime: nvidia
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      - BGE_M3_GPU_ENABLED=true
      - BGE_M3_GPU_PROBE_ENABLED=true
      - BGE_M3_MODEL_DIR=/opt/ekrs/models/bge-m3-torch
      # UQ-6: 验收期间加速 probe (T5.3 failover 检测更快)
      - BGE_M3_GPU_PROBE_INTERVAL_S=5
      # 复用主 rag 的所有 env (PARSER_TOKEN, REDIS_URL, etc.) — docker compose 自动
      # 从同 stack 服务继承; 这里只覆盖 GPU 相关.
    volumes:
      # Bind-mount host FP32 weights (transformers auto-downcast to FP16 at load):
      - /home/pangzy/code_project/bge-m3:/opt/ekrs/models/bge-m3-torch:ro
      # Compose-internal shared volumes (复用 rag 的):
      - rag-shared:/app/rag
      - qdrant-data:/qdrant/storage
      - redis-data:/data
      # Audit + debug log 持久化到 host (跟 rag 服务同路径):
      - ./rag_audit.log:/app/rag/audit.log
      - ./rag_debug.log:/app/rag/debug.log
    ports:
      - "8001:8000"
    depends_on:
      qdrant:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      # Phase 11 T11-5 修复版 (bash /dev/tcp, dash /bin/sh 不支持)
      test: ["CMD-Bash", "-c", "exec 3<>/dev/tcp/localhost/8000 && echo -e 'GET /healthz HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q '\"status\":\"ok\"' <&3"]
      interval: 10s
      timeout: 5s
      retries: 6
    profiles: ["gpu"]
```

**UQ-3 决议**: `runtime: nvidia` + `deploy.resources.reservations.devices` 双保险 (`runtime` 对老 Docker 必要, `deploy` 对 Swarm 必要; 共存无害)。

**端口隔离**: `rag-gpu` `:8001`, `rag` `:8000` — 两服务结构上可同时存在 (但**验收时只跑 GPU** — 见 UQ-5)。

**profiles**: `--profile gpu` 默认不启, `docker compose --profile gpu up -d rag-gpu` 才拉起。

### 3. `Makefile` — 3 个 target

加到 `.PHONY` 列表末尾 + 文件末尾 (跟现有 `t5-acceptance` target 同区域):

```makefile
# Phase 13b GPU 部署 — PoC 验收 (T5.1 smoke bench).
# UQ-2: precheck host 模型目录 (低成本失败预防)
# UQ-4: T5.1 容器内 exec 跑 (RAG_URL=http://localhost:8000 = 容器内 GPU 服务)
# UQ-5: gpu-up 自动停 CPU rag 防 Qdrant wipe 冲突, gpu-down 自动恢复
# UQ-6: BGE_M3_GPU_PROBE_INTERVAL_S=5 已写死在 compose override (T5.3 failover)
gpu-up:
	@test -d /home/pangzy/code_project/bge-m3 || \
		(echo "ERROR: /home/pangzy/code_project/bge-m3 missing — bind-mount would fail" && exit 1)
	cd deployment && docker compose stop rag 2>/dev/null || true
	cd deployment && docker compose --profile gpu up -d rag-gpu
	@echo "Waiting for rag-gpu healthz..."
	@for i in $$(seq 1 30); do \
		if curl -s http://localhost:8001/healthz | grep -q '"status":"ok"'; then \
			echo "rag-gpu healthy"; break; \
		fi; sleep 2; \
	done

gpu-down:
	cd deployment && docker compose --profile gpu down
	@echo "Restart CPU rag service for normal ops..."
	cd deployment && docker compose up -d rag

# T5.1 smoke bench — 28 篇 ingest, peak mem, ingest p99.
# T5.2 (equiv) + T5.3 (failover) 单独 follow-up — 不阻塞 phase13b 合入.
gpu-acceptance:
	docker compose -f deployment/docker-compose.yml exec -T rag-gpu \
		bash -c 'RAG_URL=http://localhost:8000 \
		         python /app/rag/scripts/phase13b_poc_bench.py --phase full'
```

**UQ-5 决议**: `gpu-up` 先 `docker compose stop rag` (停 CPU), 避免双服务共享 Qdrant 的 wipe 冲突。`gpu-down` 自动恢复 `rag` 服务。

## Verification (T5.1 smoke bench 阻塞项)

1. **Build GPU image**:
   `cd deployment && docker compose --profile gpu build rag-gpu`
   预期 ~3GB image, build 3-5 min (torch CUDA wheel 下载)

2. **启动 + healthcheck**:
   `make gpu-up`
   `curl -s http://localhost:8001/healthz | jq .` → `status: ok`, `version: 0.5.0`

3. **CUDA 可用性**:
   `docker exec deployment-rag-gpu-1 python -c "import torch; print(torch.cuda.is_available(), torch.__version__)"`
   → `True 2.11.0+cu130` (or host wheel version)

4. **GPU 内存读**:
   `curl -s -X POST http://localhost:8001/v1/admin/gpu/memory-stats -H "X-Admin-Key: $$ADMIN_KEY" | jq .`
   → `{"peak_bytes": 0, "current_bytes": 0, "device_id": 0}` (首次 idle)

5. **触发 encode → audit channel_switched**:
   提交一个 bundle → `tail -f deployment/rag_audit.log | grep channel_switched`
   预期: 首次 `from_channel=unknown, to_channel=gpu` (phase13b-t3 docstring §3 行为)

6. **跑 T5.1 real-infra bench (容器内 exec)**:
   `make gpu-acceptance`
   预期:
   - 28 篇 ingest 全 success
   - GPU mem peak ≤ 6GB (从 `/v1/admin/gpu/memory-stats` 读)
   - 7787 chunks 总数对得上
   - 最大单篇 ≤ 30s, 2298-chunk 篇 ≤ 5s
   - failure_rate == 0

7. **Cleanup**:
   `make gpu-down` (恢复 CPU rag 服务)

## Follow-up (不阻塞本任务)

- **T5.2** (检索等价): bench 通过后单独跑 `phase13b_equiv_check.py` 对照 `deployment/phase12-recall-gt.json` (已填)
- **T5.3** (故障转移): 单独跑 `phase13b_failover_test.py`, `BGE_M3_GPU_PROBE_INTERVAL_S=5` 已配
- **Phase 13c**: cross-process audit writer 注入 (UQ-6 from Phase 13b closure)
- **Shippable GPU image**: 内嵌 FP32 weights + 预转换 FP16, ~5GB, 后续 sprint 议题

## Reuse (不重写)

- `rag/Dockerfile:1-60` (apt + pip + shared + rag COPY + ONNX vendor) — 直接复制到 `Dockerfile.gpu`, 加 torch install 即可
- `deployment/docker-compose.override.yml:1-22` (镜像 ARG override 模式) — 同结构扩 `rag-gpu`
- `rag/ekrs_rag/services/torch_bge_m3.py:130` (FP16 auto-downcast) — 不需要预转换权重
- `rag/ekrs_rag/services/encoding_pool.py:96-181` (`_init_child` pre-warm + probe daemon) — 全部已有, 容器启了即生效
- `rag/ekrs_rag/api/routes/admin.py:69-217` (`/v1/admin/gpu/memory-stats` + `/invalidate`) — Phase 13b T5 已 ship, GPU 路径自动暴露
- `scripts/_phase13b_common.py` (`RAG_URL` env override 模式) — `make gpu-acceptance` 容器内 exec, RAG_URL=http://localhost:8000 自动对

## 不做 (out of scope)

- 不 vendor `pytorch_model.bin` 进 image (D1-A PoC 选定)
- 不预转换 FP16 权重 (transformers runtime downcast 自动)
- 不改 `rag/Dockerfile` / `docker-compose.yml` 主文件 (override 模式扩展)
- 不动 Settings / `BGE_M3_*` env vars (Phase 13b T1 已 ship)
- 不改 T5 脚本 (env override `RAG_URL` 已支持)
- 不跑 T5.2/T5.3 full (T5.1 smoke bench 是阻塞项; T5.2/T5.3 留 follow-up, 失败不阻塞 phase13b 合入)

## Unresolved Questions (已决议)

| UQ | 问题 | 决议 |
|---|---|---|
| UQ-1 | torch 版本锁 | `torch==2.11.*` (允许 patch bug fix) |
| UQ-2 | host 权重 precheck | `Makefile gpu-up` 头部 `test -d` 失败立即 exit 1 |
| UQ-3 | runtime + deploy 双保险 | 保留双保险 (兼容老 Docker + Swarm) |
| UQ-4 | T5.1 跑 host vs 容器 | 容器内 `compose exec -T rag-gpu`, RAG_URL=http://localhost:8000 |
| UQ-5 | 双服务 Qdrant wipe 冲突 | `gpu-up` 自动 `docker compose stop rag`; `gpu-down` 自动恢复 |
| UQ-6 | probe interval 加速 | compose env `BGE_M3_GPU_PROBE_INTERVAL_S=5` 写死, T5.3 failover 检测更快 |

## Memory update (任务完成后)

写 `phase13b-gpu-deployment.md`:
- commit hash + image size + build 时间
- UQ-1 ~ UQ-6 全部 resolved
- T5.1 bench 通过 baseline 数据 (peak mem, ingest p99, 28 篇 chunks/sec)
- T5.2/T5.3 real-infra 留 follow-up
- Phase 13c next: cross-process audit wiring (UQ-6 from closure), shippable image 内嵌权重
