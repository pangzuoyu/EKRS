# Phase 5 可观测性 — 设计

日期: 2026-07-12
范围: Prometheus 真实指标 + audit log 按 spec §12 落地 + Query/Ingestion 双 Replay
后端: prometheus-client + python-json-logger + aiosqlite schema 扩展

## 目标

满足 ekrs-handbook §6 Phase 5 验收：
- 指标可抓取 → `/metrics` 真实暴露 Prometheus 格式
- 可审计、可重现 → audit.log 永久落盘、Replay 端点可重放
- 全链路 trace 贯穿 → contextvars 注入 trace_id 到 audit/metrics/log

## 架构

```
HTTP 请求
   │
   ▼
ObservabilityMiddleware (FastAPI)
   │  注入 trace_id (header X-Trace-Id 或生成 uuid4)
   │  计时
   ▼
路由 (@audited / @metered 装饰器)
   │  audit("endpoint_started") + metrics.inprogress++
   ▼
业务逻辑 (solver / ingestion / Qdrant / Redis)
   │  显式 audit(...) / metric.inc/dec
   │  trace_id 来自 contextvars, 全链路可见
   ▼
响应
   │  audit("endpoint_completed", duration_ms)
   │  metrics.duration.observe / counter.inc{status}
```

## 组件与文件

### 新增

```
rag/ekrs_rag/observability/
  __init__.py
  metrics.py        # Counter/Histogram 注册表 + safe_inc
  audit.py          # AuditWriter (python-json-logger, FileHandler 永久)
  trace.py          # contextvars + middleware 注入

rag/ekrs_rag/api/middleware/observability.py    # FastAPI middleware
rag/ekrs_rag/api/decorators.py                  # @audited / @metered
```

### 修改

```
shared/ekrs_shared/audit.py          # 改用 python-json-logger + FileHandler (永久)
rag/ekrs_rag/api/routes/metrics.py   # 替换占位 → prometheus_client.generate_latest()
rag/ekrs_rag/api/routes/ingestion.py # 新增 POST /v1/ingestion/replay
rag/ekrs_rag/api/routes/constraints.py # solve 流程接 audit + replay=true 路径
rag/ekrs_rag/concurrency/compensation.py # 显式 audit("compensation_retry")
rag/ekrs_rag/core/logging.py         # 增加 RotatingFileHandler (debug.log, 100MB x 5)
rag/ekrs_rag/main.py                 # 注册 middleware + 启动 audit 健康检查
rag/ekrs_rag/storage/task_repo.py    # Phase 4.5 schema: source_path + payload_sha256
```

### 依赖

```toml
"prometheus-client>=0.20"   # 新增
# python-json-logger>=2.0 已有
```

## 数据流

### trace_id 注入

```
HTTP X-Trace-Id header
   ├─ 存在 → trace_ctx_var.set(value)
   └─ 缺失 → uuid4().hex 生成
整个请求生命周期内 audit writer / metric label-safe 包装 / logger.extra 都从 contextvar 读
请求结束 → contextvar.reset(token)
```

注: **trace_id 不作为 Prometheus label** (cardinality 爆炸)。label 限定为 endpoint/method/status/outcome/result/operation。

### Audit 事件清单

| event | 触发点 | 关键字段 |
|-------|--------|----------|
| `endpoint_started` | middleware | endpoint, method |
| `endpoint_completed` | middleware | endpoint, status_code, duration_ms |
| `constraint_solve_started` | constraints route | query, scope_path, strict |
| `constraint_solved` | solve 成功 | trace_id, query, branches_count, parameters, evidence_count, duration_ms |
| `constraint_solve_failed` | solve 异常 | trace_id, error_type, error_msg |
| `query_replay_executed` | constraints (replay=true) | replayed_trace_id, deterministic_match |
| `ingestion_received` | ingestion notify | request_id, doc_id, source_path |
| `ingestion_completed` | ingestion 成功 | request_id, doc_id, chunks_written, duration_ms |
| `ingestion_failed` | ingestion 失败 | request_id, doc_id, attempts, error_type |
| `ingestion_replay_started` | /v1/ingestion/replay | request_id, replayed_by |
| `ingestion_replay_completed` | replay 成功 | request_id, sha256_match, duration_ms |
| `ingestion_replay_sha256_mismatch` | hash 校验失败 | request_id, expected_sha256, actual_sha256 |
| `compensation_retry` | compensation scanner | request_id, attempts, error_msg |
| `qdrant_write_failed` | Qdrant 写异常 | collection, batch_size, error_type |
| `lock_acquire_failed` | RedisLock 返回 None | lock_key, ttl_sec |

## Prometheus 指标集 (12 个)

### HTTP (middleware 自动)

- `rag_http_requests_total{endpoint,method,status}` counter
- `rag_http_request_duration_seconds{endpoint,method}` histogram
- `rag_http_requests_inprogress{endpoint}` gauge

### Ingestion

- `rag_ingestion_total{status}` counter (completed|failed|duplicate|in_flight)
- `rag_ingestion_duration_seconds` histogram
- `rag_ingestion_chunks_written` counter

### Constraint Solve

- `rag_constraint_solve_total{outcome}` counter (solved|failed|replay_match|replay_mismatch)
- `rag_constraint_solve_duration_seconds` histogram
- `rag_constraint_branches_count` histogram

### Concurrency

- `rag_lock_acquire_total{result}` counter (acquired|failed|timeout)
- `rag_compensation_pending_tasks` gauge
- `rag_compensation_retries_total{result}` counter (requeued|exhausted)

### 依赖健康

- `rag_qdrant_write_failures_total{operation}` counter (upsert|delete)

默认 buckets: HTTP=(0.005..10s), Solve=(0.01..5s), Ingest=(0.1..300s)

预期总 series ≤ 80。

## Audit log 文件

### audit.log

- 路径: `$AUDIT_LOG_PATH` (env, 默认 `audit.log`)
- 永久, 永不轮转, 永不删除
- 一行一条 JSON, python-json-logger JsonFormatter 输出
- 不受 EKRS_DEBUG 控制, prod 永远开启
- 启动期路径不可写 → 进程退出非零 (硬约束)

### debug.log

- 路径: `$DEBUG_LOG_PATH` (env, 默认 `logs/debug.log`)
- 仅 EKRS_DEBUG=true 启用
- RotatingFileHandler: 100MB × 5 backups
- best-effort: 路径不可写仅 warn, 不影响启动

### audit 行 schema

```json
{
  "timestamp": "2026-07-12T20:15:30.123Z",
  "level": "INFO",
  "logger": "ekrs.audit",
  "event": "constraint_solved",
  "trace_id": "...",
  "...": "事件专属字段"
}
```

## Replay 实现

### A. Query Replay (POST /v1/constraints, replay=true)

请求 schema (replay=true 时):
```json
{
  "query": "高温下温度上限",
  "scope_path": ["national"],
  "strict": true,
  "replay": true,
  "replay_trace_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

```
1. 收到请求 {query, scope_path, strict, replay: true, replay_trace_id: <old>}
2. 按 replay_trace_id 反查 audit.log 中 constraint_solve_started + constraint_solved
   ├─ 不存在 / 不完整 → 400 {error: "no_prior_solve"}
3. 从 audit 反序列化上轮的 query/scope_path/strict 作为本次输入
   (请求体里的 query/scope_path/strict 字段被忽略, replay_trace_id 才是 source of truth)
4. 走正常 solve 路径 (新 trace_id, 由 middleware 生成)
5. 对比新结果 vs audit 旧结果 (branches_count + parameters)
   ├─ 完全一致 → deterministic_match=true
   └─ 不一致 → deterministic_match=false + diff 列表 [{path, old, new}, ...]
6. audit("query_replay_executed", replayed_trace_id, deterministic_match, ...)
7. metric rag_constraint_solve_total{outcome="replay_match"|"replay_mismatch"}.inc()
8. 响应: {trace_id, replayed_trace_id, branches, deterministic_match, diff}
```

### B. Ingestion Replay (POST /v1/ingestion/replay)

请求: `{request_id, replayed_by}`

```
1. TaskRepo.find(request_id)
   ├─ 不存在 → 404
   ├─ status IN (PENDING, RUNNING) → 409 {reason: "in_flight"}
   └─ status=COMPLETED ↓
2. source_path 为 NULL → 409 {reason: "pre_phase5"}
3. 读 JSONL 文件, 计算 sha256, 与 tasks.payload_sha256 对比
   ├─ 不一致 → 409 + audit("ingestion_replay_sha256_mismatch")
   └─ 一致 ↓
4. 走正常 ingestion 流程 (幂等 + Redis 锁 → Qdrant upsert, point ID 去重 = idempotent 覆盖)
5. audit("ingestion_replay_completed", ...)
6. 响应 200 {request_id, status: "completed", chunks_written, duration_ms}
```

### Phase 4.5 schema 迁移

```sql
ALTER TABLE tasks ADD COLUMN source_path TEXT;
ALTER TABLE tasks ADD COLUMN payload_sha256 TEXT;
```

迁移在 `TaskRepo.init()` 启动时跑 (PRAGMA table_info 检查列存在性)。
老 row 无 source_path → replay 返回 409 `{reason: "pre_phase5"}`。

Ingestion notify 入口同时写入 source_path + sha256 (parser 提供 source_path, 可选; 缺失时 warn 但不阻断)。

### 鉴权

两个 Replay 端点共用 `PARSER_TOKEN` (与 notify 一致)。

## 错误处理

### 启动期

| 失败 | 行为 |
|------|------|
| audit.log 路径不可写 | 进程退出非零 |
| Prometheus 注册冲突 | fail-fast 退出 |
| debug.log 不可写 (EKRS_DEBUG=true) | warn + 继续 |

### 运行期

| 失败 | 行为 | 阻断? |
|------|------|-------|
| audit 单条 write 失败 | debug.log + rag_audit_write_failures_total++ | 否 |
| metric inc 失败 | debug.log warn | 否 |
| trace_id contextvar 未设 | audit 用 "unknown" 填充 | 否 |
| audit JSON 行损坏 (replay 读) | 跳过 + warn + 继续找 | (可能最终 400) |
| Ingestion replay: JSONL 文件已被 parser 删 | 409 {reason: "file_missing"} + audit | 是 |

## 测试

### 单元 (新增 100% 覆盖 observability)

`rag/tests/unit/observability/test_metrics.py` (4 tests)
`rag/tests/unit/observability/test_audit.py` (6 tests)
`rag/tests/unit/observability/test_trace.py` (4 tests)
`rag/tests/unit/storage/test_task_repo_phase45.py` (4 tests)

### 集成 (6 tests)

`rag/tests/integration/test_metrics_endpoint.py` (3 tests)
`rag/tests/integration/test_query_replay.py` (3 tests)
`rag/tests/integration/test_ingestion_replay.py` (4 tests)

### 覆盖率目标

| 模块 | 目标 |
|------|------|
| rag/ekrs_rag/observability/* | 100% line |
| rag/ekrs_rag/api/middleware/observability.py | ≥ 95% |
| rag/ekrs_rag/api/decorators.py | 100% line |
| rag/ekrs_rag/storage/task_repo.py (Phase 4.5 列) | 保持 100% |

### Mock 模式

- audit writer 测试: `tmp_path` 写真实文件 → 读回行验证
- Prometheus 测试: `generate_latest()` → 正则匹配指标名
- trace 测试: FastAPI TestClient + `asyncio.gather` 验证 contextvar 隔离
- Replay 测试: monkeypatch 让求解器第二次返回不同结果 → 断言 deterministic_match=false

## 不做 (out of scope)

- CI gate (pytest + lint + coverage threshold 阻断) → Phase 5.5
- Lock watchdog 续约 → Phase 5.5
- 跨实例共享 DB (Postgres/MySQL) 做幂等 → Phase 6+
- Grafana dashboard JSON → 后续运维
- audit.log 自动归档到对象存储 → 后续
- Replay 批量/按时间范围 → YAGNI
- Audit 加密/签名 → 当前合规范围外

## 未决问题 (设计前确认)

1. **trace_id vs request_id**: 推荐并存 — request_id 是持久 idempotency key, trace_id 是单次 HTTP UUID, 各管各的。
2. **/metrics 鉴权**: 推荐内网不鉴权, 外网由 Ingress 限制。如需 token, 加 `METRICS_TOKEN` env。
3. **debug.log 默认路径**: 推荐从 `DEBUG_LOG_PATH` env 读, 默认 `logs/debug.log`。
4. **audit 保留**: 本期永久, 外部 logrotate 归档。
5. **Ingestion Replay 语义**: 选 A (幂等覆盖 — point ID 去重). 选 B (清后写) 需要额外 delete_by_doc_id 步骤 + 风险, 不做。
6. **Query Replay "上轮" 定义**: 按请求体中的 `replay_trace_id` 字段精确查 audit 中的 constraint_solve_started + constraint_solved, 不按 query 模糊匹配。请求体的 query/scope_path/strict 在 replay 模式下被忽略, replay_trace_id 是 source of truth。

## Multi-instance 部署说明

Prometheus Counter/Histogram 进程内, 不跨实例聚合 — 部署侧需在 Prometheus
server 端用 `sum by (...)` 聚合多 Pod 指标。audit.log 每 Pod 独立文件, 跨实例
对账不在本期范围。