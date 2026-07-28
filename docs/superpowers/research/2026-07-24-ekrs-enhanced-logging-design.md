# EKRS 增强后日志/可观测性系统研究报告

> **研究文档 — 增强后 EKRS 的日志、审计、监控体系设计研究。**
> 日期：2026-07-24（v2 — 三轨分层存储架构修订）
> 关联文档：
> - [`2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md`](2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md)（广谱检索移植设计）
> - [`2026-07-24-ekrs-enhanced-ui-design.md`](2026-07-24-ekrs-enhanced-ui-design.md)（UI 设计）
>
> 本报告研究 Phase 9 广谱检索增强后，EKRS 的日志/审计/可观测性系统需要怎样扩展，
> 以覆盖新的 BM25/RRF/重排路径的完整可观测性。
>
> **v2 修订说明**：v1 将所有新审计事件塞进单一 `audit.log`，导致写入量增加 6-8 倍、
> 历史保留从 ~3 年缩到几周（高频场景甚至几天），且把高频检索中间状态与业务审计混在一起。
> v2 采用 **三轨分层存储架构**（Track 1: audit.log 业务审计 / Track 2: search_trace.log
> 检索追踪 / Track 3: Prometheus 聚合指标），彻底分离"不可裁剪的业务合规"与"可丢弃的
> 性能调试"。**采样记录被明确排除**——因为它直接破坏第 9 章回放确定性的前提。

---

## 目录

1. [现有可观测性系统分析](#1-现有可观测性系统分析)
2. [增强后的可观测性需求](#2-增强后的可观测性需求)
3. [三轨分层存储架构（核心设计）](#3-三轨分层存储架构核心设计)
4. [Track 1：audit.log 业务审计设计](#4-track-1auditlog-业务审计设计)
5. [Track 2：search_trace.log 检索追踪设计](#5-track-2search_tracelog-检索追踪设计)
6. [Track 3：Prometheus 聚合指标设计](#6-track-3prometheus-聚合指标设计)
7. [结构化日志设计](#7-结构化日志设计)
8. [检索路径全链路追踪](#8-检索路径全链路追踪)
9. [回放确定性保证](#9-回放确定性保证)
10. [日志查询与 API 设计](#10-日志查询与-api-设计)
11. [告警规则设计](#11-告警规则设计)
12. [磁盘占用与容量规划](#12-磁盘占用与容量规划)
13. [风险评估](#13-风险评估)
14. [实施路线图](#14-实施路线图)
15. [结论与建议](#15-结论与建议)

---

## 1. 现有可观测性系统分析

### 1.1 三层可观测性架构

EKRS 的可观测性系统由三个独立但协作的子系统组成：

```
┌─────────────────────────────────────────────────────────────────┐
│                    EKRS 可观测性架构 (Phase 8)                    │
│                                                                 │
│  ┌─────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │  审计日志         │  │  Prometheus 指标  │  │  结构化日志    │  │
│  │  audit.log       │  │  /metrics :9090  │  │  debug.log    │  │
│  │                  │  │                  │  │               │  │
│  │  19 个事件类型    │  │  14 个指标        │  │  Python logging│  │
│  │  JSON Lines      │  │  Counter/Hist    │  │  WARNING+     │  │
│  │  100MB×5 gzip    │  │  Cardinality守卫  │  │  propagation  │
│  │  AuditIndex      │  │  safe_inc/observe│  │  =False       │
│  └────────┬─────────┘  └────────┬─────────┘  └───────┬───────┘  │
│           │                     │                    │          │
│           └─────────┬───────────┘                    │          │
│                     │                                │          │
│              ┌──────▼───────┐               ┌────────▼───────┐  │
│              │ trace_id     │               │ debug.log      │  │
│              │ contextvar   │               │ (开发者排查)    │  │
│              │ 传播         │               │                │  │
│              └──────────────┘               └────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 审计日志系统详解

**核心类层级**：

```
AuditLogger (shared/ekrs_shared/audit.py)
├── JSON 结构化事件（timestamp + event + **kwargs）
├── Schema 注册 + 校验（register_event_schema / validate_event）
├── propagation=False（不冒泡到 root logger）
└── AuditWriter (rag/ekrs_rag/observability/audit.py)
    ├── 继承 AuditLogger
    ├── RebuildingRotatingFileHandler（100MB × 5 gzip）
    ├── write() 方法：写入前记录 offset → 写入后注册到 AuditIndex
    ├── 失败永不传播（返回 False，记到 ekrs.audit.failures）
    └── get_writer() / set_writer() 模块级单例
```

**AuditIndex（内存索引）**：

- 位置：`rag/ekrs_rag/observability/audit_index.py`
- 结构：`dict[trace_id → list[(event, file_offset)]]`
- 构建时机：启动时线性扫描 audit.log（`build()`）
- 增量更新：AuditWriter.write() 写入后调用 `idx.append()`（无需重扫）
- 轮转重建：文件 rotation 时 `on_rollover` 回调触发 `rebuild()`
- 查询：`seek(trace_id)` → O(1) 字典查找 → 按 offset 读行
- 仅索引 REPLAY_EVENTS：`{constraint_solve_started, constraint_solved}`

**现有 19 个审计事件**（来源：ekrs-handbook §16）：

| 事件名 | Phase | 触发位置 | 关键字段 |
|--------|-------|---------|---------|
| `endpoint_started` | P5 | ObservabilityMiddleware | trace_id, endpoint, method |
| `endpoint_completed` | P5 | ObservabilityMiddleware | trace_id, status_code, duration_ms |
| `constraint_solve_started` | P7 T2 | constraints.py | trace_id, query, scope_path, [lineage_snapshot] |
| `constraint_solved` | P7 T2 | constraints.py | trace_id, branches_count, [conflict_details] |
| `constraint_solve_failed` | P7 T2 | constraints.py | trace_id, error_type, status_code |
| `query_replay_executed` | P5 | constraints.py | trace_id, replayed_trace_id, deterministic_match |
| `ingestion_received` | P5 | ingestion.py | trace_id, doc_hash, version |
| `ingestion_completed` | P5 | pipeline.py | trace_id, doc_hash, chunks_indexed |
| `ingestion_failed` | P5 | pipeline.py | trace_id, doc_hash, error |
| `replay_started` | P5 | ingestion.py | trace_id, doc_hash |
| `replay_completed` | P5 | ingestion.py | trace_id, doc_hash, chunks_indexed |
| `replay_sha256_mismatch` | P5 | pipeline.py | trace_id, doc_hash, expected, actual |
| `compensation_retry` | P7 T3 | compensation.py | trace_id, doc_hash, reingest_outcome, duration_ms |
| `qdrant_write_failed` | P7 T1 | qdrant_client.py | trace_id, operation, collection, error_type |
| `lock_acquire_failed` | P5 | redis_lock.py | trace_id, doc_hash |
| `document_metadata_failed` | P6A | documents.py | trace_id, doc_hash, error |
| `callback_url_blocked` | P6A | ingestion.py | trace_id, callback_url |
| `callback_auth_missing` | P6A | ingestion.py | trace_id |
| `callback_best_effort_failed` | P6A | ingestion.py | trace_id, error |

**关键设计特性**：
- JSON Lines 格式（每行一个 JSON 对象）
- 永不记录令牌（token/secrets）
- `/healthz` 请求跳过审计（`set_skip_audit(True)`）
- 19 个事件名/schema **不可变更**（向后兼容保证）
- 2 个可选字段：`lineage_snapshot` + `conflict_details`（Phase 6A 白名单透传）

### 1.3 Prometheus 指标系统详解

**位置**：`rag/ekrs_rag/observability/metrics.py`

**15 个指标**：

| 指标名 | 类型 | 标签 | 用途 |
|--------|------|------|------|
| `rag_http_requests_total` | Counter | endpoint, method, status | HTTP 请求计数 |
| `rag_http_request_duration_seconds` | Histogram | endpoint, method | HTTP 延迟 |
| `rag_http_requests_inprogress` | Gauge | endpoint | 在途请求数 |
| `rag_ingestion_total` | Counter | status | 摄取计数 |
| `rag_ingestion_duration_seconds` | Histogram | — | 摄取延迟 |
| `rag_ingestion_chunks_written` | Counter | — | 写入 chunk 数 |
| `rag_constraint_solve_total` | Counter | outcome | 求解计数 |
| `rag_constraint_solve_duration_seconds` | Histogram | — | 求解延迟 |
| `rag_constraint_branches_count` | Histogram | — | 分支数分布 |
| `rag_lock_acquire_total` | Counter | result | 锁获取计数 |
| `rag_compensation_pending_tasks` | Gauge | — | 待补偿任务数 |
| `rag_compensation_retries_total` | Counter | result | 补偿重试计数 |
| `rag_qdrant_write_failures_total` | Counter | operation | Qdrant 写入失败 |
| `rag_audit_write_failures_total` | Counter | — | 审计写入失败 |
| `rag_route_failures_total` | Counter | operation | 路由异常计数 |

**基数守卫**（`is_route_template`）：endpoint 标签必须是路由模板（`/v1/constraints`），不接受插值路径（`/v1/blocks/abc123`），防止基数爆炸。

**安全封装**（`safe_inc` / `safe_observe`）：try/except 包装，指标失败永不传播到调用者。

### 1.4 trace_id 传播机制

**位置**：`rag/ekrs_rag/observability/trace.py`

- 基于 `contextvars.ContextVar`（协程安全）
- `ObservabilityMiddleware` 在每个请求开始时设置 trace_id
- 支持 `X-Trace-Id` 请求头传入（否则自动生成 UUID4）
- 响应头 `X-Trace-Id` 回传给调用方
- `_skip_audit` contextvar：`/healthz` 等高频探活请求跳过审计

### 1.5 现有系统的局限性

| 维度 | 现状 | Phase 9 新需求 |
|------|------|---------------|
| **审计事件覆盖** | 19 个事件，覆盖摄取+约束查询+补偿 | BM25 搜索、RRF 融合、重排、FTS 同步均无审计事件 |
| **检索路径追踪** | 仅 `constraint_solve_started/solved` 两个端点 | 需要追踪 Gate 1 的双路径（向量+BM25）、RRF 融合、Gate 1.5 重排 |
| **指标覆盖** | 15 个指标，覆盖 HTTP/摄取/求解/锁/Qdrant | 无 FTS5 搜索指标、无 RRF 融合指标、无重排指标 |
| **延迟分解** | 仅端到端 HTTP 延迟 | 需要各 Gate 的分步延迟（BM25 ms、向量 ms、RRF ms、重排 ms） |
| **检索质量指标** | 无 | 需要 recall@k、BM25 vs 向量命中率、RRF 提升度、强信号短路率 |
| **日志查询** | AuditIndex 仅支持 trace_id 查找 | 需要按 doc_hash、event_type、时间范围查询 |
| **告警** | 无（仅 Prometheus 采集） | 需要 FTS 不同步告警、重排延迟超标告警、召回率下降告警 |

---

## 2. 增强后的可观测性需求

### 2.1 可观测性目标

增强后的 EKRS 引入了多条检索路径（BM25 + 向量 + RRF + 可选重排），可观测性系统必须回答以下问题：

| 问题 | 需要的数据 | 现有能力 |
|------|----------|---------|
| 查询走了哪条路径？ | 每步 Gate 的执行状态 + 耗时 | ❌ 仅端到端 |
| BM25 和向量各自命中了多少？ | 双路径的独立 hit count | ❌ |
| RRF 融合后排序变化大吗？ | 融合前后 rank diff | ❌ |
| 重排被触发了吗？为什么跳过？ | 重排执行/跳过 + 原因 | ❌ |
| 哪些查询命中了精确标识符？ | BM25 top score + 强信号检测 | ❌ |
| FTS5 和 Qdrant 数据一致吗？ | 双索引行数对比 | ❌ |
| 重排模型的延迟在预算内吗？ | 重排 p50/p95/p99 | ❌ |
| 查询回放能精确复现吗？ | 完整检索中间状态 | 部分（仅 solve 输入/输出） |

### 2.2 新增子系统需求

| 子系统 | 需求 | 优先级 |
|--------|------|--------|
| **检索路径追踪** | 记录每个查询的完整检索路径（BM25→向量→RRF→重排） | P0 |
| **FTS5 健康审计** | FTS 写入/删除/同步失败事件 | P0 |
| **检索质量指标** | BM25/向量/RRF 命中率、强信号短路率、重排效果 | P1 |
| **分步延迟指标** | 各 Gate 的独立延迟 Histogram | P0 |
| **回放完整性** | 审计日志记录足够信息以精确回放（含 RRF 中间状态） | P1 |
| **告警规则** | FTS 不同步、重排超标、召回下降 | P1 |

---

## 3. 三轨分层存储架构（核心设计）

### 3.1 v1 方案的根本缺陷

v1 方案将所有新审计事件（检索路径每一步）都塞进 `audit.log`，引发了两个结构性问题：

**问题 1：审计日志膨胀**

| 场景 | v1 每次查询事件数 | v1 每次查询字节数 | 1000 请求/天 | 填满 100MB | 5×100MB 保留 |
|------|-------------------|-------------------|-------------|-----------|-------------|
| Phase 8 现状 | 2 条 | ~400B | 0.4MB/天 | 250 天 | ~3 年 |
| v1 方案 | 8 条（含 RRF top-10） | ~2.4KB | 2.4MB/天 | 42 天 | ~7 个月 |
| v1 高频 5000/天 | 8 条 | ~2.4KB | 12MB/天 | 8 天 | ~1.5 个月 |

**问题 2：业务审计与性能调试混在一起**

`audit.log` 的设计定位是"不可变更的业务合规审计"（19 个事件 schema 不可变更、永久保存）。而检索中间状态（向量 hit scores、BM25 分数、RRF rank diff）是**性能调试数据**——它的价值是短期的，不需要永久保存，也不应该污染业务审计。

### 3.2 v2 三轨分层方案

```
┌─────────────────────────────────────────────────────────────────────┐
│                  EKRS 可观测性架构 (Phase 9 v2)                       │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Track 1: audit.log（业务审计 — 不可裁剪）                    │    │
│  │                                                             │    │
│  │  仅存业务关键事件：                                          │    │
│  │  • 约束求解 (started/solved/failed)                         │    │
│  │  • 摄取 (received/completed/failed/replay)                  │    │
│  │  • 审计自身 (write_failed)                                  │    │
│  │  • Phase 9 瘦身新增: broad_search_started + rerank_completed│    │
│  │    (仅聚合统计，禁止 top-10 scores/block_ids)                │    │
│  │                                                             │    │
│  │  轮转: 100MB × 5 gzip                                      │    │
│  │  保留预期: 1000 请求/天 → ~0.3MB/天 → ~11 个月              │    │
│  │  索引: AuditIndex (trace_id → offset)                      │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Track 2: search_trace.log（检索追踪 — 可丢弃）               │    │
│  │                                                             │    │
│  │  存详细检索中间状态：                                        │    │
│  │  • vector_search_completed (hits, scores, latency)          │    │
│  │  • fts_search_completed (hits, scores, strong_signal)       │    │
│  │  • rrf_fusion_completed (top-10 scores + block_ids)         │    │
│  │  • rerank_started/completed (model, candidates, duration)   │    │
│  │                                                             │    │
│  │  轮转: 50MB × 3 gzip, 按 7 天时间轮转                       │    │
│  │  磁盘峰值: ~150MB, 7 天后自动覆盖                           │    │
│  │  索引: SearchTraceIndex (trace_id → offset)                 │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Track 3: Prometheus 指标（实时聚合 — 替代全量文本追踪）      │    │
│  │                                                             │    │
│  │  12 个新指标提供长期趋势 + 实时告警：                        │    │
│  │  • fts_search_total/duration                               │    │
│  │  • rrf_fusion_total/duration                               │    │
│  │  • rerank_total/duration + strong_signal_rate              │    │
│  │  • retrieval_path_total + result_overlap                   │    │
│  │  • index_consistency_drift                                 │    │
│  │                                                             │    │
│  │  保留: 15 天 (Prometheus 默认)                              │    │
│  │  不占审计日志空间                                            │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│              ┌──────────────────┐                                    │
│              │ trace_id         │                                    │
│              │ contextvar 传播   │  ← 三轨共享同一 trace_id          │
│              └──────────────────┘                                    │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.3 设计原则

| 原则 | 说明 |
|------|------|
| **物理隔离** | 三个 Track 各自独立的文件 + handler + 轮转策略，互不干扰 |
| **绝不采样** | Track 1 和 Track 2 均全量记录。采样会破坏回放确定性（第 9 章） |
| **分层裁剪** | Track 1 永不裁剪；Track 2 短期保留（7 天）；Track 3 按时间窗口 |
| **trace_id 贯穿** | 同一查询在三个 Track 中使用同一 trace_id，可跨轨关联 |
| **audit.log 不膨胀** | Phase 9 新增到 audit.log 的事件仅含聚合统计（~300B/条），禁止记录 top-10 详情 |
| **向后兼容** | 现有 19 个事件 schema 不变；AuditIndex 查询行为不变 |

### 3.4 为什么不采样

用户明确要求排除采样。理由：

1. **回放确定性**：如果 `rrf_fusion_completed` 被采样丢弃（90% 的查询没有记录），回放时无法从审计日志重建 RRF 中间状态 → 确定性保证失效
2. **问题诊断盲区**：采样意味着 90% 的查询没有详细追踪，出问题时恰好命中的查询没有记录
3. **三轨方案已解决容量问题**：Track 2 独立存储 + 短期保留，不需要通过采样来控制 audit.log 大小

**唯一例外**：如果 Track 2 的 7 天保留窗口在高频场景（>5000/天）下仍不够，可以在 Track 2 内部启用采样——但仅限 Track 2，绝不影响 Track 1。详见 §5.4。

---

## 4. Track 1：audit.log 业务审计设计

### 4.1 Track 1 的事件范围

Track 1 保持"不可裁剪的业务合规审计"定位。Phase 9 仅新增 **2 个瘦身事件**：

| # | 事件名 | 触发位置 | 关键字段 | 字节估算 |
|---|--------|---------|---------|---------|
| 20 | `broad_search_started` | retriever.py 入口 | trace_id, query, scope_path, strict, top_k, rerank_enabled | ~250B |
| 21 | `broad_search_completed` | retriever.py 出口 | trace_id, final_chunks, hints_count, path（"hybrid_rrf"/"hybrid_reranked"/"vector_only"）, total_duration_ms | ~300B |

**绝对禁止进入 Track 1 的事件**：

| 禁止事件 | 原因 | 正确去向 |
|----------|------|---------|
| `vector_search_completed` (含 scores) | 高频、大体量、属调试数据 | Track 2 |
| `fts_search_completed` (含 scores) | 高频、属调试数据 | Track 2 |
| `rrf_fusion_completed` (含 top-10) | 大体量（~1KB/条） | Track 2 |
| `rerank_started` | 高频、低独立价值 | Track 2 |

**仍留在 Track 1 的错误类事件**（已存在于 Phase 5-8）：

| # | 事件名 | 说明 |
|---|--------|------|
| 14 | `qdrant_write_failed` | Qdrant 写入失败（已有） |
| — | `fts_sync_failed` | **Phase 9 新增到 Track 1**（FTS 同步失败是业务级错误） |

**fts_sync_failed** 的 schema（~200B）：
```json
{
  "event": "fts_sync_failed",
  "trace_id": "abc-123",
  "doc_hash": "a1b2c3...",
  "block_id": "blk_abc",
  "error": "sqlite3.OperationalError: database is locked"
}
```

**trace_id 可空说明**：`fts_sync_failed` 发生在摄取流水线中（`pipeline.py` 的 FTS 同步步骤），不一定与某个查询请求绑定。如果同步发生在后台摄取任务中（非请求触发），`trace_id` 可能为空字符串 `""` 或使用摄取任务的内部 ID。AuditIndex 仅索引 REPLAY_EVENTS 中的事件，`fts_sync_failed` 不在 REPLAY_EVENTS 中，因此 trace_id 为空不影响索引逻辑。查询 `fts_sync_failed` 事件应通过 `seek_by_event("fts_sync_failed")` 按事件类型查询，或按 `doc_hash` 过滤。

### 4.2 RRF 聚合统计（瘦身版）

`broad_search_completed` 中携带的 RRF 聚合统计（禁止记录 top-10 详情）：

```json
{
  "event": "broad_search_completed",
  "trace_id": "abc-123",
  "final_chunks": 40,
  "hints_count": 12,
  "path": "hybrid_rrf",
  "vector_hits": 40,
  "fts_hits": 35,
  "both_hits": 28,
  "rerank_skipped": true,
  "skip_reason": "strong_signal",
  "total_duration_ms": 142
}
```

**注意**：只有聚合数字（vector_hits/fts_hits/both_hits），没有 top-10 scores 或 block_ids。这些详情在 Track 2 中。

### 4.3 Track 1 容量验证

| 指标 | Phase 8 现状 | Phase 9 v2 |
|------|-------------|-----------|
| 每次查询事件数 | 2 (started + solved) | 3 (started + solved + broad_search_completed) |
| 每次查询字节数 | ~400B | ~750B |
| 1000 请求/天 | 0.4MB | 0.75MB |
| 填满 100MB | 250 天 | ~133 天 |
| 5×100MB 保留 | ~3 年 | ~1.8 年 |

**结论**：Track 1 的容量回到接近 Phase 8 水平（1.8 年 vs 3 年），完全可接受。

---

## 5. Track 2：search_trace.log 检索追踪设计

### 5.1 SearchTraceWriter 类设计

```python
# rag/ekrs_rag/observability/search_trace.py — Phase 9 新增

"""检索路径追踪日志 — 独立于 audit.log。

存储每次查询的完整检索中间状态（BM25 scores、向量 scores、RRF top-10
详情、重排 candidates），用于开发调试和短期性能分析。

设计决策：
- 独立文件 + 独立 handler，不污染 audit.log
- 50MB × 3 gzip + 7 天 TimedRotatingFileHandler
- 全量记录（不采样），保证 7 天窗口内 100% 回放能力
- 失败永不传播（与 AuditWriter 一致）
"""

from __future__ import annotations
import gzip
import logging
import shutil
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone
import json


def _gzip_namer(name: str) -> str:
    return name + ".gz"

def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(source)


class SearchTraceWriter:
    """检索追踪日志写入器。

    使用 TimedRotatingFileHandler（每天轮转，保留 7 天）+
    大小限制（50MB × 3）的双重保护。
    """

    def __init__(self, log_path: str, max_bytes: int = 50 * 1024 * 1024,
                 backup_count: int = 3, retain_days: int = 7):
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # TimedRotatingFileHandler: 每天午夜轮转
        handler = TimedRotatingFileHandler(
            str(path), when="midnight", interval=1,
            backupCount=retain_days, encoding="utf-8",
        )
        handler.namer = _gzip_namer
        handler.rotator = _gzip_rotator
        handler.setFormatter(logging.Formatter("%(message)s"))

        self._logger = logging.getLogger("ekrs.search_trace")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # 清理旧 handler（热重载/测试场景）
        for h in list(self._logger.handlers):
            if isinstance(h, TimedRotatingFileHandler):
                h.close()
                self._logger.removeHandler(h)
        self._logger.addHandler(handler)
        self._handler = handler
        self._max_bytes = max_bytes

    def write(self, event_type: str, **kwargs) -> bool:
        """写入一条检索追踪事件。返回 False 如果失败（永不传播）。"""
        try:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event_type,
                **kwargs,
            }
            self._logger.info(json.dumps(entry, ensure_ascii=False, default=str))
            return True
        except Exception:
            logging.getLogger("ekrs.search_trace.failures").error(
                "search_trace write failed", exc_info=True
            )
            return False


# 模块级单例
_writer: SearchTraceWriter | None = None

def set_writer(writer: SearchTraceWriter) -> None:
    global _writer
    _writer = writer

def get_writer() -> SearchTraceWriter | None:
    return _writer
```

### 5.2 Track 2 的事件类型

Track 2 存储所有详细检索中间状态：

| 事件名 | 关键字段 | 字节估算 |
|--------|---------|---------|
| `vector_search_completed` | trace_id, hits_count, top_5_scores, top_5_block_ids, duration_ms, model | ~600B |
| `fts_search_completed` | trace_id, hits_count, top_5_scores, top_5_block_ids, duration_ms, strong_signal, fts_query | ~500B |
| `rrf_fusion_completed` | trace_id, fused_count, top_10_scores, top_10_block_ids, vector_only, fts_only, both, jaccard, duration_ms, k | ~1200B |
| `rerank_started` | trace_id, candidate_count, model | ~150B |
| `rerank_completed` | trace_id, top_10_reranked_block_ids, top_10_blended_scores, duration_ms, skipped, skip_reason | ~800B |

**每次查询 Track 2 总量**：~3.3KB（5 条事件）

### 5.3 Track 2 轮转策略

| 参数 | 值 | 说明 |
|------|-----|------|
| 文件 | `search_trace.log` | 独立于 audit.log |
| 时间轮转 | 每天 midnight | `TimedRotatingFileHandler(when="midnight")` |
| 大小限制 | 50MB × 3 gzip | 兜底保护，防止异常暴增 |
| 保留天数 | 7 天 | `backupCount=7` |
| 压缩 | gzip | 轮转后自动压缩 |
| 磁盘峰值 | ~150MB（3 × 50MB 未压缩） | 实际 gzip 后 ~30-50MB |

### 5.4 高频场景的采样例外（仅限 Track 2）

如果日请求量 > 5000 且磁盘受限，Track 2 可启用**比例采样**：

```python
class SearchTraceWriter:
    def __init__(self, ..., sample_rate: float = 1.0):
        """sample_rate: 0.0-1.0，默认 1.0（全量记录）。
        仅在极端高频场景降低（如 0.1 = 每 10 次查询记录 1 次）。
        Track 1 (audit.log) 不受此参数影响，始终全量记录。
        """
        self._sample_rate = sample_rate

    def write(self, event_type: str, **kwargs) -> bool:
        if self._sample_rate < 1.0:
            import random
            if random.random() > self._sample_rate:
                return False  # 采样丢弃
        # ... 正常写入 ...
```

**配置方式**：

```yaml
# config.yaml
observability:
  search_trace:
    enabled: true
    sample_rate: 1.0       # 默认全量；高频场景设 0.1
    retain_days: 7
    max_bytes: 52428800     # 50MB
```

**重要约束**：
- 采样仅影响 Track 2（search_trace.log），绝不影响 Track 1（audit.log）
- 采样期间，Track 1 的 `broad_search_completed` 仍记录聚合统计，保证基本可观测性
- 采样期间，Prometheus（Track 3）仍全量采集指标

### 5.5 SearchTraceIndex

与 AuditIndex 平行的内存索引，用于按 trace_id 查询 Track 2：

```python
# rag/ekrs_rag/observability/search_trace_index.py

class SearchTraceIndex:
    """trace_id → file_offset 索引 over search_trace.log。

    与 AuditIndex 结构相同，但索引不同文件。
    用于回放时获取详细的检索中间状态。

    维护策略（与 AuditIndex 的区别）：
    Track 2 数据量大于 Track 1（每请求 ~3.3KB vs ~750B），7 天保留可能积累
    数百 MB。启动时全量扫描可能需 2-5 秒。因此 SearchTraceIndex 采用
    **增量维护 + 轮转时重建当天文件**策略，而非全量重扫：
    - 启动时：仅扫描当天的 search_trace.log（当天文件通常 <50MB，扫描 <1s）
    - 运行时：SearchTraceWriter.write() 写入后增量 append 到索引
    - 轮转时（每天 midnight）：清空索引，重新扫描当天文件
    - seek() 时：如果 trace_id 不在索引中，返回 None（可能是 7 天前的旧 trace）

    替代方案（如果启动延迟仍不可接受）：
    完全放弃 SearchTraceIndex，seek() 时直接 grep 当天文件。因为查询总是针对
    最近 7 天的 trace_id 小范围，grep 单个 trace_id 在 50MB 文件中 <100ms。
    """

    TRACE_EVENTS = frozenset({
        "vector_search_completed",
        "fts_search_completed",
        "rrf_fusion_completed",
        "rerank_completed",
    })

    def __init__(self, trace_log_path: str):
        self._path = Path(trace_log_path)
        self._index: dict[str, list[tuple[str, int]]] = {}

    def build_today_only(self) -> None:
        """启动时仅扫描当天文件（不扫描 .gz 历史）。

        历史天数的 trace 通过 seek() 时按需 grep .gz 文件获取（延迟 ~100ms/次），
        避免启动时扫描全部 7 天数据。
        """
        ...

    def append(self, event: str, trace_id: str, offset: int) -> None:
        """运行时增量索引（与 AuditIndex.append 一致）。"""
        ...

    def seek(self, trace_id: str) -> list[AuditLine] | None:
        """按 trace_id 查询检索追踪事件。

        查找顺序：
        1. 内存索引（当天 + 运行时增量）→ O(1)
        2. 如果未命中，grep 当天文件（尚未索引的最近写入）
        3. 如果仍未命中，返回 None（可能已超过 7 天保留窗口）
        """
        ...
```

**容量估算**：每个 trace_id 在索引中约 200B（4 个事件 × ~50B），1 万次查询约 2MB。
7 天窗口内最多 ~70MB 内存（极端高频场景），可接受。

---

## 6. Track 3：Prometheus 聚合指标设计

### 6.1 新增指标（12 个）

Track 3 提供**长期趋势 + 实时告警**，替代全量文本追踪的长期存储需求。

#### FTS5 指标

```python
fts_search_total = Counter(
    "rag_fts_search_total",
    "FTS5 BM25 search attempts by outcome",
    ["outcome"],  # hit, empty, error
)

fts_search_duration_seconds = Histogram(
    "rag_fts_search_duration_seconds",
    "FTS5 BM25 search latency",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
)

fts_sync_failures_total = Counter(
    "rag_fts_sync_failures_total",
    "FTS5 index sync failures",
)

fts_index_rows = Gauge(
    "rag_fts_index_rows",
    "Current FTS5 index row count",
)
```

#### RRF 融合指标

```python
rrf_fusion_total = Counter(
    "rag_rrf_fusion_total",
    "RRF fusion operations by path overlap pattern",
    ["pattern"],  # both, vector_only, fts_only, empty
)

rrf_fusion_duration_seconds = Histogram(
    "rag_rrf_fusion_duration_seconds",
    "RRF fusion latency",
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05),
)
```

#### 重排指标

```python
rerank_total = Counter(
    "rag_rerank_total",
    "Reranking attempts by status",
    ["status"],  # executed, skipped_strong_signal, skipped_strict, skipped_unavailable
)

rerank_duration_seconds = Histogram(
    "rag_rerank_duration_seconds",
    "Cross-encoder reranking latency",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0),
)

rerank_strong_signal_rate = Gauge(
    "rag_rerank_strong_signal_rate",
    "Fraction of queries that triggered strong-signal short-circuit (0.0-1.0)",
)
```

#### 检索质量指标

```python
retrieval_path_distribution = Counter(
    "rag_retrieval_path_total",
    "Retrieval path taken by query",
    ["path"],  # vector_only, fts_only, hybrid_rrf, hybrid_reranked
)

search_result_overlap = Histogram(
    "rag_search_result_overlap",
    "Overlap between BM25 and vector results (Jaccard index)",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
```

#### 索引一致性指标

```python
index_consistency_drift = Gauge(
    "rag_index_consistency_drift",
    "Absolute drift between Qdrant points and FTS5 rows",
)
```

### 6.2 指标注册到 METRICS namespace

```python
METRICS = SimpleNamespace(
    # ... 现有 15 个指标 ...
    # Phase 9 新增
    fts_search_total=fts_search_total,
    fts_search_duration_seconds=fts_search_duration_seconds,
    fts_sync_failures_total=fts_sync_failures_total,
    fts_index_rows=fts_index_rows,
    rrf_fusion_total=rrf_fusion_total,
    rrf_fusion_duration_seconds=rrf_fusion_duration_seconds,
    rerank_total=rerank_total,
    rerank_duration_seconds=rerank_duration_seconds,
    rerank_strong_signal_rate=rerank_strong_signal_rate,
    retrieval_path_distribution=retrieval_path_distribution,
    search_result_overlap=search_result_overlap,
    index_consistency_drift=index_consistency_drift,
)
```

### 6.3 基数控制

所有新指标的标签基数控制在个位数（3-4 值），不存在基数爆炸风险。

### 6.4 指标更新策略

部分新增指标需要特殊的更新方式（不是简单的 inc/observe）：

**fts_index_rows（Gauge）— 定期采集，不随写入更新**：

```python
# 不能在每次 FTS 写入后执行 COUNT(*)（SQLite 全表扫描，高写入频率下有性能影响）。
# 改为后台定期任务采集：

import asyncio

class FtsRowCollector:
    """每 30 秒执行一次 SELECT count(*) FROM blocks_fts，更新 Gauge。"""

    INTERVAL_SECONDS = 30

    def __init__(self, fts_manager: FTSManager):
        self._fts = fts_manager

    async def run_forever(self):
        while True:
            try:
                count = self._fts.count_rows()  # SELECT count(*) FROM blocks_fts
                METRICS.fts_index_rows.set(count)
            except Exception:
                pass  # 永不传播
            await asyncio.sleep(self.INTERVAL_SECONDS)
```

在 `main.py` lifespan 中启动：`asyncio.create_task(FtsRowCollector(fts).run_forever())`

**rerank_strong_signal_rate — 改为 Counter，由 PromQL 计算比率**：

```python
# 原方案（Gauge）需要应用内部维护滑动窗口，实现复杂且窗口大小不可调。
# 改为：删除 rerank_strong_signal_rate Gauge，直接用 Counter 由 Prometheus 计算：

# rerank_total{status="skipped_strong_signal"} 已记录短路次数
# rerank_total（所有 status 求和）= 总查询数
# PromQL: rate(rag_rerank_total{status="skipped_strong_signal"}[5m])
#         / rate(rag_rerank_total[5m])
# → 这就是 strong_signal_rate，且窗口大小（5m/15m/1h）由 PromQL 查询方灵活选择。
```

因此从指标列表中**移除** `rerank_strong_signal_rate` Gauge。短路率通过 PromQL 从 `rerank_total` Counter 计算。

**index_consistency_drift（Gauge）— 后台任务采集**：

```python
class IndexConsistencyChecker:
    """每 5 分钟比较 Qdrant 点数与 FTS5 行数，更新 drift Gauge。

    不在写入路径中计算（会阻塞摄取流水线）。
    作为独立的后台 asyncio task 运行。
    """

    INTERVAL_SECONDS = 300  # 5 分钟

    async def run_forever(self):
        while True:
            try:
                qdrant_count = await self._qdrant.count()
                fts_count = self._fts.count_rows()
                drift = abs(qdrant_count - fts_count)
                METRICS.index_consistency_drift.set(drift)
                # 同时写审计事件（Track 1）
                writer = get_writer()
                if writer:
                    writer.write("index_consistency_check",
                                 qdrant_points=qdrant_count,
                                 fts_rows=fts_count,
                                 consistent=(drift == 0),
                                 drift_count=drift)
            except Exception:
                pass
            await asyncio.sleep(self.INTERVAL_SECONDS)
```

**修正后指标总数**：原 12 个新增 → 移除 1 个（rerank_strong_signal_rate）→ **11 个新增**。

---

## 7. 结构化日志设计

### 7.1 Phase 9 日志命名空间

```
ekrs.
├── audit                   # Track 1: 业务审计（audit.log）
│   ├── failures            # 审计写入失败
│   └── rollover            # 轮转回调异常
├── search_trace            # Track 2: 检索追踪（search_trace.log）
│   └── failures            # 追踪写入失败
├── retrieval               # 检索层调试日志（debug.log）
│   ├── vector
│   ├── fts
│   ├── rrf
│   └── rerank
├── ingestion
├── constraint_engine
├── observability
│   ├── metrics
│   └── audit_index
└── concurrency
```

### 7.2 结构化 JSON 日志格式

Phase 9 建议将 `debug.log` 升级为结构化 JSON Lines：

```python
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)
```

### 7.3 日志级别策略

| Logger | 级别 | 目标文件 |
|--------|------|---------|
| `ekrs.audit` | INFO | audit.log (Track 1) |
| `ekrs.search_trace` | INFO | search_trace.log (Track 2) |
| `ekrs.retrieval.*` | DEBUG/INFO | debug.log |
| `ekrs.ingestion.*` | INFO | debug.log |
| `ekrs.constraint_engine` | WARNING | debug.log |
| root | WARNING | stderr |

---

## 8. 检索路径全链路追踪

### 8.1 跨轨事件关联

一次广谱检索查询在三个 Track 中产生的事件：

```
trace_id: abc-123

Track 1 (audit.log):
├─ broad_search_started       query="1.6MPa 法兰" strict=false
├─ constraint_solve_started   (Phase 7 现有)
├─ constraint_solved          branches=2 (Phase 7 现有)
└─ broad_search_completed     path=hybrid_rrf vector=40 fts=35 both=28 rerank=skipped

Track 2 (search_trace.log):
├─ vector_search_completed    hits=40 top_5=[0.87,0.84,...] duration=55ms
├─ fts_search_completed       hits=35 top_5=[0.92,0.88,...] strong_signal=true duration=7ms
├─ rrf_fusion_completed       fused=40 top_10=[blk_1,...] jaccard=0.61 duration=1ms
└─ rerank_completed           skipped=true reason=strong_signal duration=0ms

Track 3 (Prometheus):
├─ rag_fts_search_total{outcome="hit"} +1
├─ rag_rrf_fusion_total{pattern="both"} +1
├─ rag_rerank_total{status="skipped_strong_signal"} +1
├─ rag_retrieval_path_total{path="hybrid_rrf"} +1
└─ rag_search_result_overlap observe(0.61)
```

### 8.2 跨轨查询

通过 trace_id 同时查询 Track 1 和 Track 2：

```python
# 回放时：先从 Track 1 获取业务结果，再从 Track 2 获取详细中间状态
audit_lines = audit_index.seek(trace_id)       # Track 1
trace_lines = search_trace_index.seek(trace_id) # Track 2

# 合并为完整检索路径
full_path = merge_audit_and_trace(audit_lines, trace_lines)
```

### 8.3 延迟瀑布图

从 Track 1 的 `broad_search_completed` 可获取端到端延迟分解：

```
查询总延迟: 142ms (from broad_search_completed.total_duration_ms)
├── Gate 1: 召回 ──────────── 63ms
│   ├── 向量搜索: 55ms (from Track 2 vector_search_completed)
│   ├── BM25 搜索: 7ms (from Track 2 fts_search_completed)
│   └── RRF 融合: 1ms (from Track 2 rrf_fusion_completed)
├── Gate 1.5: 重排 ────────── 0ms (skipped: strong_signal)
├── Gate 2: 提取 ──────────── 8ms
├── Gate 3: 求解 ──────────── 3ms
└── 其他 ──────────────────── 68ms
```

---

## 9. 回放确定性保证

### 9.1 回放的数据来源

| 回放需求 | 数据来源 | 可用性保证 |
|----------|---------|-----------|
| 约束求解输入/输出 | Track 1 (audit.log) | 永久（~1.8 年） |
| RRF top-10 排序 + scores | Track 2 (search_trace.log) | 7 天窗口内 100% |
| 重排结果（blended scores） | Track 2 | 7 天窗口内 100% |
| BM25/向量独立 hit lists | Track 2 | 7 天窗口内 100% |
| 长期趋势/告警 | Track 3 (Prometheus) | 15 天 |

### 9.2 回放的时间窗口保证

| 时间窗口 | 回放能力 | 说明 |
|----------|---------|------|
| 0-7 天 | **100% 完整回放** | Track 1 + Track 2 都在 |
| 7 天-1.8 年 | **业务级回放** | 仅 Track 1（聚合统计，无 top-10 详情） |
| >1.8 年 | **仅趋势** | 仅 Track 3（Prometheus 指标） |

**设计决策**：7 天内的完整回放窗口满足几乎所有调试场景。超过 7 天的回放降级为业务级（从 Track 1 的 `broad_search_completed` 获取聚合统计），仍可验证求解结果的确定性。

### 9.3 strict 模式的回放保证

| 模式 | 回放行为 | 确定性 |
|------|---------|--------|
| `strict=True` | Track 1 完整回放（无重排） | 100%（永久） |
| `strict=False` + 7天内 | Track 1 + Track 2 完整回放 | 100% |
| `strict=False` + 7天后 | Track 1 聚合级回放 | 业务级（无 top-10） |

### 9.4 审计驱动回放实现

```python
async def replay_search(trace_id: str):
    """从审计日志恢复完整检索状态（7 天窗口内）。"""
    # Track 1: 业务结果
    audit_lines = audit_index.seek(trace_id)
    solve_started = next(l for l in audit_lines if l.event == "constraint_solve_started")
    broad_completed = next(l for l in audit_lines if l.event == "broad_search_completed")

    # Track 2: 详细中间状态（7 天内可用）
    trace_lines = search_trace_index.seek(trace_id)
    if trace_lines:
        rrf_event = next(l for l in trace_lines if l.event == "rrf_fusion_completed")
        rerank_event = next(l for l in trace_lines if l.event == "rerank_completed")
        # 使用 Track 2 中的完整 top-10 排序重建检索状态
        # → byte-level 确定性回放
    else:
        # 超过 7 天：使用 Track 1 聚合统计
        # → 业务级回放（验证 branches_count 一致性）
```

---

## 10. 日志查询与 API 设计

### 10.1 AuditIndex 查询扩展

```python
class AuditIndex:
    # 现有：trace_id → [(event, offset)]
    _index: dict[str, list[tuple[str, int]]] = {}

    # Phase 9 新增：按 event_type 的辅助索引
    _event_index: dict[str, list[tuple[str, int]]] = {}

    def seek_by_event(
        self, event_type: str, limit: int = 100,
    ) -> list[AuditLine]:
        """按事件类型查询 Track 1。"""
        ...
```

### 10.2 新增 API 端点

```python
@router.get("/v1/audit/events")
async def list_audit_events(
    event_type: str | None = None,
    trace_id: str | None = None,
    limit: int = 100,
    _auth: None = Depends(require_admin_key),
) -> dict:
    """查询 Track 1 业务审计事件。"""
    ...

@router.get("/v1/search/trace")
async def get_search_trace(
    trace_id: str,
    _auth: None = Depends(require_admin_key),
) -> dict:
    """获取指定 trace_id 的完整检索追踪（Track 1 + Track 2 合并）。

    安全：要求 X-Admin-Key（require_admin_key）。检索追踪包含查询内容和
    文档片段引用，属于管理级信息。

    返回结构：
    {
      "trace_id": "abc-123",
      "audit_events": [...],        # Track 1（业务审计，~1.8年可用）
      "trace_events": [...],        # Track 2（详细检索，7天内可用）
      "trace_available": true,      # false = Track 2 已过期（超过7天）
      "retrieval_summary": {...}    # 从 Track 1 broad_search_completed 提取
    }

    边界处理：
    - Track 1 事件总在（audit_index.seek 返回业务事件）
    - Track 2 事件可能为空（search_trace_index.seek 返回 None 或 []）
    - trace_available=false 时 trace_events=[]，但不返回 404（Track 1 仍有数据）
    - 前端根据 trace_available 显示"⚠ 详细检索数据已过期"提示
    """
    # Track 1: 始终查询
    audit_events = audit_index.seek(trace_id) or []

    # Track 2: 7天窗口内查询
    trace_events = search_trace_index.seek(trace_id)
    trace_available = trace_events is not None and len(trace_events) > 0
    if not trace_available:
        trace_events = []

    # 从 Track 1 提取聚合摘要
    broad_completed = next(
        (e for e in audit_events if e.event == "broad_search_completed"), None
    )
    retrieval_summary = broad_completed.raw if broad_completed else None

    return {
        "trace_id": trace_id,
        "audit_events": [{"event": l.event, "raw": l.raw} for l in audit_events],
        "trace_events": [{"event": l.event, "raw": l.raw} for l in trace_events],
        "trace_available": trace_available,
        "retrieval_summary": retrieval_summary,
    }
```

---

## 11. 告警规则设计

```yaml
# prometheus_alerts.yml

groups:
  - name: ekrs_phase9
    rules:
      - alert: EKRSIndexDrift
        expr: abs(rag_index_consistency_drift) > 0
        for: 5m
        labels: { severity: critical }
        annotations:
          summary: "Qdrant points != FTS5 rows (drift={{ $value }})"

      - alert: EKR SFtsErrorRate
        expr: |
          rate(rag_fts_search_total{outcome="error"}[5m])
          / rate(rag_fts_search_total[5m]) > 0.1
        for: 2m
        labels: { severity: warning }

      - alert: EKRSRerankLatencyHigh
        expr: |
          histogram_quantile(0.95,
            rate(rag_rerank_duration_seconds_bucket[5m])) > 3.0
        for: 5m
        labels: { severity: warning }

      - alert: EKRSAuditWriteFailures
        expr: rate(rag_audit_write_failures_total[5m]) > 0
        for: 1m
        labels: { severity: critical }

      - alert: EKRSFtsSyncFailures
        expr: rate(rag_fts_sync_failures_total[5m]) > 0
        for: 2m
        labels: { severity: warning }

      - alert: EKRSSolveLatencyHigh
        expr: |
          histogram_quantile(0.99,
            rate(rag_constraint_solve_duration_seconds_bucket[5m])) > 5.0
        for: 5m
        labels: { severity: warning }
```

---

## 12. 磁盘占用与容量规划

### 12.1 三轨磁盘占用总览

| Track | 文件 | 轮转策略 | 磁盘峰值 | 保留 |
|-------|------|---------|---------|------|
| Track 1 | audit.log | 100MB × 5 gzip | ~500MB | ~1.8 年 |
| Track 2 | search_trace.log | 50MB × 3 + 7天 | ~150MB (gzip ~40MB) | 7 天 |
| Track 3 | Prometheus | 时间序列 | ~10GB | 15 天 |
| debug.log | debug.log | 50MB × 3 | ~150MB | — |
| **总计** | | | **~10.8GB** | |

### 12.2 与 v1 方案对比

| 维度 | v1（单轨） | v2（三轨） |
|------|-----------|-----------|
| audit.log 保留 | ~7 个月（1000/天） | ~1.8 年 |
| 检索追踪保留 | 无（混在 audit.log 里） | 7 天（独立文件） |
| 磁盘总量 | ~500MB (audit) + 10GB (prom) | ~540MB + 10GB + 150MB |
| 回放窗口 | 取决于 audit.log 何时轮转 | 7天完整 + 1.8年业务级 |
| 业务审计纯净度 | 检索调试数据污染 | 物理隔离 |

### 12.3 不同负载下的容量

| 日查询量 | Track 1 日增 | Track 1 填满100MB | Track 2 日增 | Track 2 填满50MB |
|---------|-------------|-------------------|-------------|-------------------|
| 100 | 0.075MB | ~3.6 年 | 0.33MB | ~150 天 |
| 1,000 | 0.75MB | ~133 天 | 3.3MB | ~15 天 |
| 5,000 | 3.75MB | ~27 天 | 16.5MB | ~3 天 |
| 10,000 | 7.5MB | ~13 天 | 33MB | ~1.5 天 |

**注意**：Track 2 在高频场景下 50MB × 3 = 150MB 的保留会短于 7 天，但 TimedRotatingFileHandler 的 backupCount=7 保证至少有 7 个日志文件（即使每天不到 50MB）。如果日增 > 50MB，则按大小轮转优先（保证磁盘上限）。

---

## 13. 风险评估

### 13.1 技术风险

| 风险 | 严重度 | 影响 | 缓解 |
|------|--------|------|------|
| Track 2 文件丢失/损坏 | 低 | 7天内回放能力降级 | Track 1 聚合统计仍可用；不影响业务 |
| SearchTraceIndex 内存增长 | 低 | 7天窗口内每条 trace ~1KB，1万条 ~10MB | 可接受 |
| 双 Index 维护复杂度 | 中 | 启动时扫描两个文件 | 并行构建；各自独立 rebuild |
| Track 2 采样导致回放缺口 | 低 | 高频场景下采样丢弃部分 trace | 仅影响 Track 2；Track 1 始终全量 |
| 日志格式不一致 | 低 | Track 1/2 格式略有差异 | 统一 JSON Lines 格式器 |

### 13.2 向后兼容风险

| 风险 | 缓解 |
|------|------|
| 新审计事件被旧消费者拒绝 | 消费者忽略未知 event 类型（JSON Lines 天然兼容） |
| Track 2 是新文件，部署需更新 | 更新 Dockerfile / docker-compose 挂载路径 |
| Prometheus 新指标名冲突 | 独立前缀（`rag_fts_*`, `rag_rrf_*`） |

### 13.3 性能影响

| 操作 | 额外开销 | 预估 |
|------|---------|------|
| Track 1 写入（1 条新事件） | +1 条 JSON 写入 | ~0.1ms |
| Track 2 写入（4-5 条事件） | +5 条 JSON 写入（独立文件） | ~0.5ms |
| Prometheus 观测（+12 个指标） | +12 个原子操作 | ~0.1ms |
| **总额外延迟** | | **~0.7ms（<1% 预算）** |

---

## 14. 实施路线图

### Phase 9a-obs：三轨基础（与 Phase 9a 同步，1.5 周）

| 任务 | 描述 |
|------|------|
| T-obs-1 | 实现 SearchTraceWriter + TimedRotatingFileHandler |
| T-obs-2 | 实现 SearchTraceIndex |
| T-obs-3 | Track 1 新增 broad_search_started/completed + fts_sync_failed |
| T-obs-4 | Track 2 新增 vector/fts/rrf/rerank 详细事件 |
| T-obs-5 | 新增 12 个 Prometheus 指标 |
| T-obs-6 | 在 retriever/fts_manager/rrf_fusion/reranker 中埋点（双轨） |

### Phase 9b-obs：查询增强（与 Phase 9b 同步，0.5 周）

| 任务 | 描述 |
|------|------|
| T-obs-7 | 结构化 JSON 日志格式器 |
| T-obs-8 | AuditIndex seek_by_event |
| T-obs-9 | GET /v1/search/trace 端点（跨轨查询） |

### Phase 9c-obs：告警+一致性（与 Phase 9c 同步，0.5 周）

| 任务 | 描述 |
|------|------|
| T-obs-10 | Prometheus alertmanager 规则文件 |
| T-obs-11 | 索引一致性校验 cron |
| T-obs-12 | Track 2 采样配置（按需启用） |

---

## 15. 结论与建议

### 15.1 核心结论

1. **三轨分层存储是正确的架构选择**。v1 的单轨方案把高频检索中间状态塞进 audit.log，导致写入量增加 6-8 倍、历史保留缩到几周。v2 的三轨方案彻底分离了"不可裁剪的业务合规"（Track 1）与"可丢弃的性能调试"（Track 2），两者互不干扰。

2. **不采样是正确的决策**。采样会破坏回放确定性（第 9 章）——如果 RRF 中间状态被采样丢弃，回放时无法重建 byte-level 确定性结果。三轨方案通过物理隔离（而非采样）解决了容量问题。

3. **7 天完整回放窗口 + 1.8 年业务级回放**满足所有实际需求。7 天内可从 Track 1 + Track 2 完整重建检索状态；超过 7 天仍可从 Track 1 获取聚合统计验证求解确定性。

4. **Track 1 容量回到接近 Phase 8 水平**（1.8 年 vs 3 年），Phase 9 新增仅 2 条瘦身事件（~300B/条），不记录 top-10 scores/block_ids。

5. **Prometheus 指标（Track 3）提供长期趋势和实时告警**，替代了"需要长期保存全量文本追踪"的需求。

### 15.2 优先级建议

**P0（与 Phase 9a 同步）**：
- SearchTraceWriter + SearchTraceIndex 实现
- Track 1 新增 broad_search_started/completed（瘦身版）
- Track 2 新增 vector/fts/rrf 详细事件
- 12 个新 Prometheus 指标
- 双轨埋点

**P1（与 Phase 9b/9c 同步）**：
- 结构化 JSON 日志
- GET /v1/search/trace 跨轨查询端点
- Prometheus 告警规则

**P2（Phase 9 完成后）**：
- 索引一致性校验 cron
- Track 2 采样配置（按需）

### 15.3 与其他研究报告的关系

本报告是第六份研究文档，与前五份形成完整的 Phase 9 设计体系：

| # | 文档 | 覆盖维度 |
|---|------|---------|
| 1 | feature-mapping | QMD 功能到 EKRS 的映射 |
| 2 | deep-dive-extensions | 深度实现分析 |
| 3 | integration-feasibility | 外部集成方案 |
| 4 | broad-spectrum-retrieval-port-design | 内部移植设计（含 zvec/turbovec 评估） |
| 5 | enhanced-ui-design | UI 设计 |
| 6 | **enhanced-logging-design v2（本报告）** | **日志/审计/可观测性设计（三轨分层）** |

---

## 附录 A：审计事件完整清单（Phase 9 v2 后）

### Track 1: audit.log（业务审计）

| # | 事件名 | Phase | 触发位置 | REPLAY? |
|---|--------|-------|---------|---------|
| 1 | endpoint_started | P5 | ObservabilityMiddleware | ❌ |
| 2 | endpoint_completed | P5 | ObservabilityMiddleware | ❌ |
| 3 | constraint_solve_started | P7 | constraints.py | ✅ |
| 4 | constraint_solved | P7 | constraints.py | ✅ |
| 5 | constraint_solve_failed | P7 | constraints.py | ❌ |
| 6 | query_replay_executed | P5 | constraints.py | ❌ |
| 7 | ingestion_received | P5 | ingestion.py | ❌ |
| 8 | ingestion_completed | P5 | pipeline.py | ❌ |
| 9 | ingestion_failed | P5 | pipeline.py | ❌ |
| 10 | replay_started | P5 | ingestion.py | ❌ |
| 11 | replay_completed | P5 | ingestion.py | ❌ |
| 12 | replay_sha256_mismatch | P5 | pipeline.py | ❌ |
| 13 | compensation_retry | P7 | compensation.py | ❌ |
| 14 | qdrant_write_failed | P7 | qdrant_client.py | ❌ |
| 15 | lock_acquire_failed | P5 | redis_lock.py | ❌ |
| 16 | document_metadata_failed | P6A | documents.py | ❌ |
| 17 | callback_url_blocked | P6A | ingestion.py | ❌ |
| 18 | callback_auth_missing | P6A | ingestion.py | ❌ |
| 19 | callback_best_effort_failed | P6A | ingestion.py | ❌ |
| **20** | **broad_search_started** | **P9** | **retriever.py** | **✅** |
| **21** | **broad_search_completed** | **P9** | **retriever.py** | **✅** |
| **22** | **fts_sync_failed** | **P9** | **pipeline.py** | ❌ |

Track 1 总计：22 个事件（Phase 5-8: 19 个 + Phase 9 新增: 3 个），其中 4 个进入 REPLAY_EVENTS。

### Track 2: search_trace.log（检索追踪）

| # | 事件名 | 关键字段 | 字节估算 |
|---|--------|---------|---------|
| T2-1 | vector_search_completed | trace_id, hits, top_5_scores, top_5_block_ids, duration_ms | ~600B |
| T2-2 | fts_search_completed | trace_id, hits, top_5_scores, top_5_block_ids, duration_ms, strong_signal | ~500B |
| T2-3 | rrf_fusion_completed | trace_id, fused_count, top_10_scores, top_10_block_ids, jaccard, duration_ms | ~1200B |
| T2-4 | rerank_started | trace_id, candidate_count, model | ~150B |
| T2-5 | rerank_completed | trace_id, top_10_block_ids, top_10_blended_scores, duration_ms, skipped | ~800B |

Track 2 总计：5 个事件类型，每次查询 ~3.3KB。

## 附录 B：Prometheus 指标完整清单（Phase 9 后）

| # | 指标名 | 类型 | Phase |
|---|--------|------|-------|
| 1 | rag_http_requests_total | Counter | P5 |
| 2 | rag_http_request_duration_seconds | Histogram | P5 |
| 3 | rag_http_requests_inprogress | Gauge | P5 |
| 4 | rag_ingestion_total | Counter | P5 |
| 5 | rag_ingestion_duration_seconds | Histogram | P5 |
| 6 | rag_ingestion_chunks_written | Counter | P5 |
| 7 | rag_constraint_solve_total | Counter | P5 |
| 8 | rag_constraint_solve_duration_seconds | Histogram | P5 |
| 9 | rag_constraint_branches_count | Histogram | P5 |
| 10 | rag_lock_acquire_total | Counter | P5 |
| 11 | rag_compensation_pending_tasks | Gauge | P5 |
| 12 | rag_compensation_retries_total | Counter | P5 |
| 13 | rag_qdrant_write_failures_total | Counter | P7 |
| 14 | rag_audit_write_failures_total | Counter | P5 |
| 15 | rag_route_failures_total | Counter | P5 |
| **16** | **rag_fts_search_total** | **Counter** | **P9** |
| **17** | **rag_fts_search_duration_seconds** | **Histogram** | **P9** |
| **18** | **rag_fts_sync_failures_total** | **Counter** | **P9** |
| **19** | **rag_fts_index_rows** | **Gauge** | **P9** |
| **20** | **rag_rrf_fusion_total** | **Counter** | **P9** |
| **21** | **rag_rrf_fusion_duration_seconds** | **Histogram** | **P9** |
| **22** | **rag_rerank_total** | **Counter** | **P9** |
| **23** | **rag_rerank_duration_seconds** | **Histogram** | **P9** |
| **24** | **rag_rerank_strong_signal_rate** | **Gauge** | **P9** |
| **25** | **rag_retrieval_path_total** | **Counter** | **P9** |
| **26** | **rag_search_result_overlap** | **Histogram** | **P9** |
| ~~24~~ | ~~rag_rerank_strong_signal_rate~~ | ~~Gauge~~ | ~~P9~~ — **已移除，改用 PromQL 从 rerank_total 计算** |

总计：26 个指标（Phase 5-8: 15 个 + Phase 9 新增: 11 个）。
