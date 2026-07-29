# Phase 10 T10a-2 — 摄入流水线同步写 FTS 实施计划

## 父计划

[`2026-07-28-phase10-broad-spectrum-retrieval.md`](2026-07-28-phase10-broad-spectrum-retrieval.md) T10a-2 行 + §风险 FTS5/Qdrant 非原子 + §开放问题 1/5 已闭. 本计划不重复决议.

## 范围

将 `FTSManager` (T10a-1 已建) 接入 `IngestionPipeline.ingest()`:
1. **同步写**: Qdrant upsert 成功后, 按 `chunk_index` 顺序对每个 chunk 生成 `chunk_id = FTSManager.generate_chunk_id(doc_hash, idx)`, 调 `fts.upsert(chunk_id, chunk.source_block_ids[0], chunk, payload)`.
2. **失败回滚**: FTS upsert 异常 → 已成功的 Qdrant 点保留 (不为删除 Qdrant 点而失 idempotency), 但 FTS 部分行用 `delete_by_chunk_id` 撤回 (T10a-1 H2 单 chunk 回滚原语). 整体失败路径写 audit + 失败 outcome.
3. **对账后台任务**: 5 分钟间隔, 比对 FTS row count vs Qdrant point count; 不一致时 emit `index_consistency_drift` 审计事件 + 更新 Prometheus gauge `ekrs_index_consistency_drift_total`. **不自动修复** (避免误删).
4. **审计事件注册**: 父计划 §风险已锁定 `fts_consistency_drift`, 需在 `main.py:_EVENT_SCHEMAS` 注册 schema (`{"drift_count"}`).

**T10a-2 边界**: pipeline 同步写 + 对账任务 + `fts_consistency_drift` schema 注册. **不做**:
- `fts_synced` / `fts_searched` 写入 — T10a-7.
- retriever 端 RRF 接入 — T10a-3/4.
- chunk_id 生成时机 — T10a-5 (本任务**只**用 `FTSManager.generate_chunk_id(doc_hash, idx)`, 由 pipeline 在 chunk 索引生成时即时调).
- Qdrant payload 加 `chunk_id` 字段 — T10a-5 (本任务**只**写 FTS, 不动 Qdrant payload schema).

## 设计

### pipeline.ingest 同步写 (Step 5.6)

```python
# 现有 Step 5 (Qdrant upsert) 之后, Step 6 (callback) 之前插入 Step 5.6:
# Step 5.6: FTS sync write — paired with Qdrant
if self._fts is not None:
    try:
        # H4: use replace_doc (atomic delete+bulk-upsert) for idempotency.
        # FTS5 没有 PRIMARY KEY — 简单 upsert 重 ingest 会产生重复行.
        self._fts.replace_doc(doc_hash, chunks, version=version)
        if self._audit_writer is not None:
            # H1+M6: write() returns False if schema not registered.
            # Until T10a-7 registers `fts_synced`, this returns False silently.
            # After T10a-7, returns True and audit event is persisted.
            self._audit_writer.write(
                "fts_synced",
                doc_hash=doc_hash, version=version,
                chunks_written=len(chunks),
            )
    except Exception as fts_err:
        # 不阻断 outcome: Qdrant 已成功, FTS 漂移由对账任务处理
        logger.warning("fts_sync_failed_after_qdrant: doc=%s v=%d err=%s",
                       doc_hash, version, fts_err)
```

**关键决策**:
- **pipeline 注入 `fts: FTSManager | None = None` (kwarg)**: 镜像 `qdrant` 注入模式. `None` 走退化路径 (byte-level 等于 Phase 9 baseline). 默认值保证现有调用方 (main.py lifespan) 不破. **H3 修复**.
- **不抛**: FTS 失败 → log warning → 不阻断 callback. 理由: Qdrant 是 truth-of-record, FTS 是镜像; 镜像丢失只能"先标记再对账", 不能让成功 ingestion 变 failed.
- **`replace_doc` 而不是 per-chunk upsert loop**: FTS5 virtual table 无 PRIMARY KEY; 简单 upsert 在 re-ingest 时产生重复行, BM25 检索会返回同一 chunk 两次. `replace_doc` 在 Tb.2 实现 = `delete_by_doc` + bulk upsert, 给定 doc_hash 的旧行整体替换. **H4 修复**.
- **`chunk_id` 即时生成**: 不用 T10a-5 的 retriever 端生成 (那是查询时机), pipeline 写入时即生成. `chunk_index` 用 enumerate 顺序. `replace_doc` 内部 enumerate 调用 `FTSManager.generate_chunk_id(doc_hash, idx)`.
- **`source_block_ids[0]`**: FTS 表 schema 是 `block_id UNINDEXED` 单值. Chunk 可能有多个 source blocks, 取首块标识作为"代表" block_id. **M1 语义**: `get_chunk_id(block_id)` 只在 block_id 是 chunk 的首个 source block 时返回 chunk_id; 非首个 source block 返回 None. chunk↔block 完整 N:M 关系存 Qdrant payload (`source_block_ids` 数组). T10a-5 完整双向映射是后续工作.

### FTS5 vs Qdrant 顺序

```
Qdrant upsert (idempotent — point_id 来自 uuid5(doc_hash, version, source_block_ids))
   ↓ 成功
FTS upsert loop (per-chunk, generate chunk_id via FTSManager.generate_chunk_id)
   ↓ 异常 → delete_by_chunk_id rollback + audit
callback (best-effort)
```

Qdrant 先写是因为: Qdrant 写入失败 → 整 ingest 失败 (Step 5 抛 Exception). Qdrant 成功后再写 FTS, 即使 FTS 失败, vector search 仍然可用, 漂移由对账任务发现.

### 对账后台任务 (`concurrency/consistency_checker.py`)

```python
class ConsistencyChecker:
    """5min 间隔对账任务: FTS row count vs Qdrant point count.

    不修复, 仅 emit drift 审计 + 更新 gauge. 调用方在 lifespan 注册.
    """

    def __init__(self, fts: FTSManager, qdrant: QdrantManager,
                 audit_writer: AuditEmitter | None,
                 metrics_collector: MetricsCollector | None,
                 interval_s: int = 300) -> None: ...

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            await self._check_once()

    async def _check_once(self) -> int:
        """Run one consistency check.

        Returns:
            drift_count (int): |fts_active_count - qdrant_total_count|.
                0 means in sync.
        """
        try:
            # H5: FTS count excludes status='illegal' rows.
            # Qdrant has no 'illegal' status — total count is the truth.
            fts_count = self._fts.count_active()  # H2: new method on FTSManager
            qdrant_count = self._qdrant.count_points()
            drift = abs(fts_count - qdrant_count)
            if drift > 0:
                if self._audit_writer:
                    self._audit_writer.write(
                        "fts_consistency_drift",
                        drift_count=drift,
                        fts_count=fts_count, qdrant_count=qdrant_count,
                    )
                # M7: ekrs counter, incremented once per drift event
                if self._metrics_collector:
                    self._metrics_collector.drift_total.inc()
            return drift
        except Exception as e:
            logger.warning("consistency_check_failed: %s", e)
            return 0
```

**关键决策**:
- **不删除多余行**: 漂移可能是 FTS 写入滞后 (Qdrant 已成功, FTS 未到) 或 FTS 写入失败 (FTS 缺失). 误删 = 制造更大的不一致. T10a-2 原则: 检测 + 告警, 不修复.
- **5 分钟间隔 (M2)**: 父计划 T10a-2 行明确. `interval_s` 默认 300, env override `INDEX_CONSISTENCY_INTERVAL_S`. 任务通过 `lifespan` 注册 `asyncio.create_task(self._checker.run_forever())`; 服务关闭时 `task.cancel()`. `asyncio.CancelledError` 自然向上传播, lifespan cancel 干净停.
- **metric 名 (M7)**: `ekrs_index_consistency_drift_total` (Counter, `inc()` 每次发现 drift +1). 与父计划 §T10a-2 行 `index_consistency_drift` 一致, 加 `ekrs_` 前缀和 `_total` 后缀符合 Prometheus 命名约定.

### Qdrant count_points 加法 (父计划已闭 开放问题 1/5/6 但 count API 未实现)

Qdrant client 提供 `count()` 方法. T10a-2 引入 `QdrantManager.count_points() -> int` 单行方法, 用 `self._client.count(self._collection_name).count`. 集成测试 mock 这方法.

### FTSManager 新增方法 (Tb.2 实现)

| 方法 | 用途 | 来源 |
|---|---|---|
| `count_active() -> int` | 对账任务: count `WHERE status='active'` 排除 illegal 行 | H2 |
| `replace_doc(doc_hash: str, chunks: list[Chunk], *, version: int) -> int` | 整 doc 替换: delete_by_doc + bulk upsert (enumerate 生成 chunk_id) | H4 |

**`replace_doc` 签名**: `(doc_hash, chunks, *, version)` — `version` 进 payload_json 用于诊断 (T10a-5 双向映射扩展点), 不在 BM25 索引列. 返回写入行数. 集成测试用真实 Chunk 验证 round-trip.

### 审计事件 schema

`main.py:_EVENT_SCHEMAS` 新增:
```python
"fts_consistency_drift": {"drift_count"},
```

不需要在 T10a-2 注册 `fts_synced` / `fts_searched` — 那两个 T10a-7 才 emit. 本任务 pipeline 调用 `audit_writer.write("fts_synced", ...)` 在 T10a-7 之前 `write()` 返回 False 不抛, T10a-7 之后返回 True 进入审计. 设计上保持 audit emit 调用点稳定, schema 注册时机延迟. **M6 决策**.

### Iron Rules 合规

| Rule | 影响 |
|---|---|
| R1 | FTS 写只读 chunk.text + chunk.source_block_ids + scope_path, 不动 hint 提取 |
| R2 | 求解器接口不变 (FTS 只在 ingestion + 对账期间被调, 不进 solver) |
| R3 | 不动三闸门 (FTS 仅在 ingestion 后置, 不参与 recall / extract / solve) |
| R5 | SQLite FTS5 不是图库 |
| R7 | scope_path 写入 FTS 行 (与 T10a-1 一致); scope_filter 不在 ingestion 时用, 留给 T10a-4 |
| R8 | 对账比对 status='active' 计数 (排除 'illegal') |

## 4 个 TDD 任务 (Tb.1 / Tb.2 / Tb.3 / Tb.4, 跳过 IMPROVE)

| # | 任务 | 工作量 | 验收 |
|---|---|---|---|
| **Tb.1** | RED: pipeline FTS sync + consistency checker + Qdrant count_points 测试 | 1 天 | 8 个 fail test (含 FTS rollback 路径 + 对账 drift 审计) |
| **Tb.2** | GREEN: pipeline Step 5.6 + ConsistencyChecker + count_points + schema 注册 | 1 天 | 所有 Tb.1 测试 pass; 退化路径 (fts=None) byte-level 等于 Phase 9 |
| **Tb.3** | 集成测试: 真 Qdrant mock + 真 FTS tmpfile, 验证 drift detection | 0.5 天 | 5 个 round-trip pass |
| **Tb.4** | 文档 + 标签 + 记忆 | 0.5 天 | CHANGELOG + handbook + memory; 无新 tag; FF push master |

**Tb.1/Tb.2/Tb.3/Tb.4 不做 T10a-3/4/5/6/7 的工作**. RRF = T10a-3. retriever 接入 = T10a-4. chunk_id 时机 = T10a-5. golden 回归 = T10a-6. fts_synced/fts_searched schema + emit = T10a-7.

### Tb.1 测试用例枚举

`tests/unit/test_pipeline_fts_sync.py` 至少 8 个测试:
1. `test_pipeline_ingest_writes_fts_after_qdrant_success` — happy path
2. `test_pipeline_ingest_fts_failure_does_not_fail_ingest` — FTS 异常 → outcome 仍 success (warn log)
3. `test_pipeline_ingest_fts_rollback_deletes_partial_rows` — FTS 部分写后异常 → delete_by_chunk_id 撤回
4. `test_pipeline_ingest_fts_none_path_unchanged` — 退化路径 (fts=None) → Qdrant 写入路径不变
5. `test_consistency_checker_emits_drift_audit_when_counts_mismatch` — drift > 0 → fts_consistency_drift
6. `test_consistency_checker_no_emit_when_counts_match` — drift == 0 → 无 emit
7. `test_consistency_checker_count_failure_logged_no_emit` — Qdrant 不可达 → log warning, 不 emit
8. `test_qdrant_count_points_returns_int` — QdrantManager.count_points 单测

`tests/integration/test_pipeline_fts_sync_integration.py` 至少 5 个:
1. `test_pipeline_ingest_writes_both_qdrant_and_fts` — 真实 Qdrant mock + 真 FTS, ingest 后两个 store 都有
2. `test_fts_count_matches_qdrant_count_after_ingest` — 对账 happy path
3. `test_consistency_checker_detects_drift_after_partial_fts_failure` — 模拟 FTS 失败 → 对账发现 drift
4. `test_fts_rollback_clears_partial_rows_for_failed_doc` — FTS 中途失败后 doc 在 FTS 中完全不存在
5. `test_consistency_checker_drift_metric_incremented` — Prometheus gauge 增量

= 13 个用例, 超过 ≥8 下限.

## 标签策略

父计划 §"标签策略" 已规约: `phase10.1` 锁在 1c44eee (T10b-1, do-not-move); `phase10` 留给 T10a-7 closure. **本任务不开新 tag**.

**Push 路径 (M2)**: FF push master (单 commit 或多 commit, 任务结束才 push). refspec-push 作为 fallback.

## 验证闸门 (本任务关闭条件)

- [ ] `tests/unit/test_pipeline_fts_sync.py` ≥8 测试全 pass
- [ ] `tests/integration/test_pipeline_fts_sync_integration.py` ≥5 round-trip 测试全 pass
- [ ] mypy 干净 (49/49 → 50/50 标准不变)
- [ ] `make test` 全套不退化 (现有 631+ 测试 + Phase 10 T10a-1 30 测试 + T10b-1 60+8 测试)
- [ ] 退化路径 `fts=None` byte-level 等于 Phase 9 baseline (用现有 pipeline 回归测试套)
- [ ] CHANGELOG.md `[Unreleased] ## Added` 段写好
- [ ] ekrs-handbook.md §6 timeline 加 T10a-2 行
- [ ] memory `phase10-t10a-2-closed.md` 已写

## 风险

| 风险 | 缓解 |
|---|---|
| FTS 写入滞后导致短暂漂移 | 对账任务 5min 检测; emit drift 审计; 不修复 |
| 对账任务自身失败 (Qdrant 不可达) | `_check_once` 内 try/except, log warning, 不抛 |
| `fts_synced` schema T10a-7 才注册, 本任务 emit 会失败 | Tb.2 中 emit 用 try/except (audit emit 失败不阻断 ingestion); 注释标注 "T10a-7 正式 emit" |
| pipeline 注入新依赖破坏现有测试 | 用 `fts=None` 路径兼容现有 pipeline_path 测试; 新增 `test_pipeline_ingest_fts_none_path_unchanged` 显式锁退化语义 |
| count_points 在 Qdrant 1.x 早期版本不存在 | Qdrant 1.11+ 都有 `count()` 方法 (Phase 2 baseline 锁定 1.11); fallback 到 scroll + len() 不是首选, 增加 IO |

## 开放问题 (实施前关闭)

1. ~~**FTS 写异常时是否阻断 ingestion**~~ — **关闭**: 不阻断. Qdrant 是 truth-of-record; FTS 丢失由对账发现.
2. ~~**对账发现 drift 后是否自动修复**~~ — **关闭**: 不修复. 父计划 T10a-2 行明确"只告警 + emit, 不自动修复".
3. ~~**对账间隔**~~ — **关闭**: 5 分钟, 父计划 T10a-2 行明确.
4. ~~**ConsistencyChecker 注入方式**~~ — **关闭**: lifespan 注册 `asyncio.create_task`, 与现有 lifespan 风格一致.
5. ~~**Qdrant count API 选择**~~ — **关闭**: `client.count(collection).count` (Qdrant 1.11+ 标准 API).

**已无未关闭问题. 可开始 Tb.1.**
