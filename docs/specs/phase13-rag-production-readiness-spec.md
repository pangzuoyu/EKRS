# Phase 13 Spec — RAG 入库服务生产就绪化

状态: **v0.9 draft**(2026-08-22;待 v10 F2 失败分类 + A 类重试实测数据后复核 → v1.0)
关联: Phase 12 收尾; 基线: v10 批处理(1407 doc, 含 2326-chunk 极端 doc)
工期重估: P0 全套 6-8 人日, P1 +2-3 人日(原稿 3-5 只覆盖 happy path, 见 §2 勘误)
优先级: P0 = 上线阻塞; P1 = 上线前; P2 = 后续迭代

---

## 1. 背景与问题

v10 验证了数据面正确性: chunker(行级冲刷 d37efce + 误判修正 e2b4b0e)、
微批次编码(4635e0c, 1343-chunk OCR 表正常入库)、数据路径完整。
当前模式是"离线批处理 + 脚本协调", 直接上生产会触发:

| 风险 | 触发场景 | 生产后果 |
|---|---|---|
| livenessProbe 超时 | 大 doc 编码阻塞 event loop 最长 ~10min | Pod 被杀, 悬空状态 |
| API 无响应 | 同上(encode 同步在 loop, `qdrant_client.py:199` 链) | notify HTTP=0 假失败风暴(v10 实测 200+ 条) |
| 搜索不可用 | 编码占满 CPU + loop | 搜索延迟飙升 |
| 无资源上限 | 单 doc 可生成任意 chunks(实测最大 2326) | 资源被单 doc 霸占 |

## 2. 原稿勘误(必须吸收, 否则方向错)

| # | 原稿主张 | 事实 |
|---|---|---|
| E1 | P0-2 "API 立即返回 202" 是新能力 | **notify 本来就 202 秒回**(v10 日志 22-274ms); 真问题是 pipeline 的 encode 阻塞 loop 冻结一切响应。P0-2 实际交付物 = 编码不占 loop |
| E2 | P1-2 "TaskRepo 在内存" | TaskRepo 是 **aiosqlite 持久化**(Phase 4), 且有 CompensationScanner 对账。缺的只是重启后 running→pending 恢复逻辑 |
| E3 | P1-3 "无监控" | Phase 5/5.5D 已有 Prometheus 全套(route counters/latency/failures + :9090 sidecar multiproc exporter)。只缺队列深度/编码耗时 2-3 个指标 |
| E4 | status 换 task_id 键控 | **破坏契约**: doc-to-md 联调 + Phase 9 脚本都按 doc_hash 查 status。保持 doc_hash 键控, 仅增补 queued/running 状态 |
| E5 | `future._process` 强杀子进程 | concurrent.futures **无此 API**(future 不映射到进程)。用 **pebble.ProcessPool**(原生 per-task timeout + SIGKILL)或手写 multiprocessing.Process |
| E6 | signal.alarm 做主超时 | 4635e0c 调查实证: ORT 持 GIL 时 **Python 信号 handler 与 faulthandler watchdog 都不执行**。唯一可靠层 = 父进程 wait_for + 子进程 SIGKILL; 子进程内 alarm 只能当可选的快速失败优化 |
| E7 | ProcessPoolExecutor 默认 fork | ORT 会话含线程池, **fork 后 UB**。必须 spawn → 每 worker 独立加载模型(~90s 启动, 2.2GB/进程)。内存预算: 2 worker ≈ 8-10GB(模型×2 + arena), 20GB 容器下 max_workers 与内存的约束表必须先写 |
| E8 | worker 只做 encode_and_upsert | 必须执行 pipeline Step 5 **全段**: encode → Qdrant upsert → **FTS replace_doc 配对写**(T10a-2, R7/R8) → delete_old_versions(Range(lt))。只编码不配对写会制造 FTS↔Qdrant drift |
| E9 | 超时 600s | 2326-chunk doc 预计 10-15min(待 A-retry 实测)。分层: 每子批 120s + 全任务 1800s(初值, F2 后校准) |
| E10 | 阈值 MAX_CHUNKS=1500 | 会拒掉 v10 已成功入库的 2326-chunk doc。阈值是**产品决策**, 待 F2 出真实分布后定 |

## 3. P0 详细设计(修正版)

### P0-1 健康检查解耦
- `/healthz`: 仅进程存活 + uptime; **依赖 P0-2**(loop 解除阻塞前, 换 handler 无效) → 两项同批交付
- `/ready`(新): ping Qdrant/Redis, 不可用 503, K8s 摘流量; docker-compose healthcheck 同步改
- 验收: 编码 2326-chunk 期间 /healthz P99 < 100ms; Qdrant 停 → /ready 503

### P0-2 编码异步化(worker 进程隔离)
- `EncodingWorker`: asyncio.Queue + **pebble** ProcessPool(spawn, max_workers=2)
- worker 函数 = pipeline Step 5 全段(见 E8), 入口保留幂等键 `md5(trace_id|doc_hash|version)` skip + RedisLock 跨 worker 互斥
- notify 路由不变(202 + request_id); status 保持 doc_hash 键控, 状态机 pending→queued→running→terminal(IngestionOutcome 映射不动)
- TaskRepo 记队列态; 重启恢复 = running→pending 重置(扩展现有 CompensationScanner)
- 验收: /notify < 200ms 恒定; 2326-chunk ≤ 15min 完成且期间 /healthz 正常; golden 208 零退化; **FTS↔Qdrant count 一致**(drift 检测器静默)

### P0-3 分层超时
- 父进程: `asyncio.wait_for(future, 全任务 1800s)` 超时 → pebble SIGKILL 子进程 → outcome=failed(可重试) + 审计
- 子进程内(可选优化): 每子批 120s alarm 快速失败
- 验收: 注入死锁(复现 4635e0c 场景)→ 任务 1800s 内进 timeout, 进程回收, 服务不冻结, 可重试

### P0-4 准入控制
- 入口预检: n_blocks / total_raw_chars / estimated_chunks(复用 pick_bundles 估算模式)
- 超限 → **IngestionOutcome("rejected") + 审计事件**(走 outcome 映射, 不裸 403 — 保持路由层契约)
- 过载 503 只看队列深度(>10); CPU 采样去掉(噪声)
- 阈值初值: chunks ≤ 1500 / raw ≤ 1M chars, **F2 后按分布定稿**(见 E10)
- 验收: 超限 doc 收到明确 rejected + 审计落盘; 队列深 10 时新请求 503

## 4. P1 / P2

**P1-1 并发**: max_workers=2 × intra_op=4 = 8 编码线程(20 核 OK); 内存约束表先行(E7)
**P1-2 持久化恢复**: TaskRepo 重启恢复逻辑 + 24h 失败任务清理
**P1-3 指标**: 新增 `rag_task_queue_depth`(Gauge) / `rag_task_duration_seconds`(Histogram, 含 timeout 桶) / `rag_doc_rejections_total`(Counter, reason 维度); 注意 pebble 子进程指标需 multiproc 共享目录汇聚
**P1-4(新) 查询侧 encode**: retriever 的 query embedding 也在 loop(单条 ~100ms) — P0-2 后成为 loop 上最后一块同步推理, to_thread 化
**P2-1 大 doc 预拆分**(注意: 改变 chunk 顺序 → chunk_id 变 → 必须 version bump 走重入库, T10a-5 round-trip 保持)
**P2-2 可中断优先级队列**(搜索优先) **P2-3 HPA**

## 5. 不变量保持清单(原稿缺失, 违反即回退)

- R1-R8 七铁律全程不变(尤其 R7 scope_path / R8 索引层只滤非法)
- `IngestionOutcome` frozen 契约 + 路由层 outcome→TaskRepo 映射(Phase 6A)
- 新审计事件走 4 步: `_EVENT_SCHEMAS` 注册 + write-site + handbook §16 inventory + 真实 AuditWriter 回归测试
- `_send_callback` tenacity 语义(4xx 不重试/5xx 重试)经 worker 的 callback 保留
- 幂等键 / delete_old_versions(Range lt) / FTS 配对写 / RedisLock
- 与 doc-to-md 契约: data.jsonl 路径语义、X-Parser-Token、status 按 doc_hash — 均不动

## 6. 迁移路径
1. 代码(TDD, 每项独立 commit) → 本地用 v10 真实 bundle 子集集成测(含 1343/2326-chunk 样本)
2. staging 灰度: 关探针跑 v10 同数据对比一致性(Qdrant/FTS count + golden + replay)
3. 生产前: 开探针 + 故意 kill 验恢复 + 50 并发压测 + 搜索延迟旁路测量
4. 切流 10% → 24h → 100%; 旧版回滚通道保留 1 周

## 7. 最终验收清单
功能: 202 秒回 / status 实时态 / 2326-chunk 不阻塞 / healthz<100ms / 超时可杀可重试 / 超限拒绝
性能: 吞吐 ≥ 60 docs/h(v10 基线不降) / 入库高峰搜索 P99 ≤ 2s / Pod 30s Ready
可靠: 探针 1h 零失败 / 重启任务不丢 / drift 检测器静默 / golden 208 零退化

## 8. 参考 commit
微批次 4635e0c · 行级冲刷 d37efce · 误判修正 e2b4b0e · notify 退避 (60s backoff commit) ·
分类器 `scripts/classify_ingest_failures.py` · F2 计划 docs/superpowers/plans/2026-08-21-phase12-followups.md

## 9. 待决问题(F2/A-retry 后复核)
1. 2326-chunk 实际编码时长 → 校准 P0-3 分层超时值
2. ~~pebble 依赖受限网络可用性~~ **已验证(2026-08-22)**: aliyun 镜像含 pebble 5.2.1(34KB 纯 Python wheel 零编译依赖), rag Dockerfile 的 `PIP_INDEX_URL` build-arg(Phase 8 T8-3a)原生支持镜像覆盖 → pebble 路线定为主案, 手写 Process 降为 fallback 不预期使用
3. 全量 chunk 分布 P99/P999 → 定稿 P0-4 阈值(E10)
4. 大 doc 假失败的 A 类重试成功率 → 验证 60s 退避是否足够
5. 吞吐对比(60 docs/h)与 worker 数/内存实测 → 定 max_workers
6. monster doc 入库后 recall 影响 → 联动 F3(quality_warning 占比)决定是否收紧阈值
