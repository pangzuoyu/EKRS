# Phase 13 增补 Spec — GPU 加速通道 + CPU Fallback 架构

状态: **v1.1**(2026-08-23;整合用户三项修正 — CLS pooling / 方案 A 双头输出 / 离线模型+启动自检;勘误史见 §0)
关联: docs/specs/phase13-rag-production-readiness-spec.md(P0 主 spec); Phase 12 v10 数据
决策记录: 验收标准 = **检索等价**(Top-10 召回一致), 不要求向量数值一致(2026-08-23 user)

---

## 0. 原稿勘误(不改必翻车)

| # | 原稿 | 事实/修正 |
|---|---|---|
| G1 | 示例 `last_hidden_state.mean(dim=1)` | **mean pooling ≠ bge-m3 的 CLS pooling**。现行 OnnxBgeM3 用 ONNX 导出的 `sentence_embedding`(XLM-R `<s>` CLS)。GPU 路径必须 `last_hidden_state[:, 0]` + L2 normalize, 否则全部向量系统性偏移, 检索等价验收 0 分 |
| G2 | GPU 服务只返回 dense | **缺 sparse 头**。EKRS 是混合检索(dense+sparse RRF, T10a 系列): `EncodedVector(dense, sparse)` 是 upsert 硬契约。GPU 服务必须同时产出 lexical weights(BAAI learned 头 `relu(W_lex·h+b)`, 权重就在仓库 `sparse_linear.pt` — 本身是 torch 文件, 移植零成本) |
| G3 | "2326-chunk doc CPU 编码 8-12min" | 2326 是 raw/500 估算值, 实际 648 chunks。真实极端: **7787 chunks ≈ 26min**(retry2 实测串行车队 5h+ 连续阻塞), 1343-chunk 实测 143s。动机数据以此为准 |
| G4 | Dockerfile 从 HF 拉 `BAAI/bge-m3` | **受限网络 + 仓库未含 pytorch 权重**(vendored 的是 2.2GB ONNX; 当年做 OnnxBgeM3 就是为了不依赖 2.1GB pytorch_model.bin)。两条路: (a) **ORT CUDAExecutionProvider** — 直接复用 vendored ONNX + 现有代码只换 provider, 零模型来源问题, 选型理由"ORT GPU 慢于 torch FP16"在实际收益面前需重估; (b) torch 路径需 hf-mirror 镜像拉权重入仓。**建议 13a 首日先做 (a) 的 PoC 基准再定** |
| G5 | `pip install flash-attn --no-build-isolation` | runtime 基础镜像无 nvcc, 源码编译 30-60min 且受限网络大概率失败。须用预编译 wheel(官方 index 按 torch/cuda 版本匹配)或 `attn_implementation="sdpa"`(PyTorch 2.3 内置, 性能约为 FA2 的 85-90%)兜底 |
| G6 | "Top-10 召回结果**完全一致**" | FP16 vs FP32 边界 rank 翻转是统计必然, 完全一致大概率过不了。建议保留完全一致为主标准但预置回退线: **Top-10 重合率 ≥95% 且 recall@10 差 ≤1pp**(余弦相似度分布 ≥0.999 作过程指标, 不做门槛 — 同原稿 §8.2) |
| G7 | "GPU 容器独立 /healthz" | 与 P0-2 关系要理顺: 调度层(EncodingRouter)**就是** P0-2 worker 的 encode 步骤 — 不是又一层。GPU 路径天然解决 P0-1(loop 不被占), CPU fallback 仍走 pebble 进程池 |
| G8 | 主 spec 路径引用 | 实际: `docs/specs/phase13-rag-production-readiness-spec.md` |

## 1. 背景与动机(v10 实测口径)

| 瓶颈 | 实测表现(2026-08-23 修正口径) |
|---|---|
| 编码速度 | 实测 **9-48 chunks/s** 区间(48 = 2298-chunk doc 空闲态 47.2s; 9.4 = 1343-chunk 病态 doc 143s); 7787-chunk 极端 doc ≈ **3-14min** |
| 资源隔离 | retry2 全灭根因 = **队列效应**: ~30 个落地 doc × 每个 1-5min 串行编码 ≈ 数小时连续 loop 阻塞, healthz 超时 + notify HTTP=0 风暴(v10-v13 累计 A+B 类 ~400 条假失败, **C 类真失败恒 0**) |
| 吞吐 | v10 全程 ~0.5 docs/min(含停机), chunk 吞吐 ~4000/h; 单 doc 幂等 skip 路径 <5s |

CPU 方案(ONNX 4 线程 + 微批次)正确性已验证(5724-chunk 成功入库), 速度是物理上限 → GPU 主通道 + CPU 备用。

## 2. 设计目标

| 目标 | 指标 |
|---|---|
| GPU 吞吐 | 7787-chunk doc ≤ 3min; 1343-chunk ≤ 30s |
| 8GB 显存 | 单批峰值 ≤ 6GB(留 2GB 余量) |
| 可靠性 | GPU 故障 30s 内切 CPU, 服务不中断 |
| 兼容 | chunker/微批次/Qdrant+FTS 双写/quality_warning 全不动 |
| 检索等价 | 见 §8(含 G6 回退线) |

## 3. GPU 主通道

### 3.1 选型(修正后)

| 组件 | 选型 | 说明 |
|---|---|---|
| 后端 | **torch FP16 优先**(2026-08-23 user): ① 社区实证快于 ONNX GPU; ② `sparse_linear.pt` 本就是 torch state_dict, torch 侧零适配; ③ retry2 的 28-doc 即测集。**torch FP16 在 8GB 显存稳定且性能达标 → 直接跳过 ORT-GPU**; 仅当 torch 路径不可行(权重拉取/显存)才回退 onnxruntime-gpu CUDA EP(复用 vendored 模型, 零来源问题) |
| 精度 | FP16(b 路径) | 权重 1.1GB |
| Attention | sdpa 兜底, FA2 预编译 wheel 可得则用 | G5 |
| Pooling | **CLS + L2 normalize**(G1) | 与 ONNX sentence_embedding 对齐 |
| Sparse | **learned 头 relu(W·h+b), 仓库 sparse_linear.pt**(G2) | 输出 {token_id: weight} 字典, 与 EncodedVector.sparse 同型 |
| Batch | 32 起步, 64 需显存实测 | seq 512 与微批次一致 |

### 3.2 服务要点(修正后伪码, v1.1)

```python
model = AutoModel.from_pretrained(MODEL_DIR, torch_dtype=torch.float16).cuda().eval()
# MODEL_DIR = 仓库内模型目录(构建期经 hf-mirror 预下载入仓/挂载), 不直连 HF

@router.post("/encode")
async def encode(texts: list[str]) -> dict:          # 返回 dense+sparse 双头(方案 A)
    for batch in split(texts, MAX_BATCH_SIZE):
        inputs = tok(batch, padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.no_grad():
            h = model(**to_cuda(inputs)).last_hidden_state
        dense = l2_norm(h[:, 0])                     # CLS pooling (G1) — 与官方 ONNX 一致
        sparse = relu(h @ W_lex.T + b_lex)           # BAAI learned 头 (G2) — 权重=仓库 sparse_linear.pt
        # 验证锚点: dense 与 ONNX FP32 输出余弦 ≥0.999
    torch.cuda.empty_cache()
    return {"dense_vecs": ..., "lexical_weights": ...}   # 兼容 OnnxBgeM3.encode 返回型
```

**sparse 方案 A 定案依据**: 不需要 TF-IDF/BM25 降级 — CPU 路径现用的 BAAI learned 头
(`relu(W_lex·h+b)`)权重文件 `sparse_linear.pt` 就在仓库(3.4KB, 本身是 torch state_dict),
GPU 侧加载同一文件即得与 CPU 逐 token 对齐的 sparse, 实现成本低于任何替代方案且天然满足验收 #8。

**启动自检(验收 #10)**: 容器启动后对固定探针文本集跑 GPU FP16 vs 仓库 vendored ONNX FP32
(即 CPU 容器同款模型, 勿用第三方 ONNX 导出), dense 余弦 ≥0.999 才把 GPU 通道注册进 Router;
失败/模型缺失 → GPU 通道不注册, 全量纯 CPU(网络降级路径, 与 §11 回滚一致)。

### 3.3 GPU 指标
`gpu_memory_used_bytes` / `gpu_memory_peak_bytes` (Gauge) / `encode_batch_size` / `encode_latency_seconds` (Histogram) — 经 P1-3 multiproc 目录汇聚。

## 4. CPU 备用通道
零代码复用现状: ONNX 4 线程 + 微批次 64 + 行级冲刷 + 动态超时 + Qdrant/FTS 双写 + quality_warning。
切 CPU 条件: GPU 故障 / OOM / 队列深 >10 / **doc 实际 chunks > GPU 通道上限**(与 P0-4 联动, 见 G7)。

## 5. 调度层(EncodingRouter = P0-2 worker 的 encode 步骤)

- 健康: 每 30s 探 GPU /healthz, 2 次失败标记降级; 恢复探活自动回主通道
- 路由: GPU 可用且队列 ≤10 → GPU; 否则 CPU(pebble 池)
- 超时: GPU 单请求 30s(其内部自拆批), 全任务沿用 P0-3 分层超时
- 切换审计: channel_switched 事件(走审计 4 步注册)

## 6. 部署架构
Router(1C/2G) → GPU Worker(2C/8G/**8GB 显存专用, k8s gpu=1**) + CPU Fallback(8C/16G, 可横扩) → Qdrant+FTS。
GPU Worker 与 CPU 容器**不共享 GPU**(显存竞争)。

## 7. 性能预期(保守)
CPU 基线(2026-08-23 实测): 48 chunks/s 空闲态 / 9.4 chunks/s 病态 doc; 单条 ~0.1s。
GPU 目标: 单条 30-50ms; 2298-chunk ≤5s; 7787-chunk ≤30s; 稳态 10-15 docs/min。
冷加载 10-30s(2.2GB 权重, 预加载)。

## 8. 验收标准

| # | 项 | 标准 |
|---|---|---|
| 1 | 检索等价 | **Top-10 差异 ≤1%(业务可接受阈值)** — recall@10 黄金集上 GPU vs CPU 通道的 Top-10 重合率 ≥99%; 仅当差异导致**真实召回下降超阈值**才触发回退(G6); 可解释的 FP16/FP32 数值漂移(余弦 ≥0.999, 过程指标)不触发 |
| 2 | 显存 | 7787-chunk 编码期峰值 ≤6GB 无 OOM |
| 3 | 性能 | 7787-chunk ≤30s; 2298-chunk ≤5s(基线: CPU 实测 47.2s) |
| 4 | 故障转移 | GPU 容器 kill → 30s 内切 CPU 不中断 |
| 5 | 过载 | GPU 队列 >10 溢出 CPU |
| 6 | 回归 | golden 208 零退化 |
| 7 | 稳定性 | 连续 2h / 2000+ doc 无 OOM 无碎片化降速 |
| 8 | **sparse 完整性**(方案 A) | GPU 服务返回 dense+sparse 双头; sparse 与 CPU 路径(ONNX learned 头)在非零 token 索引/值上一致或召回等价(重合率 ≥95%) |
| 9 | **Pooling 一致性** | 采样集上 GPU 与 CPU 通道 CLS 向量余弦相似度 ≥0.999(两侧均 CLS pooling) |
| 10 | **模型加载自检** | 容器启动 ≤30s 完成加载, 自检脚本(vs 仓库 vendored ONNX FP32)余弦 ≥0.999 方可接流量; 失败则不注册 GPU 通道, 纯 CPU 降级 |

### 8.2 向量一致性策略(2026-08-23 user 决策)
不要求 GPU(FP16)与 CPU(FP32)数值一致, 以检索等价为验收。理由: 检索对微小误差不敏感; 强制一致牺牲性能; recall@10 是既有业务指标。

## 9. 实施计划(修正)
- **13a** GPU 容器 + **torch FP16 PoC 优先**(通过则跳过 ORT-GPU; 测集=retry2 28-doc, G4) — 1-2 天
- **13b** 调度层(合并进 P0-2 worker, G7) — 0.5-1 天
- **13c** 集成测试 + 检索等价验收(含 sparse, G2/G8) — 0.5 天
- **13d** 压测调优(batch/显存/泄漏) — 0.5 天
共 3-4 人日, 与 P0 并行正交。

## 10. 与 P0 整合
P0-1: GPU 路径天然解耦 loop / P0-2: Router 即 worker encode 步 / P0-3: GPU 请求 30s + 全任务分层 / P0-4: 准入加"显存可用性"+ **chunks>GPU 上限走 CPU 通道或拒绝**(与 post-chunk 实数检查联动, 主 spec 修订)。

## 11. 回滚
GPU 持续故障 → gpu_health=false 全走 CPU(= v10 架构)。OOM → batch 降 16 或大 doc 溢出 CPU。性能不达 → 切流回退, GPU 转离线批处理加速器。

## 12. 参考
微批次 4635e0c · 行级冲刷 d37efce+e2b4b0e · P0 主 spec `docs/specs/phase13-rag-production-readiness-spec.md` · v10 验证报告 `deployment/phase12-v10-verification.md`(生成中) · FA2 github.com/Dao-AILab/flash-attention

## 13. 增补记录
2026-08-23 v1.0-draft: 用户增补稿 + 8 项勘误(G1 pooling/G2 sparse 为阻断级); 检索等价验收已含回退线预置。
2026-08-23 v1.1: 用户三项修正整合 — CLS pooling 定稿(验收 #9); sparse 方案 A 定案(验收 #8, 定案依据=仓库 sparse_linear.pt 即 BAAI learned 头, 免 TF-IDF 降级); 离线模型+启动自检(验收 #10, 自检基准=仓库 vendored ONNX 非第三方导出, 构建期 hf-mirror 预下载)。
