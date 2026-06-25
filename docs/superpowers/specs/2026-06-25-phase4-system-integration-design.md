# Phase 4 系统集成 — 设计

日期: 2026-06-25
范围: 最小闭环 (回调幂等 + 分布式锁 + 启动轮询补偿)
后端: Redis 锁 + aiosqlite

## 目标
满足 ekrs-handbook §6 Phase 4 验收：并发安全 + 状态最终一致

## 架构
```
Parser ──notify──> RAG(/v1/ingestion/notify)
                     │
                     ├─[1] Redis 锁 (lock:ingest:{doc_id}, ttl=300s)
                     ├─[2] aiosqlite tasks 写 PENDING
                     ├─[3] 解析 → Qdrant 写入
                     ├─[4] tasks → COMPLETED, Lua 释放锁
                     └─[5] 回调 Parser

RAG 启动 ──> compensation 扫描器
              └─ tasks WHERE status IN (PENDING, FAILED) AND updated_at < now-5min
                 → 重新入队 (attempts < 3)
```

## 组件
1. `shared/ekrs_shared/idempotency.py` — request_id 工具
2. `rag/ekrs_rag/concurrency/redis_lock.py` — acquire/release + Lua 释放
3. `rag/ekrs_rag/concurrency/compensation.py` — scan_and_retry() 启动钩子
4. `rag/ekrs_rag/storage/task_repo.py` — aiosqlite tasks CRUD
5. 修改 `rag/ekrs_rag/api/routes/ingestion.py` — 入口加幂等 + 锁
6. 修改 `rag/ekrs_rag/main.py` — startup 注册 compensation

## 数据模型
```sql
CREATE TABLE tasks (
  request_id TEXT PRIMARY KEY,  -- 幂等键
  doc_id TEXT NOT NULL,
  status TEXT NOT NULL,         -- PENDING|RUNNING|COMPLETED|FAILED
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX idx_tasks_status_updated ON tasks(status, updated_at);
```

## 错误处理
- Redis 不可达 → 503 fail-fast；已存在 PENDING 记录由补偿兜底
- Qdrant 写入失败 → 状态 FAILED, attempts++, 启动补偿重试
- 锁过期但 RUNNING → compensation 扫描器按 `attempts < 3` 限重试避免双写
- 重复 notify 同 request_id → UNIQUE 约束触发，返回 200 幂等响应

## 测试
- 单元 (≥6): RedisLock mock、idempotency hash、TaskRepo CRUD、Lua 释放 token 校验
- 集成 (≥4): 并发 notify 同 doc_id 只入库一次、重复 request_id 不重复入库、启动补偿重试、Redis 故障降级
- 目标: 覆盖率 ≥ 80%

## 不做 (out of scope)
- 锁看门狗续约 → Phase 5
- Redlock 多实例 → 单 Redis 够用
- 状态机一致性检查 → 启动扫描器 + aiosqlite UNIQUE 已覆盖

## 未决问题
1. Redis 连接是 lazy-init 还是 startup eager-init? (倾向 eager + 健康检查)
2. tasks 表清理策略: 永久保留还是 7 天滚动? (倾向永久, 容量小)
3. callback 失败重试是否也走 compensation 任务表? (倾向单独 callback_retry 表)
4. 锁 TTL 300s 是否覆盖大型文档? (需实测, 可能需按 doc_size 动态)
