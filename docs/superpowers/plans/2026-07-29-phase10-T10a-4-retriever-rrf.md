# Phase 10 T10a-4 — Retriever 并联接入 RRF 实施计划

## 父计划

[`2026-07-28-phase10-broad-spectrum-retrieval.md`](2026-07-28-phase10-broad-spectrum-retrieval.md) §T10a-4 行 (line 29) + §Iron Rules R4 已锁 "FTS 有结果时 RRF 重新排序, scope_priority 在 RRF 融合后过滤". 本计划不重复决议.

## 范围

将 `reciprocal_rank_fusion` (T10a-3) 接入 `EKRSRetriever`:

1. **`EKRSRetriever.__init__` 增 `fts: FTSManager | None = None` kwarg** (M1 决策). `fts=None` 走退化路径, byte-level 等于 Phase 9 baseline.
2. **`retrieve(query, top_k, active_scope)` 并联两路**: vector (existing) + FTS (new, 仅 fts 有配置时). `asyncio.gather` 平行. FTS 异常**隔离**: 不阻断 vector 路径, log warning + 走纯 vector 路径.
3. **FTS 命中 → payload 反查**: 新方法 `FTSManager.search_with_payload(query) -> list[tuple[chunk_id, payload_dict, score]]`. 从 FTS 行 `payload_json` UNINDEXED 列反查, 避免 N 次额外 Qdrant lookup. (M2 决策; 不影响 T10a-1 已通过的 30 测试.)
4. **RRF 融合 key**: 默认 `f"{doc_hash}:{source_block_ids[0]}"`. T10a-5 引入 `chunk_id` 后, 默认改 `chunk_id` (call-site 限定, 不破坏 T10a-5 之前的兼容路径).
5. **`RetrievalResult` 增 `fusion_stats: FusionStats | None = None` 字段**. fts=None 时 = None (退化路径), fts 有配置时 = T10a-3 dataclass. **M4 锁定**: 字段类型 `Optional` 而非 `dataclass.field(default=None)` 但 **field 不做 `None` 与 `dataclass` 区分; consumer 端用 `is None` 判定**.
6. **scope_priority 在 RRF 之后 apply** (父计划 R4 锁定): 仍走 `_rank_by_scope()`, 不动.

**T10a-4 边界**: retriever 接入 RRF + 并联检索. **不做**:
- chunk_id Qdrant payload schema 加字段 — T10a-5.
- fts_searched 审计 emit — T10a-7 (FusionStats 字段已就位, T10a-7 直接 consume).
- audit.log 配套 retry-failed/encode-cache — T10a-7.

## 设计

### 异步并发模型

```python
async def retrieve(self, query, top_k=40, active_scope=None) -> RetrievalResult:
    if self._fts is not None:
        # 并联两路, FTS 异常隔离
        vector_hits_task = asyncio.create_task(
            asyncio.to_thread(self._qdrant.search, query_text=query, top_k=top_k)
        )
        fts_hits_task = asyncio.create_task(
            asyncio.to_thread(self._fts.search_with_payload, query)
        )
        vector_hits, fts_hits = await asyncio.gather(
            vector_hits_task, fts_hits_task, return_exceptions=True
        )
        if isinstance(vector_hits, Exception):
            vector_hits = []  # vector 异常 → 降级到 FTS-only
        if isinstance(fts_hits, Exception):
            fts_hits = []  # FTS 异常 → log + 降级到 vector-only (M3 决策)
    else:
        vector_hits = self._qdrant.search(query_text=query, top_k=top_k)
        fts_hits = []
    
    # 构造 ranked lists (each is List[Chunk])
    vector_chunks = [self._payload_to_chunk(p, s) for p, s in vector_hits]
    fts_chunks = [self._payload_to_chunk(p, s) for _, p, s in fts_hits]
    
    # RRF 融合 (best-rank semantics by T10a-3)
    fused, stats = reciprocal_rank_fusion(
        [vector_chunks, fts_chunks],
        key_fn=lambda c: f"{c.doc_hash}:{c.source_block_ids[0]}",
    )
    
    # 提取 (chunk, fused_score) 元组, 重建 chunks + scores 列表
    fused_chunks = [c for c, _ in fused]
    fused_scores = [round(s, 10) for _, s in fused]  # 量化保持确定性
    
    # scope_priority 过滤 + 排序 (Phase 6B 既有逻辑)
    filtered = self._apply_scope(fused_chunks, fused_scores, active_scope)
    chunks, vec_scores, scope_scores, final_scores = self._rank_by_scope(*filtered)
    
    return RetrievalResult(
        chunks=chunks, vector_scores=vec_scores,
        scope_scores=scope_scores, final_scores=final_scores,
        fusion_stats=stats if self._fts is not None else None,  # 退化 = None
    )
```

**关键决策**:

- **`asyncio.gather(..., return_exceptions=True)`**: 两路独立异常隔离. FTS 失败 → log warning + fts_hits=[]; vector 失败 → log + vec_hits=[]. 任一存活路径仍产出 fused results, retriever 不抛 (M3 决策: 检索降级 vs ingestion 失败 — ingestion 必须显式 callback, 检索可静默降级 + 监控对账).
- **`asyncio.to_thread`**: Qdrant + FTS 都是 sync IO-blocking; `to_thread` 防止 event loop block. lifespan 已有 event loop, 不需要额外 create_thread.
- **`round(score, 10)`**: 防止 IEEE-754 漂移让 Phase 9 现有 retriever 测试 flaky. **M5 决策**: RRF 不量化 (浮点确定), retriever 输出端量化.
- **`key_fn = f"{c.doc_hash}:{c.source_block_ids[0]}"`**: Phase 9 现有 chunk 无 `chunk_id` 字段 (T10a-5 引入). 先用 doc_hash + 首个 source_block_id (与 Phase 9 PK 端 IR 一致). T10a-5 完成后 key_fn 改用 `c.chunk_id` (call-site update; T10a-5 plan 治理).
- **`fusion_stats=stats if self._fts is not None else None`**: fts=None 退化路径 fusion_stats=None, 保证 Phase 9 byte-level baseline (无 fusion_stats 字段 = 默认 None).
- **`_rank_by_scope`**: 现有逻辑不动 (Phase 6B 已 passing 测试). RRF 之后 scope 过滤 + 排序.

### `RetrievalResult` 扩展

```python
@dataclass
class RetrievalResult:
    chunks: List[Chunk]
    vector_scores: List[float]
    scope_scores: List[float]
    final_scores: List[float]
    fusion_stats: Optional[FusionStats] = None  # NEW (T10a-4). None = FTS disabled.
```

**byte-level 兼容 (M1+M4 锁定)**: 现有 Phase 6B 测试用 `assert result.chunks == []` / `assert result.vector_scores == [0.8]` 等字段级断言, 不比较 dataclass 整体. 加入 `fusion_stats: None` 字段 = 默认值, 不影响字段级断言. **新增字段对老测试 byte-level 透明**.

### `FTSManager.search_with_payload` (M2 决定)

```python
def search_with_payload(self, query, *, limit=40, scope_filter=None) -> list[tuple[str, dict, float]]:
    """Same FTS5 BM25 search, but also returns payload dict from
    `payload_json` UNINDEXED column. Returned tuples: (chunk_id, payload_dict, score).
    """
    row_chunks = self.search(query, limit=limit, scope_filter=scope_filter)  # [(chunk_id, score)]
    # 重新拼 SQL 一次拿 payload_json 避免 N+1 queries:
    placeholders = ",".join("?" * len(row_chunks))
    if not row_chunks:
        return []
    chunk_ids = [c for c, _ in row_chunks]
    rows = self._conn.execute(
        f"SELECT chunk_id, payload_json FROM blocks_fts WHERE chunk_id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    payload_map = {c: json.loads(p) for c, p in rows if p}
    score_map = dict(row_chunks)
    out = []
    for cid in chunk_ids:
        if cid in payload_map:
            out.append((cid, payload_map[cid], score_map[cid]))
    return out
```

**M2 锁定理由**: 不动 `search()` 已有签名, T10a-1 / T10a-2 现有 30 测试不破坏. `search_with_payload` 走**单次 IN-查询**而非 N 次单查询, 性能 = O(1) RTT.

### Iron Rules 合规

| Rule | 影响 |
|---|---|
| R1 | FTS 不参与 hint 提取 — retriever 仍然 extract_hints on fused chunks (Phase 6B 不动) |
| R2 | retriever 接口不变 — `_rank_by_scope` 是纯函数 (input chunks+scores → output 排序) |
| R3 | 三闸门不动 (只在闸门 1 内融合; 闸门 2 (extract) 闸门 3 (solve) 不动) |
| R4 | FTS 有结果时 RRF 重新排序, scope_priority 在融合后过滤 (父计划 R4 + 本 plan §设计), fts=None 时与 Phase 9 完全一致 |
| R5 | 不引入 KG |
| R6 | 不引入 cross-encoder (10c 范围); 不推断, 只 BM25 deterministic |
| R7 | scope_filter 仍 `OR`-restricted (FTS `_build_match_expr` 不变); retriever 端 active_scope 过滤亦不动 |
| R8 | `status='illegal'` FTS-side filter (T10a-1 既有) + Qdrant payload filter 不变 |

## 4 个 TDD 任务 (Td.1 / Td.2 / Td.3 / Td.4)

| # | 任务 | 工作量 | 验收 |
|---|---|---|---|
| **Td.1** | RED: retriever RRF 接入 + 退化路径测试 + FTSManager.search_with_payload 测试 | 0.5 天 | 9 个 fail test (5 retriever + 1 fts search_with_payload + 3 退化路径) |
| **Td.2** | GREEN: retriever integration + FTSManager.search_with_payload (最小) | 0.5 天 | 9 测试 pass; Phase 6B 既有 retriever 测试不退化 (4 个 test) |
| **Td.3** | IMPROVE: FTS 异常隔离 / 并联超时 / payload 缺字段鲁棒 | 0.25 天 | 3 边界测试 pass |
| **Td.4** | 文档 + 标签 + 记忆 + FF push | 0.25 天 | CHANGELOG + handbook + memory + FF push master; 无新 tag |

### Td.1 测试枚举

`tests/unit/test_retriever_t10a4.py` ≥9 测试:

1. `test_retrieve_fts_none_path_byte_level_equal_phase9` — fts=None kwargs 时, `result.fusion_stats is None`, chunks/scores 与 Phase 9 完全一致 (相同输入)
2. `test_retrieve_fusion_stats_none_for_fts_none_default` — 默认构造 (fts=None) → result.fusion_stats is None
3. `test_retrieve_fts_path_passes_fusion_stats` — 配置 FTS 后, retrieve 返回 FusionStats 三字段非 None
4. `test_retrieve_dual_path_fuses_chunks_with_rrf` — vector 与 FTS 重叠 + 独有都进入 fused
5. `test_retrieve_fts_exception_does_not_fail_vector` — FTS search 抛异常 → log warning + vector-only 结果
6. `test_retrieve_fts_exception_logs_warning` — FTS 异常被 logger.warning 记录
7. `test_retrieve_scope_priority_applied_after_rrf_fusion` — fts 配置时, _rank_by_scope 仍在 RRF 之后调用
8. `test_retrieve_concurrent_parallel_calls` — `asyncio.gather` 实证: 两次 retrieve 并发执行总耗时 < sum(单次) - evidence 用 mock time
9. `test_phase6b_retriever_tests_still_pass` — Phase 6B 既有的 4 测试不退化 (regression gate)

`tests/unit/test_fts_search_with_payload.py` ≥1 测试:

1. `test_search_with_payload_returns_payload_dicts` — FTS 写入后 search_with_payload 返回 (chunk_id, payload_dict, score) 元组

### Td.3 IMPROVE 边界测试

- `test_retrieve_fts_payload_missing_returns_empty_chunk` — FTS 行 payload_json 缺失/损坏 → 跳过该行 (不抛, 不污染 fused)
- `test_retrieve_no_fts_search_call_when_disabled` — fts=None → fts.search_with_payload 不被调 (instrumented mock)
- `test_retrieve_concurrency_gather_with_exception` — gather 收到 vector 异常 + FTS 异常 → 都不抛, 静默降级到空

### Td.4 文档

- CHANGELOG.md `[Unreleased] ## Added` 加 T10a-4 段
- ekrs-handbook.md §6 timeline 加 T10a-4 行
- CLAUDE.md Current State 加 T10a-4 行
- memory `phase10-t10a-4-closed.md`
- MEMORY.md 加 pointer
- git commit `feat(retrieval): ...`
- FF push master

## 标签策略

父计划 §"标签策略": `phase10.1` 锁 1c44eee; `phase10` 留给 T10a-7 closure. **本任务不开新 tag**.

**Push 路径 (M2)**: FF push master. refspec-push fallback per `phase7-closure.md`.

## 验证闸门

- [ ] `tests/unit/test_retriever_t10a4.py` ≥9 测试全 pass
- [ ] `tests/unit/test_fts_search_with_payload.py` ≥1 测试 pass
- [ ] Phase 6B 既有 retriever 测试 (4 个 `tests/unit/test_retriever.py`) 不退化
- [ ] mypy 干净 (新增 2 文件, 标准不变 50/50 → 52/52)
- [ ] `make test` 全套不退化 (现有 854+ 测试, 含 T10a-2 21 + T10a-3 17)
- [ ] 退化路径 fts=None byte-level 等于 Phase 9 (字段级断言验证)
- [ ] CHANGELOG.md `[Unreleased] ## Added` 写好
- [ ] ekrs-handbook.md §6 timeline 加 T10a-4 行
- [ ] memory `phase10-t10a-4-closed.md` 已写
- [ ] FF push master 成功
- [ ] 无新 tag

## 风险

| 风险 | 缓解 |
|---|---|
| FTS 异常导致 retrieve 抛 → consumer 莫名失败 | `gather(return_exceptions=True)` 隔离 + log warning + 走单路降级 |
| 并联超时 (FTS 超长 query) | T10a-4 暂不加 timeout; 留 T10a-7 配套 (已有 timeout api per Phase 6B); Td.3 IMPROVE 加 boundary test 验证 |
| `chunk_id` 字段未引入 (T10a-5 引入) | key_fn fallback to doc_hash + source_block_ids[0]; T10a-5 完成后 call-site update |
| `payload_json` 缺失/损坏 | Td.3 IMPROVE 加单测: 跳过该行; 不抛, 不污染 |
| Round(score, 10) 影响 Phase 9 baseline | M5: Phase 6B 测试用整数 score / 公式已知; 现有 vec_scores 0.8, 0.99 等小数 round(10) 与 Phase 9 同 |
| `fusion_stats: None` 默认导致现有 dataclass 比较失败 | M1+M4 锁定: Phase 6B 测试用 `result.vector_scores == [0.8]` 字段级断言, 不比较 dataclass 整体; 加单测覆盖 |

## 开放问题 (实施前关闭)

1. ~~**retriever 入口 async 化**~~ — **关闭**: `retrieve()` 改为 `async def`. 现有 Phase 6B 调用方 (main.py 路由) 用 `await` 已是 FastAPI 0.115 standard; `dev_ui` Streamlit 是 sync, 用 `asyncio.run` 包. (验证: 主线 main.py retriever.get_retriever 已被 Depends 注入, 路由层是 `async def` 已存在).
2. ~~**并联 vs 串行**~~ — **关闭**: 并联 (`asyncio.gather`). FTS 是 sync IO; `asyncio.to_thread` 包为 async. wall-clock < max(vector_lat, fts_lat) 而不是 sum.
3. ~~**FTS 异常隔离**~~ — **关闭**: `return_exceptions=True` + log warning + 单路降级. 不抛, 不污染 caller.
4. ~~**chunk_id 引入时机**~~ — **关闭**: T10a-4 用 fallback key (doc_hash + source_block_ids[0]). T10a-5 call-site update key_fn. 这是 call-site 字符串, 不动 product.
5. ~~**现有 retrieve() 是 sync**~~ — **关闭**: 改 `async def`. constraints.py 路由已是 `async def`, 现有 sync 调用无 `await` (FastAPI 容忍但在 async 函数里调 sync IO 会 block event loop). T10a-4 改 `async def` 必须同时:
   - `constraints.py:147, 208` 调改为 `await`
   - `tests/unit/test_retriever.py` 4 测试加 `@pytest.mark.asyncio` + `await`, 全靠 pyproject.toml `asyncio_mode = "auto"` 自动适配, 无 conftest 改动
   Td.1 RED 单测会先 verify 现有 sync 测试路径 (block-event-loop) 与新 async 路径的 byte-level 一致性.

**已无未关闭问题. 可开始 Td.1.**

## GSTACK REVIEW REPORT

**Run:** 1 (self-review pre-implementation) · **Status:** clean (1 patch recommended, applied below)
**Date:** 2026-07-29
**Reviewer:** claude (eng-review pass)

### Findings — 5 项 (1 MEDIUM + 4 INFO)

| # | Severity | Conf | Finding | Resolution |
|---|---|---|---|---|
| [M1] | MEDIUM | 8/10 | `retrieve()` 改 `async def` 后, `constraints.py:147, 208` 必须加 `await`; 否则 `RuntimeWarning: coroutine was never awaited` 静默退化 | ✅ Td.1 RED 显式覆盖 (T10a-4 1/4): 调用方 add `await`, 既有 retriever 测试加 `@pytest.mark.asyncio` + `await` |
| [I1] | INFO | 7/10 | T10a-4 默认 key_fn 用 `f"{doc_hash}:{source_block_ids[0]}"`; T10a-5 引入 `chunk_id` 时 call-site update 是否规范 | ✅ docstring + 内部 # M2 决策注释 (call-site 单点变化, 留 T10a-5 治理) |
| [I2] | INFO | 6/10 | `gather(return_exceptions=True)` + 静默降级 → 静默让监控视野少一次 FTS 故障 | ✅ Td.3 IMPROVE 边界覆盖; T10a-2 ConsistencyChecker 5min 对账已经存在, 故障最终可见 |
| [I3] | INFO | 5/10 | `asyncio.to_thread` 包装 sync Qdrant/FTS — 实际单进程测试用 sync 调用即可, 并联性不显著 | ✅ 单测 mock 双路耗时即可验证并联 wall-clock; 集成留 stress tooling |
| [I4] | INFO | 4/10 | `round(score, 10)` 量化可能在 Phase 6B 既有用例产生 1ULP 漂移; byte-level baseline 风险 | ✅ Td.1 RED #9 单测明确断言: Phase 6B 既有用例**字段级断言**, 不比较 dataclass 整体; 量化仅发生在 Phase 9 已有 score 与 RRF 无关 |

### Run 1 闭环 patch (M1 锁定) — 已应用

`constraints.py:147, 208` 加 `await`, `tests/unit/test_retriever.py` 4 测试加 `@pytest.mark.asyncio` + `await`. conftest 不变 (`asyncio_mode = "auto"` 全局生效, 单测无需显式装饰 when in `tests/unit/`).

### Run 1 Verdict

**QUALITY: 8.0/10**. 1 MEDIUM (M1 async propagation) 已 patch + 单测覆盖; 4 INFO mitigation 文档化. **Ready to implement** — proceed Td.1 RED.
