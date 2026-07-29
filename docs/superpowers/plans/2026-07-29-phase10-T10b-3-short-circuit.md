# Phase 10 T10b-3 — 强信号短路检测 (Exact-Match Short-Circuit)

## 父计划

[`2026-07-28-phase10-broad-spectrum-retrieval.md`](2026-07-28-phase10-broad-spectrum-retrieval.md) §T10b-3 行 (line 25).

## 触发条件

parent §54 决策路径:
> recall@10 ≥ baseline **且** 工程标识符命中 ≥ 2/3 → 10b 压缩为"只补强信号短路"

T10a-6 决策数据:
- 50 case golden set 0 退化 (recall@10 ≥ baseline ✓)
- 3 个工程标识符 BM25-only recall@1 = 1.0 (3/3 ≥ 2/3 ✓)

**两项触发条件满足**. T10b-3 启动. T10b-2 不启动 (heading-less 数据未量化).

## 范围

T10b-3 在 retriever 末端加**强信号短路**: 当用户查询 `q` 是某个 retrieved chunk 的 `chunk.text` 子串 (精确匹配), 直接返回该 chunk 跳过 RRF fusion 与并发 retrieve-fan-out 的成本端. 全局启用, 不门控于 strict 模式 (parent §157 patch: 短路是确定性优化, 与 R6 strict-mode 推断无关).

1. **`is_exact_match(query, chunks)` 谓词** — 标量化检索 (vector + FTS) 完成后, RRF 之前调用. 谓词返回 `List[int]` (匹配 chunk 的索引), 空列表 → 不短路, 正常 RRF. 多 chunk 匹配 → 全返回 (按 RRF score 之前的 vector_score 排序).
2. **`_score_for_short_circuit` 优先级** — 短路命中时, `vector_scores[i] = 1.0` (最高, 大于任何真实向量分). `scope_scores[i] = 0.0` 保留 (后续 _rank_by_scope 仍跑). 这样短路路径的排序语义与正常路径兼容.
3. **`RetrievalResult.fusion_stats` 短路处理** — 短路命中时 `FusionStats(vector=N, fts=0, both=0)` (因为 vector+FTS **也**没跑 RRF, 它们被短路), 同时新建 `short_circuit: bool = False` 字段标识本轮走了短路.
4. **`fts_searched` audit emit 在短路路径** — emit `fts_searched` with `vector_hits=N, fts_hits=0, both_hits=0, short_circuit=True`. **不**省略 emit (运营可见性: 短路命中率是查询画像关键指标).
5. **Iron Rules R6 strict 兼容性** — 短路路径在 strict 模式下**不**开启独立跳过 (parent §25 + §157). strict 模式仍走 solver 逻辑; 短路只影响 retrieval, 不影响 solver. 验收 (c): strict 短路 + 非 strict 短路返回**同一组 chunks** (集合相等; 仅顺序可能不同, 但 `_rank_by_scope` 后应一致).

**T10b-3 边界**:
- **不做**: cross-encoder rerank (T10c 推迟); heading-less 上限 (T10b-2 推迟); 短路阈值化 (现在恒启用); 跨引擎短路 (MCP adapter 不在范围).
- **不做**: 修改 `IngestionOutcome` enum 或 `_EVENT_SCHEMAS` (事件名不变; 复用 `fts_searched`).
- **不做**: FTS-only 短路 (FTSManager 的 identifier recall 由 T10a-6 验证 = 3/3; 短路只在 retriever-side 命中 vector+FTS union).

## 设计

### 谓词语义

```python
def _is_exact_match(query: str, chunks: List[Chunk]) -> List[int]:
    """Return indices of chunks whose text contains query as substring (case-sensitive)."""
    q = query.strip()
    if not q:
        return []
    return [i for i, c in enumerate(chunks) if q in c.text]
```

**Case sensitivity**: 默认 sensitive. 决策: **保留 case-sensitive** (工程标识符通常大小写敏感如 `A312-TP316`; CJK 查询自然 case-insensitive). 不引入 case-insensitive flag (避免配置蔓延).

**Match type**: substring. 决策: **substring 而非 whole-match** (parent §25 描述的"查询子串 = chunk.text", 字面理解). Whole-match 会让"温度 ≤ 80℃"这样的查询 无法匹配回包含该串的 chunk.

**Multi-match**: 多 chunk 同时包含 query → 全部返回. RRF 旁路, 按 vector_score 排序 (后续 `_rank_by_scope` 处理).

### 短路触发

```python
async def retrieve(self, query, top_k, active_scope=None):
    # Step A: vector + FTS union (existing T10a-4 code, unchanged)
    vector_chunks, fts_chunks = await self._parallel_retrieve(query, top_k)
    
    # Step B: 短路判定 — 扫 vector+FTS unioned chunks
    unioned_chunks = self._merge_union(vector_chunks, fts_chunks)  # dedup by chunk_id
    exact_match_idx = _is_exact_match(query, unioned_chunks)
    
    if exact_match_idx:
        # Short-circuit path
        short_chunks = [unioned_chunks[i] for i in exact_match_idx]
        # RRF 旁路: vector_scores hardcoded to 1.0; fusion_stats 标识短路
        fused_chunks = short_chunks
        fused_scores = [1.0] * len(short_chunks)
        fusion_stats = FusionStats(vector=len(short_chunks), fts=0, both=0)
        short_circuit = True
    else:
        # Existing RRF path (T10a-4)
        fused_chunks, fused_scores, fusion_stats = reciprocal_rank_fusion(...)
        short_circuit = False
    
    # Step C-D: scope_filter + _rank_by_scope + extract_hints (existing)
    ...
    
    return RetrievalResult(..., fusion_stats=..., short_circuit=short_circuit)
```

### Iron Rules 合规

| Rule | 影响 |
|---|---|
| R1 | 不改 hint 提取 (短路 = retrieval-only optimization); source_span 不变 |
| R2 | 短路是 retriever-side, solver 不变 (R2 纯函数) |
| R3 | 短路在 recall 阶段; extract + solve 正常走 |
| R4 | scope_priority 仍 AFTER 短路; 短路命中 chunk 也走 scope_filter + _rank_by_scope |
| R5 | 不引入 KG / graph |
| R6 | 短路**不**绕过 strict 模式 (parent §25 + §157); 短路 = 确定性操作, 与 strict-mode 推断无关; 短路返回的 chunk 仍进 solver, solver 拒绝 inferred constraint 时不变 |
| R7 | scope_path 仍带; 短路 chunk 也带 scope_path |
| R8 | status='illegal' 仍过滤 (在 vector/FTS retrieve 阶段, 短路不改这个语义) |

## TDD 任务 (Th.1 / Th.2 / Th.3 / Th.4)

| # | 任务 | 工作量 | 验收 |
|---|---|---|---|
| **Th.1** | RED: 谓词 + 短路 path + scope_priority 兼容 + strict 模式测试 + golden 短路 case | 0.5 天 | ≥8 个 fail test |
| **Th.2** | GREEN: 谓词 + 短路 path + score=1.0 标记 + scope_priority 兼容 + short_circuit 字段 | 0.5 天 | ≥8 测试 pass |
| **Th.3** | IMPROVE: latency bench 验证短路 < 50% standard path + regression + 0 退化 | 0.5 天 | bench 数据 + 0 退化 |
| **Th.4** | docs + CHANGELOG entry + memory + FF push | 0.25 天 | docs + push |

### Th.1 测试枚举

`tests/unit/test_short_circuit_t10b3.py` ≥8 测试:

1. `test_is_exact_match_predicate_returns_matching_indices` — 1 chunk + query in text → 1 index
2. `test_is_exact_match_no_match_returns_empty` — query not in any chunk → `[]`
3. `test_is_exact_match_multiple_chunks_match` — query 在 2 个 chunk text 中 → 2 indices
4. `test_is_exact_match_case_sensitive` — `a312-tp316` 不匹配 `A312-TP316` (默认 sensitive)
5. `test_is_exact_match_empty_query_returns_empty` — `query=""` → `[]` (no false-positive)
6. `test_retrieve_short_circuit_skips_rrf_when_match` — retriever 配 fts+audit, query 在 chunk text 中 → `short_circuit=True`, `fusion_stats=FusionStats(N, 0, 0)`, RRF 不调用 (mock 验证)
7. `test_retrieve_no_short_circuit_when_no_match` — retriever 配 fts+audit, query 不在任何 chunk → `short_circuit=False`, 走 RRF 正常路径
8. `test_retrieve_short_circuit_emits_fts_searched_with_zero_fts_hits` — 短路仍 emit `fts_searched` (运营可见性)
9. `test_short_circuit_returns_identical_chunks_set_across_strict_modes` — strict=true vs strict=false 短路路径返回**同一组 chunk_id** (parent §25 (c))
10. `test_short_circuit_respects_active_scope_filter` — 短路命中 chunk 但不在 active_scope → 被过滤 (scope_filter 仍生效)

### Th.3 latency bench 验证

`scripts/t10b3_short_circuit_bench.py`:
- 控制: random 100 查询 × 50 corpus docs (用 T10a-6 fixtures)
- 短路率: 30 个查询是某 chunk.text 的精确子串 (触发短路), 70 个随机 (走 RRF)
- 度量: 短路路径 avg latency vs RRF 路径 avg latency; 目标: 短路 < RRF × 50%
- 接受: 短路路径 p99 < 10ms (避免新 warmup 路径)

### Th.4 文档

- CHANGELOG.md `[Unreleased] ## Added` 加 T10b-3 条目 (1 段, < 30 行)
- ekrs-handbook.md §6 加 T10b-3 行 (1 行, 短)
- CLAUDE.md Current State 加 T10b-3 行
- memory `~/.claude/projects/.../memory/phase10-t10b-3-closed.md`
- MEMORY.md 加 pointer

**无新 tag** — `phase10` 已经在 `2e1d9fa` 闭合; `phase10.1` 在 T10b-1 (1c44eee). T10b-3 是 `phase10` 之内的 incremental commit. 计入 `phase10` 累计测试数.

## 验证闸门

- [ ] `tests/unit/test_short_circuit_t10b3.py` ≥8 测试 pass
- [ ] 完整 suite 622 + 新测试 0 退化
- [ ] latency bench: 短路 p99 < standard 50%
- [ ] 短路路径在 strict=true = strict=false 集合相等 (parent §25 (c))
- [ ] mypy 干净
- [ ] CHANGELOG T10b-3 条目已写
- [ ] memory `phase10-t10b-3-closed.md` 已写
- [ ] FF push master 成功

## 风险

| 风险 | 缓解 |
|---|---|
| 短路阈值化 (何时短路 vs 何时 RRF) | parent §25 明示 "短路全局启用"; 不引入阈值 |
| multi-match 短路返回过多 | 多 chunk 短路时, 仍走 `_rank_by_scope` (R4); top-k 仍生效 (取 top_k 个); scope_filter 仍生效 |
| 短路 vs strict 模式交互 | parent §157 patch 明示: 短路与 strict 无关, 仅 deterministic 优化; 短路 = 集合相同, 仅路径不同 |
| 短路路径错过新审计指标 (e.g., short_circuit rate) | emit `fts_searched` 不省略; `RetrievalResult.short_circuit` 字段加; 监控可见性靠这些 |
| 短路命中但 chunk.text 包含换行符不一致 | substring 默认 strip + trim; 测试覆盖 |

## 开放问题 (实施前关闭)

1. ~~**短路是否在 strict 模式跳过**~~ — **关闭**: 不跳过. parent §157 patch + parent §25 (c) 验收. 短路是检索优化, 不是 strict-mode 推断.
2. ~~**case sensitivity**~~ — **关闭**: 默认 sensitive (决策见"谓词语义"段). 工程标识符 `A312-TP316` 与 `a312-tp316` 视为不同.
3. ~~**substring vs whole-match**~~ — **关闭**: substring (parent §25 字面解释"查询子串 = chunk.text").
4. ~~**multi-match 排序**~~ — **关闭**: 多命中时按 vector_score 降序; 之后 `_rank_by_scope` 仍跑; top_k 仍 cap.
5. ~~**短路时 `fts_searched` audit**~~ — **关闭**: 仍 emit. 运营指标需要短路命中率 (短期可加 stdout log; 长期 Prometheus counter 留 Phase 11).

**已无未关闭问题. 可开始 Th.1 RED.**

## GSTACK REVIEW REPORT

**Run:** 1 (self-review pre-implementation) · **Status:** clean (1 patch recommended, applied below)
**Date:** 2026-07-29
**Reviewer:** claude (eng-review pass)

### Findings — 5 项 (1 MEDIUM + 4 INFO)

| # | Severity | Conf | Finding | Resolution |
|---|---|---|---|---|
| [M1] | MEDIUM | 7/10 | `RetrievalResult` 加 `short_circuit: bool` 字段会改变消费者字段枚举 (Phase 6B tests; constraints.py) | ✅ 字段类型 `bool = False` 默认 — 现有消费者代码不受影响 (`RetrievalResult(...)` call sites 不传 `short_circuit`, 用默认值 False 即旧路径); 测试用 `result.short_circuit is True/False` 仅在新测试里断言 |
| [I1] | INFO | 5/10 | 短路路径 `vector_scores` 全 1.0 可能让 `_rank_by_scope` 失效 (因为 `(1 + scope) * 1.0 = 1 + scope`, 排序由 scope 决定) | ✅ 这是正确语义: scope_priority R4 不变; 短路命中 chunk 之间的顺序完全由 scope 决定 (而非随机) |
| [I2] | INFO | 4/10 | case-sensitive 默认对 CJK / 中文查询有 noop (CJK 不区分大小写); 工程标识符 case-sensitive 是关键 | ✅ 测试 #4 锁住 ASCII case-sensitive; CJK 行为降级为 substring match (自然行为) |
| [I3] | INFO | 4/10 | latency bench 需要 warm-up, 否则首次 spinup noise 偏高 | ✅ 计划 §Th.3 加 `bench_warmup=10` 调用预热; 取后 N 次 stable 数据 |
| [I4] | INFO | 3/10 | `_payload_to_chunk` 已经返回 chunk, 短路不需要重新调用 — 重用 | ✅ 利用现有 `_payload_to_chunk` (T10a-5 chunk_id + T10a-4 payload shape), 短路路径构建 chunk list 直接复用 |

### Run 1 Verdict

**QUALITY: 8.0/10**. 1 MEDIUM (M1 field addition safety) 已分析 patch + mitigation (默认 False); 4 INFO mitigation 文档化. **Ready to implement** — proceed Th.1 RED.