# Phase 13b — GPU 编码通道(torch FP16 实现 + EncodingRouter 调度)

日期: 2026-08-24 · 状态: **v1.1**(eng-review 反馈整合, 8 OQ 全 RESOLVED) · 关联: `docs/specs/phase13-gpu-encoding-channel-spec.md` v1.2 + Phase 13a T9 seam

---

## 0. 一句话

把 Phase 13a T9 的 `_encode_backend` Protocol seam 替换为 torch FP16 GPU 实现; EncodingRouter 嵌进 P0-2 worker encode 步(G7 修订,不是新一层); CPU 是 fallback。**T1.1 直接实现 dense+sparse 双头**(一行 matmul 成本极低,验收 #8 硬要求,免后续 sparse 路径漂移)。

## 1. 不可妥协

- 七铁律 R1-R8 全程不变
- T9 Protocol 契约 `list[list[float]]` dense shape 不变,13b 失败必现(eng-review Issue 5)
- sparse 头沿用 `sparse_linear.pt`(G2 方案 A,免 TF-IDF 降级)
- CLS pooling + L2 norm(G1, 必修否则向量系统性偏移)
- **dense+sparse 双头一次返回 `EncodedVector` 完整结构**(review 🔴 #1,免 T3.2 半切换状态)
- GPU 故障 30s 内切 CPU,服务不中断
- 启动自检 vs 仓库 vendored ONNX FP32 余弦 ≥0.999 才注册 GPU 通道(否则纯 CPU)
- `chunk_gate > 3000` 走 CPU 通道(§10 P0-4 联动)
- 无 GPU 设备 → 静默降级 CPU,不报错
- 入仓模型仅 `/home/pangzy/code_project/bge-m3`(已下载),不连 HF / hf-mirror
- **`qdrant.upsert_chunks` 接受 `precomputed_encodings: list[EncodedVector] | None` kwarg,非 None 时跳过内部编码**(review 🔴 #2,避免重复编码)

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

目标: `rag/ekrs_rag/services/torch_bge_m3.py` 暴露 `encode_gpu(texts: list[str]) -> list[EncodedVector]`, 与 CPU 路径返回同型(**dense+sparse 双头完整结构**, review 🔴 #1 锁定)

- [ ] **1.1** 新建 `services/torch_bge_m3.py`:
  - `_load_model(model_dir)` 懒加载 `AutoModel.from_pretrained(MODEL_DIR, torch_dtype=torch.float16).cuda().eval()`, 失败 raise `EmbeddingUnavailableError`
  - `_load_sparse_head(model_dir)` 复用 CPU 路径同款 `sparse_linear.pt` 加载逻辑(`OnnxBgeM3._load_sparse_head` 抽到 `services/torch_bge_m3.py` 共享或直接复制,避免引入新 cross-module dep)
  - `_encode_gpu(texts) -> list[EncodedVector]`: 走 batch 32, `tok(batch, padding=True, truncation=True, max_length=512)`, `last_hidden_state[:, 0]` CLS pooling + L2 norm(G1), sparse = `relu(h @ W_lex.T + b_lex)`(G2 方案 A)
  - **返回 `EncodedVector` 完整结构**(`dense: list[float]` + `sparse: dict[int, float]`),与 `EmbeddingService().encode()` 同型,稀疏 token_id 走 `QdrantManager.to_qdrant_sparse` 兼容
  - **双头成本评估**: sparse matmul `[batch, seq, 1024] @ [1024, 1] + bias` ~ 1MB GPU mem + 微秒级,与 dense 主编码同 batch 内共享 `last_hidden_state`,几乎零额外成本
- [ ] **1.2** Settings 加:
  - `BGE_M3_MODEL_DIR: Path = Path("/home/pangzy/code_project/bge-m3")`
  - `BGE_M3_GPU_DEVICE_ID: int = 0`
  - `BGE_M3_GPU_ENABLED: bool = True`
  - `BGE_M3_BACKEND: Literal["torch", "onnx_gpu"] = "torch"`(review 🟢 #7, 留 hook 未来无缝切换)
- [ ] **1.3** TDD RED→GREEN: 写单测 `tests/unit/test_torch_bge_m3.py`
  - `test_encode_gpu_empty` → `[]`
  - `test_encode_gpu_dense_shape` → 每行 1024 维(Protocol 契约锁定, T9 regression 类)
  - `test_encode_gpu_l2_norm` → `np.linalg.norm(row, 2) ≈ 1.0` 浮点容差 1e-5
  - `test_encode_gpu_sparse_present` → 非空文本返 `EncodedVector.sparse` 非空 dict,含正权重 token(G2 验证)
  - `test_encode_gpu_cosine_vs_onnx` → 同一文本 vs CPU `EmbeddingService().encode()`, dense 余弦 ≥0.999(验收 #9 锁定)
  - `test_encode_gpu_unavailable_no_cuda` → monkeypatch `torch.cuda.is_available=False`, `encode_gpu` raise `EmbeddingUnavailableError`, 不崩
  - `test_encode_gpu_sparse_matches_cpu` → sparse 索引/值与 CPU 路径重合 ≥95%(验收 #8)
- [ ] **1.4** 验证: torch 加载 + 100 chunk 编码 p99 ≤ baseline CPU 单 chunk 5x(CPU 9 chunks/s ≈ 110ms/chunk; GPU 目标 30-50ms)
- [ ] **1.5** CUDA 上下文预热(review 🟡 #5): `_init_child`(Phase13a T4 已就绪)调 `torch.cuda.init()` + 一次空 `torch.tensor([0], device=f"cuda:{GPU_DEVICE_ID}")`,避免模型加载时才触发首 CUDA 调用超时
- [ ] **1.6** Commit: `feat(encoder): torch FP16 bge-m3 GPU encoder w/ dual-head (13b T1)`

### T2 启动自检(验收 #10)

目标: GPU 容器启动 ≤30s 完成加载 + 自检, dense 余弦 ≥0.999 才注册 GPU 通道; 否则纯 CPU 降级

- [ ] **2.1** `services/torch_bge_m3.py` 加 `_self_check(model_dir) -> bool`:
  - 探针集: `rag/tests/fixtures/bge_m3_self_check_probes.jsonl`(review 🟡 #4, **至少 4 类覆盖**):
    1. **纯英文短句**(e.g. `"Hello world."`)
    2. **中文长句**(e.g. `"钢材标准 GB/T 12459 温度 ≤ 80℃ 压力 1.6MPa。"`)
    3. **数字/符号密集文本**(模拟代码或表格, e.g. `"a=1.6e-3; T=80±0.5℃; range=[0.1, 100];"`)
    4. **空字符串**(边界,验证空输入不崩)
  - GPU FP16 encode(probe) vs CPU ONNX FP32 encode(probe) 同文本对, dense 余弦取 min(空字符串跳过)
  - 余弦 <0.999 → log warning, 返回 False; 返回 True 才注册 GPU
- [ ] **2.2** `services/encoding_router.py` 新模块, 暴露 `EncodingRouter.try_register_gpu() -> bool`:
  - 调 `_self_check`; True → `state.gpu_available = True`; False → `state.gpu_available = False` + log
  - 无 `torch.cuda` → 直接 False + log info("no cuda, cpu only")
  - 无 vendored ONNX(自检基准) → 直接 False + log warning
  - **状态机持久化**: `state.gpu_available` 是 process-local(单 worker 子进程内); 多 worker 各自独立, 故障 worker 切 CPU, 其他 worker 继续 GPU
- [ ] **2.3** 测试: `tests/unit/test_encoding_router.py`
  - `test_self_check_pass` mock dense 输出余弦 ≥0.999 → True
  - `test_self_check_fail` mock 余弦 <0.999 → False + 不 raise
  - `test_self_check_empty_string_skipped` → 空探针不参与 min 计算
  - `test_no_cuda_returns_false` monkeypatch `torch.cuda.is_available=False` → False
  - `test_no_onnx_baseline_returns_false` → missing probe baseline file → False
- [ ] **2.4** 集成进 `services/step5_worker.py` 的 `_init_child` (Phase 13a T4 已有): 子进程预热时调 `EncodingRouter.try_register_gpu()`, 结果存 `child_local.gpu_available`
- [ ] **2.5** Commit: `feat(encoder): GPU 启动自检 + EncodingRouter 注册门 (4-class probes)`

### T3 EncodingRouter 调度(验收 #4/#5)

目标: `EncodingRouter` 嵌进 `_run_step5` encode 步(G7 修订: Router 即 worker encode 步), GPU/CPU 通道动态切换; **state machine 仅在状态切换时 emit `channel_switched`**(review 🟢 #6)

- [ ] **3.1** `services/encoding_router.py` 加 dispatch:
  - `route(texts) -> list[EncodedVector]`: GPU 可用且队列深度 ≤10 → `_encode_gpu`; 否则 CPU `EmbeddingService().encode`
  - 队列深度来自 `EncodingPool` task registry(13a T4 已就绪);查询 thread-safe
  - **状态机(review 🟢 #6)**: `state.current_channel ∈ {gpu, cpu}`,仅在状态转换时 emit `channel_switched{from, to, reason}`; `state.last_emit_ts` 持久化避免抖动风暴
  - `channel_switched` 审计事件走 4 步:`_EVENT_SCHEMAS` 注册 + emit site + ekrs-handbook §16 登记 + 真实 AuditWriter 回归测试
- [ ] **3.2** `services/step5_worker.py` 把 `_run_step5` encode 步骤从 `qdrant.upsert_chunks` 内置编码改为先 `EncodingRouter.route(texts)` → 把 `list[EncodedVector]` 喂给 `qdrant.upsert_chunks`:
  - **关键(review 🔴 #2)**: 给 `qdrant.upsert_chunks` 加 `precomputed_encodings: list[EncodedVector] | None = None` kwarg, 非 None 时**完全跳过内部编码**(避免 GPU 算一遍 CPU 再算一遍)
  - `QdrantManager.upsert_chunks` 调用现场全检: `if precomputed_encodings is None: _encode_via_internal_service(...)` 卫语句,保证 kwarg 非 None 时不走 EmbeddingService
- [ ] **3.3** 故障转移:
  - GPU 单次 encode raise `EmbeddingUnavailableError` / `torch.cuda.OutOfMemoryError` → 自动降级 CPU, **仅当状态变化时** emit `channel_switched{from: gpu, to: cpu, reason: oom|unavailable}`
  - 30s 内 GPU 健康探活(每 30s `try_register_gpu`); GPU 恢复 → **仅当状态从 cpu 切回 gpu 时** emit `channel_switched{from: cpu, to: gpu, reason: recovered}`
- [ ] **3.4** 测试:
  - `tests/unit/test_encoding_router.py::test_route_gpu_when_available`: GPU 可用 + queue ≤10 → 走 GPU
  - `tests/unit/test_encoding_router.py::test_route_cpu_when_queue_overflow`: queue >10 → 走 CPU
  - `tests/unit/test_encoding_router.py::test_route_fallback_on_gpu_error`: GPU raise → CPU + state transition emit `channel_switched`
  - `tests/unit/test_encoding_router.py::test_state_machine_no_emit_on_same_channel`: 连续 GPU 失败 3 次,只 emit 1 次 `channel_switched`(防抖动)
  - `tests/integration/test_phase13b_e2e.py::test_dual_channel_end_to_end`: 启 worker, 投 doc 跑通 encode, 验证 Qdrant+FTS 双写一致
- [ ] **3.5** Commit: `feat(router): EncodingRouter dispatch w/ state machine + channel_switched audit`

### T4 GPU 指标 + 启动恢复(验收 P1-3)

目标: GPU 显存/批次/延迟指标进 Prometheus multiproc

- [ ] **4.1** `services/metrics.py`(Phase 13a T7 已就绪)新增:
  - `ekrs_gpu_memory_used_bytes: Gauge` (label: device_id)
  - `ekrs_gpu_memory_peak_bytes: Gauge`
  - `ekrs_gpu_encode_batch_size: Histogram`(buckets 8, 16, 32, 64)
  - `ekrs_gpu_encode_latency_seconds: Histogram`(buckets 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)
- [ ] **4.2** `torch_bge_m3._encode_gpu` 调指标:
  - **显存(review 🟡 #3)**: 用 `torch.cuda.memory_allocated(device_id)`(更准确、无外部依赖);device_id 来自 `torch.cuda.current_device()` 或 Settings `BGE_M3_GPU_DEVICE_ID`,不用 `nvidia-smi`
  - batch size → histogram
  - wall clock latency → histogram
- [ ] **4.3** 启动恢复(13a T7 已有 boot_recovery): 加 GPU-specific:
  - `gpu_available` flag 持久化到 TaskRepo(可选, 默认 in-memory)
  - 启动时若 `BGE_M3_GPU_ENABLED=true` → 调 `try_register_gpu` 一次
- [ ] **4.4** 测试: `tests/unit/test_phase13b_t4.py::test_gpu_metrics_emit` + `test_boot_recovery_reregisters_gpu`
- [ ] **4.5** Commit: `feat(metrics): GPU encode metrics via torch.cuda (P1-3 multiproc surface)`

### T5 E2E 验收(验收 #1-#10 全集)

目标: real-container E2E, GPU 通道对 28-doc 测集实现检索等价 + 性能目标

- [ ] **5.1** `scripts/phase13b_poc_bench.py`: **28-doc 测集 = Phase 12 报告中含 1343-chunk + 2298-chunk 的子集**(review 🟢 #8, 性能数字可与 Phase 12 v10 复现对比):
  - 来源: `deployment/phase12-v10-verification.md` 列出的 28-doc 清单 → 抽 JSONL 复制到 `rag/tests/fixtures/phase13b_poc_28doc/`
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
- [ ] **5.6** Commit: `test(prod): Phase 13b E2E acceptance suite (28-doc Phase12-subset bench + equiv + failover)`

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

## 7. 任务依赖与顺序(eng-review v1.1 微调)

```
T1 (encoder 实现 + 双头)  ──→  T2 (启动自检 + Router 注册门)
   │                              │
   └────→ T4 (GPU 指标 + 启动恢复) │   (T4 与 T2 并行)
                                  ↓
                             T3 (EncodingRouter 调度 + 故障转移 + audit)
                                  ↓
                             T5 (E2E 验收套件 — Phase12 子集 28-doc)
                                  ↓
                             T6 (Plan closure)
```

**eng-review v1.1 调整**:
- T1 完成后 T2 与 T4 可并行启动(指标逻辑独立,无 GPU 也可写测试)
- T3 严格依赖 T1+T2 完成后启动(qdrant.upsert_chunks kwarg 在 T3.2 落,需 T1 的 EncodedVector 类型 + T2 的 Router 注册门)
- T5 等前 4 项全绿
- T6 最后

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
| `rag/tests/fixtures/bge_m3_self_check_probes.jsonl` | 新建(5 条探针, 4 类覆盖) |
| `rag/tests/fixtures/phase13b_poc_28doc/` | 新建(Phase 12 v10 子集 28-doc JSONL) |
| `scripts/phase12_v10_extract_28doc.py` | 新建(若 UQ-A 决议需重抽) |
| `scripts/phase13b_poc_bench.py` | 新建 |
| `scripts/phase13b_equiv_check.py` | 新建 |
| `scripts/phase13b_failover_test.py` | 新建 |
| `CHANGELOG.md` | 改: [phase13b] section |
| `rag/pyproject.toml` + `shared/pyproject.toml` | 改: version 0.4.0 → 0.5.0 |

预估 touched ≈ 17 文件 / 增量 ~1300 LOC / 4-5 人日(与 spec §9 估算一致)

---

## 9. 开放问题 / 未决项

**v1.1 拍板后状态**(eng-review 2026-08-24):

| OQ | 问题 | 决策 | 落地位置 |
|---|---|---|---|
| **OQ-1** | sparse 双头 vs 单 dense | ✅ **T1.1 双头一次返回 `EncodedVector`**(dense + sparse) | T1.1, 验收 #8 |
| **OQ-2** | T9 seam 替换路径 | ✅ **(b) 保持 module fn CPU 默认**,EncodingRouter 旁路 | T3.1 |
| **OQ-3** | 28-doc 测集来源 | ✅ **Phase 12 v10 报告含 1343-chunk + 2298-chunk 子集** | T5.1 |
| **OQ-4** | bge-m3 入仓 vs 镜像 COPY | ✅ **不入仓**,镜像构建期 COPY 或 dev 挂载; ops 确认分发链路 | 不在 13b 范围 |
| **OQ-5** | GPU 容器化 vs in-process | ✅ **in-process**(G7 修订),13c 增量才做独立容器 | T1 / T3 |
| **OQ-6** | torch PoC 失败 → ORT-GPU | ✅ **留 `BGE_M3_BACKEND` Literal hook 不实现**; 失败再开 13b-ORT 增量 | T1.2 |
| **OQ-7** | 自检探针集 | ✅ **5 条混合文本 + 4 类覆盖**(纯英 / 中文长 / 数字符号密 / 空)入仓 | T2.1 |
| **OQ-8** | `channel_switched` 抖动抑制 | ✅ **状态机 + transition-only emit**, 不加 `since_last_emit_sec` | T3.1, T3.3 |

### 仍需用户拍板的非技术问题

- **UQ-A** Phase 12 v10 报告中是否含完整的 28-doc JSONL 路径?若仅有测集清单无 JSONL, T5.1 需 `scripts/phase12_v10_extract_28doc.py` 重抽;需 ops 协助定位 `deployment/phase12-v10-verification.md` 附录
- **UQ-B** `/home/pangzy/code_project/bge-m3` 镜像分发策略 — 13b 在 dev 路径已就绪, 但生产部署时镜像如何 COPY 该 2.5GB 模型层?留作 ops 议题, 不阻塞 13b
- **UQ-C** `BGE_M3_GPU_ENABLED=true` 默认开关策略 — 当前计划默认 True(单 GPU 节点);多 worker 共 GPU 时是否降为 "动态按可用" ? 留 Phase 13c 量化

---

## 10. 关联 & 引用

- 上游: Phase 13a T9 seam `commit 145f380`, closure `e5c8f39`, tag `phase13a`
- 下游: Phase 13c(独立 GPU HTTP 容器 + k8s 多副本)留作未来 plan
- Spec: `docs/specs/phase13-gpu-encoding-channel-spec.md` v1.2(`commit f602577`)
- v10 数据: `deployment/phase12-v10-verification.md`

---

## 11. eng-review 整合记录(v1.0 → v1.1)

| Review 项 | 优先级 | 整合位置 |
|---|---|---|
| 🔴 #1 sparse 双头决策 | 高 | §1 不可妥协 + T1.1 + 验收 #8 |
| 🔴 #2 qdrant.upsert_chunks precomputed kwarg | 高 | §1 不可妥协 + T3.2 |
| 🟡 #3 指标用 torch.cuda.memory_allocated 不用 nvidia-smi | 中 | T4.2 |
| 🟡 #4 自检探针 4 类覆盖 | 中 | T2.1 |
| 🟡 #5 _init_child CUDA 上下文预热 | 中 | T1.5 |
| 🟢 #6 channel_switched 状态机 | 低 | T3.1 / T3.3 / T3.4 state-machine test |
| 🟢 #7 BGE_M3_BACKEND Literal hook | 低 | T1.2 |
| 🟢 #8 28-doc 用 Phase 12 v10 子集 | 低 | T5.1 + §9 OQ-3 |

v1.1 整合 8 项 review 反馈,8 OQ 全 RESOLVED; 新增 3 个非技术 UQ(A/B/C)需用户后续拍板。
- 验收 #10 自检基准: 仓库 vendored ONNX FP32, 13a T2 阶段镜像内置
- T9 seam 路径: `rag/ekrs_rag/services/step5_worker.py:53-95`