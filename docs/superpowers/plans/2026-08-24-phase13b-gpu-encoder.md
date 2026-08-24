# Phase 13b — GPU 编码通道(torch FP16 实现 + EncodingRouter 调度)

日期: 2026-08-24 · 状态: v1.0-draft · 关联: `docs/specs/phase13-gpu-encoding-channel-spec.md` v1.2 + Phase 13a T9 seam

---

## 0. 一句话

把 Phase 13a T9 的 `_encode_backend` Protocol seam 替换为 torch FP16 GPU 实现; EncodingRouter 嵌进 P0-2 worker encode 步(G7 修订,不是新一层); CPU 是 fallback。

## 1. 不可妥协

- 七铁律 R1-R8 全程不变
- T9 Protocol 契约 `list[list[float]]` dense shape 不变,13b 失败必现(eng-review Issue 5)
- sparse 头沿用 `sparse_linear.pt`(G2 方案 A,免 TF-IDF 降级)
- CLS pooling + L2 norm(G1, 必修否则向量系统性偏移)
- GPU 故障 30s 内切 CPU,服务不中断
- 启动自检 vs 仓库 vendored ONNX FP32 余弦 ≥0.999 才注册 GPU 通道(否则纯 CPU)
- `chunk_gate > 3000` 走 CPU 通道(§10 P0-4 联动)
- 无 GPU 设备 → 静默降级 CPU,不报错
- 入仓模型仅 `/home/pangzy/code_project/bge-m3`(已下载),不连 HF / hf-mirror

## 2. 模型与资源

- 模型目录: `/home/pangzy/code_project/bge-m3/`(`pytorch_model.bin` 2.1GB + `sparse_linear.pt` 3.4KB + tokenizer)
- 设备: RTX 4070 8GB(torch 2.11.0+cu130 cuda available, 已验证)
- 显存预算: 单批峰值 ≤ 6GB(留 2GB)
- 冷加载预算: ≤30s

## 3. 范围

| 包含 | 不包含 |
|---|---|
| `services/torch_bge_m3.py` 新模块(GPU encoder 实现) | 独立 GPU HTTP 服务容器(13c 增量) |
| EncodingRouter 嵌进 `_run_step5` encode 步 | k8s `gpu=1` 调度 / 多 GPU 副本 |
| `channel_switched` 审计事件(走 4 步注册) | ORT-GPU 路径(§3.1 修正: 通过 torch PoC 则跳过) |
| 启动自检 vs ONNX FP32 cosine ≥0.999 | 跨节点 HTTP encoder(13c) |
| `scripts/phase13b_poc_bench.py`(28-doc 测集) | P0-4 admission 改动(已在 13a T2) |
| GPU 指标: `gpu_memory_used_bytes` / `gpu_memory_peak_bytes` / `encode_batch_size` / `encode_latency_seconds` | 显存硬闸 enforcement(spec §10 P0-4 联动, 留 hook 不实现) |

## 4. 风险与对策

| 风险 | 对策 |
|---|---|
| GPU OOM 杀进程 | batch 32 → 16 → 8 自适应降级;最后 chunk 溢出 CPU |
| FP16 与 FP32 余弦不达标 | 启动自检拦住(<0.999 不注册 GPU);运行期监控 cosine 异常告警 |
| 多 pebble worker 抢 1 GPU | `nvidia-smi --query-gpu=index` 拿独占 ID; Settings `GPU_DEVICE_ID: int = 0` |
| torch 2.1GB 权重冷启慢 | EncodingPool `_init_child` 预加载(13a T4 已有 hook) |
| 自检基准不存在(无 vendored ONNX) | 13a T2 已用 ONNX,镜像内必有;启动期如缺 → 静默纯 CPU |
| 28-doc 测集不在仓库 | 用 `dev_ui_v2/tests/mocks/handlers.ts` 同源 JSONL 拼, 或 `phase12-task-d-745` 子集 |

## 5. 任务清单

### T1 torch FP16 encoder 实现

目标: `rag/ekrs_rag/services/torch_bge_m3.py` 暴露 `encode_gpu(texts: list[str]) -> list[EncodedVector]`, 与 CPU 路径返回同型(满足 T9 Protocol)

- [ ] **1.1** 新建 `services/torch_bge_m3.py`:
  - `_load_model(model_dir)` 懒加载 `AutoModel.from_pretrained(MODEL_DIR, torch_dtype=torch.float16).cuda().eval()`, 失败 raise `EmbeddingUnavailableError`
  - `_load_sparse_head(model_dir)` 复用 CPU 路径同款 `sparse_linear.pt` 加载逻辑(`OnnxBgeM3._load_sparse_head` 已抽出或重抽到 `services/torch_bge_m3.py`)
  - `_encode_gpu(texts)` 走 batch 32, `tok(batch, padding=True, truncation=True, max_length=512)`, `last_hidden_state[:, 0]` CLS pooling + L2 norm(G1), sparse = `relu(h @ W_lex.T + b_lex)`(G2)
  - 满足 `_EncodeBackend` Protocol 契约:`encode_gpu` 直接返回 dense list-of-list-of-float 即可(T9 seam 签), sparse 走 `QdrantManager.to_qdrant_sparse`(走原路径)
- [ ] **1.2** Settings 加 `BGE_M3_MODEL_DIR: Path = Path("/home/pangzy/code_project/bge-m3")` + `BGE_M3_GPU_DEVICE_ID: int = 0` + `BGE_M3_GPU_ENABLED: bool = True`
- [ ] **1.3** TDD RED→GREEN: 写单测 `tests/unit/test_torch_bge_m3.py`
  - `test_encode_gpu_empty` → `[]`
  - `test_encode_gpu_shape` → `list[list[float]]` 每行 1024 维(Protocol 契约锁定, T9 regression 类)
  - `test_encode_gpu_l2_norm` → `np.linalg.norm(row, 2) ≈ 1.0` 浮点容差 1e-5
  - `test_encode_gpu_cosine_vs_onnx` → 同一文本 vs CPU `EmbeddingService().encode()`, dense 余弦 ≥0.999(验收 #9 锁定)
  - `test_encode_gpu_unavailable_no_cuda` → monkeypatch `torch.cuda.is_available=False`, `encode_gpu` raise `EmbeddingUnavailableError`, 不崩
- [ ] **1.4** 验证: torch 加载 + 100 chunk 编码 p99 ≤ baseline CPU 单 chunk 5x(CPU 9 chunks/s ≈ 110ms/chunk; GPU 目标 30-50ms)
- [ ] **1.5** Commit: `feat(encoder): torch FP16 bge-m3 GPU encoder (13b T1)`

### T2 启动自检(验收 #10)

目标: GPU 容器启动 ≤30s 完成加载 + 自检, dense 余弦 ≥0.999 才注册 GPU 通道; 否则纯 CPU 降级

- [ ] **2.1** `services/torch_bge_m3.py` 加 `_self_check(model_dir) -> bool`:
  - 探针集: `tests/fixtures/bge_m3_self_check_probes.jsonl`(5 条固定文本, 含中文+英文+数字+符号)
  - GPU FP16 encode(probe) vs CPU ONNX FP32 encode(probe) 同文本对, dense 余弦取 min
  - 余弦 <0.999 → log warning, 返回 False; 返回 True 才注册 GPU
- [ ] **2.2** `services/encoding_router.py` 新模块, 暴露 `EncodingRouter.try_register_gpu() -> bool`:
  - 调 `_self_check`; True → `state.gpu_available = True`; False → `state.gpu_available = False` + log
  - 无 `torch.cuda` → 直接 False + log info("no cuda, cpu only")
  - 无 vendored ONNX(自检基准) → 直接 False + log warning
- [ ] **2.3** 测试: `tests/unit/test_encoding_router.py`
  - `test_self_check_pass` mock dense 输出余弦 ≥0.999 → True
  - `test_self_check_fail` mock 余弦 <0.999 → False + 不 raise
  - `test_no_cuda_returns_false` monkeypatch `torch.cuda.is_available=False` → False
  - `test_no_onnx_baseline_returns_false` → missing probe baseline file → False
- [ ] **2.4** 集成进 `services/step5_worker.py` 的 `_init_child` (Phase 13a T4 已有): 子进程预热时调 `EncodingRouter.try_register_gpu()`, 结果存 `child_local.gpu_available`
- [ ] **2.5** Commit: `feat(encoder): GPU 启动自检 + EncodingRouter 注册门`

### T3 EncodingRouter 调度(验收 #4/#5)

目标: `EncodingRouter` 嵌进 `_run_step5` encode 步(G7 修订: Router 即 worker encode 步), GPU/CPU 通道动态切换

- [ ] **3.1** `services/encoding_router.py` 加 dispatch:
  - `route(texts) -> list[EncodedVector]`: GPU 可用且队列深度 ≤10 → `_encode_gpu`; 否则 CPU `EmbeddingService().encode`
  - 队列深度来自 `EncodingPool` task registry(13a T4 已就绪);查询 thread-safe
  - 切通道 emit `channel_switched` 审计事件(4 步: `_EVENT_SCHEMAS` 注册 + emit site + ekrs-handbook §16 登记 + 真实 AuditWriter 回归测试)
- [ ] **3.2** `services/step5_worker.py` 把 `_run_step5` encode 步骤从 `qdrant.upsert_chunks` 内置编码改为先 `EncodingRouter.route(texts)` → 把 `list[EncodedVector]` 喂给 `qdrant.upsert_chunks`:
  - 优先方案: 给 `qdrant.upsert_chunks` 加 `precomputed_encodings: list[EncodedVector] | None = None` kwarg, 非 None 跳过内置编码(G6 contract 不变: 仅多一条路径)
  - 替代方案: `_encode_backend` module fn 直接返 dense, qdrant sparse 路径复用(双 store, 这是 G2 难点 — 见开放问题 OQ-2)
- [ ] **3.3** 故障转移:
  - GPU 单次 encode raise `EmbeddingUnavailableError` / `torch.cuda.OutOfMemoryError` → 自动降级 CPU, emit `channel_switched{from: gpu, to: cpu, reason: oom|unavailable}`
  - 30s 内 GPU 健康探活(每 30s `try_register_gpu`); GPU 恢复 → 自动回主通道
- [ ] **3.4** 测试:
  - `tests/unit/test_encoding_router.py::test_route_gpu_when_available`: GPU 可用 + queue ≤10 → 走 GPU
  - `tests/unit/test_encoding_router.py::test_route_cpu_when_queue_overflow`: queue >10 → 走 CPU
  - `tests/unit/test_encoding_router.py::test_route_fallback_on_gpu_error`: GPU raise → CPU + `channel_switched` audit emit
  - `tests/integration/test_phase13b_e2e.py::test_dual_channel_end_to_end`: 启 worker, 投 doc 跑通 encode, 验证 Qdrant+FTS 双写一致
- [ ] **3.5** Commit: `feat(router): EncodingRouter dispatch with CPU fallback + channel_switched audit`

### T4 GPU 指标 + 启动恢复(验收 P1-3)

目标: GPU 显存/批次/延迟指标进 Prometheus multiproc

- [ ] **4.1** `services/metrics.py`(Phase 13a T7 已就绪)新增:
  - `ekrs_gpu_memory_used_bytes: Gauge` (label: device_id)
  - `ekrs_gpu_memory_peak_bytes: Gauge`
  - `ekrs_gpu_encode_batch_size: Histogram`(buckets 8, 16, 32, 64)
  - `ekrs_gpu_encode_latency_seconds: Histogram`(buckets 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)
- [ ] **4.2** `torch_bge_m3._encode_gpu` 调指标:
  - 进入时 `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` 解析 → gauge
  - batch size → histogram
  - wall clock latency → histogram
- [ ] **4.3** 启动恢复(13a T7 已有 boot_recovery): 加 GPU-specific:
  - `gpu_available` flag 持久化到 TaskRepo(可选, 默认 in-memory)
  - 启动时若 `BGE_M3_GPU_ENABLED=true` → 调 `try_register_gpu` 一次
- [ ] **4.4** 测试: `tests/unit/test_phase13b_t4.py::test_gpu_metrics_emit` + `test_boot_recovery_reregisters_gpu`
- [ ] **4.5** Commit: `feat(metrics): GPU encode metrics (P1-3 multiproc surface)`

### T5 E2E 验收(验收 #1-#10 全集)

目标: real-container E2E, GPU 通道对 28-doc 测集实现检索等价 + 性能目标

- [ ] **5.1** `scripts/phase13b_poc_bench.py`: 28-doc 测集(retry2 子集 / `phase12-task-d-745` 子集 / 自构 28-doc JSONL)逐 doc encode 测:
  - 维度: GPU FP16 encode vs CPU ONNX FP32 encode
  - 报告: 总 chunks / 总耗时 / p50/p99 / 显存峰值 / 失败 doc 数
  - 验收线: 7787-chunk ≤30s(验收 #3), 2298-chunk ≤5s; ≥6GB 显存(验收 #2); 全 doc 无 OOM(验收 #7)
- [ ] **5.2** `scripts/phase13b_equiv_check.py`: 检索等价验收(验收 #1):
  - 同 doc, 同一组 query, GPU 通道 vs CPU 通道各跑一遍 retriever
  - Top-10 重合率 ≥99%, recall@10 差 ≤1pp; 余弦 ≥0.999(过程指标,验收 #9)
  - sparse 验收(验收 #8): 同一 doc 同一 query, GPU sparse 索引 vs CPU sparse 索引重合率 ≥95%
- [ ] **5.3** `scripts/phase13b_failover_test.py`: GPU kill → CPU 接管 ≤30s(验收 #4)
- [ ] **5.4** full suite: `pytest tests/unit + tests/golden_set` 零退化(验收 #6)
- [ ] **5.5** `mypy rag/` clean(无 NEW 错误); 全部审计事件 schema 在 `_EVENT_SCHEMAS` 注册
- [ ] **5.6** Commit: `test(prod): Phase 13b E2E acceptance suite (28-doc bench + equiv + failover)`

### T6 Plan closure

- [ ] **6.1** version bump 0.4.0 → 0.5.0(新增 capability per Keep-a-Changelog)
- [ ] **6.2** CHANGELOG `[phase13b]` section: T1-T5 总结 + 10 项验收逐条 green/red
- [ ] **6.3** MEMORY 更新 phase13b 闭项
- [ ] **6.4** `phase13b` annotated tag force-move 到 closure commit
- [ ] **6.5** Commit: `chore(release): Phase 13b closure — version 0.5.0 + CHANGELOG [phase13b]`

---

## 6. 验收矩阵(closure 前自检)

| # | 项 | 状态门 | 验证 |
|---|---|---|---|
| 1 | 检索等价 | Top-10 重合 ≥99% + recall@10 差 ≤1pp | `phase13b_equiv_check.py` |
| 2 | 显存 | 7787-chunk 峰值 ≤6GB 无 OOM | `phase13b_poc_bench.py` GPU mem peak |
| 3 | 性能 | 7787-chunk ≤30s; 2298-chunk ≤5s | bench report |
| 4 | 故障转移 | GPU kill → 30s 内 CPU 接管 | `phase13b_failover_test.py` |
| 5 | 过载 | GPU 队列 >10 → CPU | `test_route_cpu_when_queue_overflow` |
| 6 | 回归 | golden 208 零退化 | `pytest tests/golden_set/` |
| 7 | 稳定性 | 2h / 2000+ doc 无 OOM 无降速 | bench 长跑 |
| 8 | sparse 完整 | GPU dense+sparse 双头, sparse 重合 ≥95% | equiv check |
| 9 | Pooling 一致 | GPU vs CPU CLS 余弦 ≥0.999 | `test_encode_gpu_cosine_vs_onnx` |
| 10 | 启动自检 | 余弦 ≥0.999 才注册, 否则纯 CPU | `test_self_check_*` |

---

## 7. 任务依赖与顺序

```
T1 (encoder 实现)
   ↓
T2 (启动自检 + Router 注册门)  ←── 依赖 T1 (encoder callable)
   ↓
T3 (EncodingRouter 调度 + 故障转移 + audit)  ←── 依赖 T1 + T2
   ↓
T4 (GPU 指标 + 启动恢复)  ←── 依赖 T1
   ↓
T5 (E2E 验收套件)  ←── 依赖 T1+T2+T3+T4
   ↓
T6 (Plan closure)
```

T2 / T4 可与 T1 并行(只要 T1 expose `encode_gpu` 接口稳定); T3 是 critical path, T6 最后。

## 8. 文件清单

| 文件 | 操作 |
|---|---|
| `rag/ekrs_rag/services/torch_bge_m3.py` | 新建 |
| `rag/ekrs_rag/services/encoding_router.py` | 新建 |
| `rag/ekrs_rag/services/step5_worker.py` | 改: 集成 EncodingRouter(走 T9 seam) |
| `rag/ekrs_rag/core/config.py` | 改: BGE_M3_MODEL_DIR / GPU_DEVICE_ID / GPU_ENABLED |
| `rag/ekrs_rag/main.py` | 改: lifespan 注册 EncodingRouter.try_register_gpu() |
| `rag/ekrs_rag/main.py` | 改: _EVENT_SCHEMAS 注册新事件 |
| `rag/tests/unit/test_torch_bge_m3.py` | 新建 |
| `rag/tests/unit/test_encoding_router.py` | 新建 |
| `rag/tests/unit/test_phase13b_t4.py` | 新建 |
| `rag/tests/integration/test_phase13b_e2e.py` | 新建 |
| `rag/tests/fixtures/bge_m3_self_check_probes.jsonl` | 新建(5 条探针) |
| `scripts/phase13b_poc_bench.py` | 新建 |
| `scripts/phase13b_equiv_check.py` | 新建 |
| `scripts/phase13b_failover_test.py` | 新建 |
| `CHANGELOG.md` | 改: [phase13b] section |
| `rag/pyproject.toml` + `shared/pyproject.toml` | 改: version 0.4.0 → 0.5.0 |

预估 touched ≈ 16 文件 / 增量 ~1200 LOC / 4-5 人日(与 spec §9 估算一致)

---

## 9. 开放问题 / 未决项

- **OQ-1** 双 store 双写(`sparse` 也要 GPU 算)or 单 dense(`sparse` 复用 CPU)? spec G2 写"GPU 服务必须同时产出 lexical weights",但 T9 Protocol seam 只覆盖 dense。决策: T3.2 走"先 GPU dense + 后 CPU sparse"双通道, OR T1.1 直接 GPU 双头返回 EncodedVector(含 sparse dict)。**建议 T1.1 直接双头**(成本只多 `h @ W_lex.T + b_lex` 一行 matmul,稀疏权已加载);如选双通道, `channel_switched` 触发逻辑更复杂。
- **OQ-2** `_encode_backend` module fn(T9 seam 当前是 CPU 默认实现)替换路径:
  - (a) module fn body 直接换成 GPU 实现 → CPU 路径消失, 失去 fallback 内存表示
  - (b) 模块 fn 保持 CPU 默认, GPU 走 EncodingRouter 旁路(本文 T3 方案) — 兼容 T9 测试(stub shape == CPU stub 验证)
  - **建议 (b)**, 保持 T9 测试稳定, EncodingRouter 是 13b 主体。
- **OQ-3** 测集 28-doc 在哪? retry2 corpus / phase12-task-d-745 / 自构? 需用户决策。**默认建议**: 自构 28 条 JSONL(覆盖中文工程文档典型 + 英文 boilerplate),路径 `rag/tests/fixtures/phase13b_poc_28doc/`,与 `bge-m3/` 目录并列 vendored(如不可放仓则用 `.gitignore` + 下载脚本)。
- **OQ-4** `/home/pangzy/code_project/bge-m3` 是否入仓? 现 2.2GB `pytorch_model.bin` + 16MB `tokenizer.json` 总 ~2.5GB, .gitignore 现状? **建议**: 不入仓, 镜像构建期 COPY 进去(`Dockerfile.bge-m3-vendored`);本地 dev 直接引用挂载路径。需 ops 确认镜像分发链路。
- **OQ-5** GPU 容器化 vs in-process? 当前设备是单机 RTX 4070, in-process pebble worker + GPU 直通最简; 13b 不引入独立 GPU HTTP 服务。**13c 增量**才是 k8s 多副本 + 独立 GPU 容器。当前 (P0-2 worker = 调度层 + 同一进程 GPU encoder) 与 spec §6 部署架构"Router → GPU Worker → CPU Fallback"一致(G7 修订)。
- **OQ-6** PoC 失败回退到 ORT-GPU 是否要做? spec §3.1 "torch FP16 优先, 通过则跳过 ORT-GPU" — 隐含如 torch 失败才退回。**本 plan 不含 ORT-GPU 实现**,仅留 hook(`BGE_M3_BACKEND: Literal["torch", "onnx_gpu"] = "torch"` Settings);如 torch PoC 显存/性能不达标, 再开 13b-ORT 增量。
- **OQ-7** 启动自检探针集是否要 stable + 入仓? 5 条文本混合中英文数字符号即可, JSONL 入仓 `rag/tests/fixtures/bge_m3_self_check_probes.jsonl` 与测集同源。是否足够体现 FP16/FP32 边界需 GPU PoC 验证。
- **OQ-8** `channel_switched` 审计事件是否高频 emit? 单次降级 1 次, 健康探活 30s/次 → 不高频;但 GPU 抖动时可能 1h 内 emit 数十次。考虑加 `since_last_emit_sec` 抑制(同 audit 4 步纪律下,先实现简单版)。

---

## 10. 关联 & 引用

- 上游: Phase 13a T9 seam `commit 145f380`, closure `e5c8f39`, tag `phase13a`
- 下游: Phase 13c(独立 GPU HTTP 容器 + k8s 多副本)留作未来 plan
- Spec: `docs/specs/phase13-gpu-encoding-channel-spec.md` v1.2(`commit f602577`)
- v10 数据: `deployment/phase12-v10-verification.md`
- 验收 #10 自检基准: 仓库 vendored ONNX FP32, 13a T2 阶段镜像内置
- T9 seam 路径: `rag/ekrs_rag/services/step5_worker.py:53-95`