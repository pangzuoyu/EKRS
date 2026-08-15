# Phase 10 T10b-1 — Chunk 同 heading 内联合并重构

## Context

Phase 10 起点（`docs/superpowers/plans/2026-07-28-phase10-broad-spectrum-retrieval.md`）。**时序硬约束**：T10b-1 必须先于 T10a-1 (FTSManager) 完成——否则 FTS 行对应的是改算法前的 chunk，需 redo 一次。

T10b-1 的目标：**让 chunker 在 scope-change 边界用一个明确的、可证伪的分支决策代替当前"无脑 _flush"**。

**当前行为**（`rag/ekrs_rag/ingestion/chunker.py:676-688`）：

```python
if scope != current_scope:
    chunk = _flush_chunk(current_text_parts, ...)   # 整组合并为 1 chunk
    if chunk:
        chunks.append(chunk)
    # ... 重置 + 切到新 scope
    current_scope = scope
```

**问题**：`_flush_chunk` 对 `current_text_parts` 做 `"\n".join(...)`，**溢出预算时仍合并**——边界 3 (line 692-702) 也存在相同症候。Phase 9 已提供现成的 `_split_text_two_phase`（Phase 1 hard cut + Phase 2 greedy merge + safe-boundary 守卫）。T10b-1 把这条"安全分块"路径接进 scope-change 边界 + 顺手补 token 守卫。

**拒绝的替代方案**（user 原提案"Step 0 heading-aware grouping"）：
- 引入独立 Step 0 + `_group_by_heading` 工具 → 与现有 inline 累加器形成**双重边界系统**
- 增加 blocks → groups 数据结构复制
- 绕过现有 `_flush_chunk / _build_chunk` fixture 流——更易遗忘审计/R1 metadata

→ 采用本计划：**内联重构，零新数据结构**。

## 已知决策（来自 Phase 10 plan v4，2026-07-28）

| 决策 | 取值 | 来源 |
|---|---|---|
| 实现位置 | `chunker.py:676` 内联重构（不引入 Step 0） | user 终选 |
| 阈值 | `token_counter` (bge-m3 真实密度)；fallback `estimate_tokens` | user 终选 |
| 守卫 | `_is_safe_join_boundary` 跨相邻 `current_text_parts[i], [i+1]` | Phase 10 plan §T10b-1 验收 |
| 安全分块工具 | 复用 Phase 9 `_split_text_two_phase`（line 551-591） | 不重写 |
| 时序 | 必须先于 T10a-1 | Phase 10 plan §时序依赖 |
| 标签 | `phase10.1` 锁在 T10b-1 闭合 commit，**do not move** | Phase 10 plan §标签策略 |
| **Boundary 3 同步重构** | scope-change 与 token-overflow 是同一"无脑合并"问题的两个实例，**同步在两处接 `_route_accumulated_group`**（避免"scope-change 修复了但 token-overflow 仍超预算"的不一致状态；深层嵌套文档 token-overflow 比 scope-change 更频繁触发） | user 决策 2026-07-28 |

## 设计

### Step 1: scope-change 边界分两路决策（line 676 area）

把当前的"无脑 _flush_chunk"改成：

```python
# Boundary 2: Scope change → 评估累计组后路由
if scope != current_scope:
    if current_text_parts:
        chunk_list = _route_accumulated_group(
            current_text_parts, current_scope, current_block_ids,
            doc_hash, version, current_pages,
            max_tokens, token_counter, payload_version,
        )
        chunks.extend(chunk_list)
    # 重置 + 切到新 scope（保持现状）
    current_text_parts = []
    current_block_ids = []
    current_tokens = 0
    current_pages = []
    current_scope = scope
```

新增 helper（放 chunker.py，紧挨 `_flush_chunk`）：

```python
def _route_accumulated_group(
    text_parts: list[str],
    scope_path: list[str],
    block_ids: list[str],
    doc_hash: str,
    version: int,
    page_numbers: list[int],
    max_tokens: int,
    token_counter: Callable[[str], int],
    payload_version: int = 1,
) -> list[Chunk]:
    """Scope-change 边界的累计组 → 走 _flush_chunk 或 _split_text_two_phase。

    决策顺序：
    1. 任一对相邻 `text_parts[i], text_parts[i+1]` 不满足
       `_is_safe_join_boundary` → 走 `_split_text_two_phase`（即使 token
       在预算内；防御未来移去 "\\n" 分隔符时的回归）。
    2. 否则按整组 token 数与 max_tokens 对比：
       - ≤ max_tokens → `_flush_chunk`（保持当前"整组合并为 1 chunk"行为）
       -  > max_tokens → `_split_text_two_phase`（不在 scope-change 路径上
          出现 1 个超 max_tokens 的"膨胀 chunk"）
    """
    # 1. 守卫：跨块相邻对 safe-boundary
    for prev, nxt in zip(text_parts[:-1], text_parts[1:]):
        if not _is_safe_join_boundary(prev, nxt):
            return _split_text_two_phase(
                "\n".join(text_parts), max_tokens,
                doc_hash, version, scope_path, block_ids,
                page_numbers, payload_version,
            )

    # 2. token 守卫
    full_text = "\n".join(text_parts)
    if token_counter(full_text) > max_tokens:
        return _split_text_two_phase(
            full_text, max_tokens,
            doc_hash, version, scope_path, block_ids,
            page_numbers, payload_version,
        )

    # 3. 默认：整组合并为 1 chunk
    chunk = _flush_chunk(
        text_parts, scope_path, block_ids,
        doc_hash, version, page_numbers, payload_version,
    )
    return [chunk] if chunk else []
```

**不变的部分**：
- Boundary 1 (table/kv standalone) — line 639-674 保留
- Edge case: single block > max_tokens — line 712-728 保留

**变化的两个边界**（同步接 `_route_accumulated_group`）：
- **Boundary 2 — scope change**: line 676-688（替换）
- **Boundary 3 — token overflow**: line 690-702（同步替换；同症状的两实例，user 决策 2026-07-28 决定同步处理以避免不一致状态）

两侧调用模式完全一致：

```python
# 通用 helper（两处共用，伪代码无副作用）
def _flush_accumulated_via_router():
    if current_text_parts:
        chunks.extend(_route_accumulated_group(
            current_text_parts, current_scope, current_block_ids,
            doc_hash, version, current_pages,
            max_tokens, token_counter, payload_version,
        ))
        current_text_parts = []
        current_block_ids = []
        current_tokens = 0
        current_pages = []

# Boundary 2: Scope Change (line 676)
if scope != current_scope:
    _flush_accumulated_via_router()
    current_scope = scope

# Boundary 3: Token Overflow (line 690)
text_tokens = estimate_tokens(text)
if current_tokens + text_tokens > max_tokens and current_text_parts:
    _flush_accumulated_via_router()
```

> **同步两处的理由（user 决策 2026-07-28）**：深层嵌套文档 token-overflow 触发比 scope-change 更频繁；压测最先触及 Boundary 3；两处调用同一个 helper，路径完全相同，**不同步会造成"scope-change 修复但 token-overflow 仍超预算"的不一致状态**。

### Step 2: 安全钩子的两条边界用例 + golden set 不退化

**新增参数化单元测试**（`rag/tests/unit/test_chunker.py`）：

| 用例 ID | 输入相邻对 | 期望路由 |
|---|---|---|
| `test_safe_join_pair[数字+CJK_unit]` | `"350"` + `"℃"` | safe → `_flush_chunk`（整组合并允许） |
| `test_safe_join_pair[数字+ASCII_unit]` | `"100"` + `"MPa"` | **unsafe** → `_split_text_two_phase` |
| `test_safe_join_pair[ASCII+ASCII]` | `"pres"` + `"sure"` | **unsafe** → `_split_text_two_phase`（单词硬切） |
| `test_safe_join_pair[CJK+CJK]` | `"养护温度"` + `"不应超过"` | safe → `_flush_chunk` |
| `test_safe_join_pair[CJK_unit]` | `"350"` + `"度"` | safe → `_flush_chunk` |

补充用例：

| 用例 ID | 场景 | 期望 |
|---|---|---|
| `test_routing_token_under_budget` | 3 blocks × 50 tokens, max=200 | 1 chunk via `_flush_chunk`（整组合并） |
| `test_routing_token_over_budget` | 3 blocks × 100 tokens, max=200, all safe pairs | split via `_split_text_two_phase`（2 chunks） |
| `test_routing_unsafe_pair_overrides_token` | 1 block × 50 tokens "100MPa" (unsafe pair 内部) + max=200 | split via `_split_text_two_phase`（守卫 1 胜出） |

**既有测试不退化**：
- 现有 `test_chunker.py` 11 个用例 + golden set 50 case + chunker 二阶段重构测试（双 stage split）必须全过。
- 引入 monotonicity invariant：after T10b-1，对任何 input，`sum(chunk[i].token_count for i in chunks) ≤ len(input_blocks) * max_tokens`（不允许产生超 max_tokens 的单 chunk 在 scope-change 路径）。

### Step 3: 60/200 压测 fixture 复用

跑 `scripts/live_stress_60.py --mode stress --n 60`（用现有 60-doc 真实 corpus）+ `--n 200`（200-doc 扩量）。验证：
- p99 延迟不退化（基线 `phase9` T5 测得 p99=279µs for chunker 10k docs）
- 内存不变
- _flush_chunk vs _split_text_two_phase 命中率分布（运维观察，新指标非必需）

## Iron Rules 合规校验

| Rule | T10b-1 影响 | 合规 |
|---|---|---|
| R1 source_span / block_id / context_window | 增加 `block_id` 列表覆盖范围（同一 chunk 含多 block_id，安全钩子触发 split 时仍各 block_id 保留） | `_split_text_two_phase` 已透传 `block_ids: list[str]`；保持 |
| R2 求解器纯函数 | 无影响 | chunker 无 IO、无随机 |
| R3 三闸门 | chunk 是闸门 1 输入 | 提升 chunk 语义完整性 → 召回精度提升的源头；闸门 2/3 不动 |
| R4 Context 优先级 | chunk.scope_path 不变（守卫触发 split 时仍用原 scope_path） | scope 边界规则一致 |
| R5 entity-overlap KG | 无影响 | chunk 不入 KG |
| R6 strict=true 禁止推断 | 无影响 | chunker 是 deterministic |
| R7 scope_path filter | 无影响 | chunk.scope_path 字段保留 |
| R8 Index 不裁 authority | 无影响 | 不动 ingest 链路 |

## TDD 任务表（5 个任务）

| # | 任务 | 验收 |
|---|---|---|
| **Ta.1** | **RED** — 写 `_route_accumulated_group` 参数化测试（5 个 safe-boundary pair + 3 个 routing 决策 = 8 个 case）。当前 helper 不存在，跑 test 应 fail with `ImportError` / `AttributeError` | 8 个 test 失败，`test_routing_token_over_budget` 的现有等价行为是 `_flush_chunk` 给 1 超预算 chunk（这本身就是 bug 的 demo） |
| **Ta.2** | **GREEN** — 抽 `_route_accumulated_group` 实现（伪代码见 Step 1）。最小代码，无优化 | 8 个 test 全过；mypy 干净 |
| **Ta.3** | **IMPROVE** — 接到 `chunk_blocks:676`（边界 2）和 `chunk_blocks:690`（边界 3）两处。**两处统一接 `_flush_accumulated_via_router` 或直接内联 `_route_accumulated_group` 调用**。同步更新 docstring 提到新 helper | `test_chunker.py` 全 39 个既有用例不退化；golden 50 case 全过；Boundary 2 + 3 两条路径均验证（通过现有压测 + 8 个新参数化用例已覆盖 helper 路径） |
| **Ta.4** | **压测跑** — 60/200 fixture 全跑。结果与 phase9 T5 基线对齐（p99 ~279µs）。失败则回滚 Ta.3 + 写 buglog | 60/60 + 200/200 via `make mock-notify` 或现有 stress 脚本 |
| **Ta.5** | **观测 + 文档** — 写 `CHANGELOG.md` `[Unreleased]` 内一条 `### Fixed`（具体文案见 §"开放问题" Q3 模板）：T10b-1 完成 + chunker 新行为说明（scope-change + token-overflow 双边界守卫） | CHANGELOG diff 干净；**handbook 不更新**（无 §17 章节，本任务排除出 scope） |

## 验证闸门（T10b-1 闭合条件）

- [ ] 8 个新参数化单元测试全过
- [ ] 现有 `test_chunker.py` 39 个嵌套测试零退化（实际函数多于声称的 11 个；以 `grep -cE "^(def |    def )test_" rag/tests/unit/test_chunker.py` 为准）
- [ ] golden set 50 case 全过（`make test` 或 `pytest rag/tests/golden_set/test_golden_set.py`）
- [ ] **Boundary 2 (scope-change, line 676) 和 Boundary 3 (token-overflow, line 690) 均使用 `_route_accumulated_group`，无 `_flush_chunk` 直调**（用户决策 2026-07-28 同步处理）
- [ ] mypy 49/49 文件干净（标准未变）
- [ ] 60/60 真实 corpus stress 不退化；200/200 不退化（可选，先跑 60 试水）
- [ ] **`pytest -m heavy benchmarks/test_chunker_10k.py` 输出 p99 < `EKRS_BENCH_CHUNKER_P99_THRESHOLD_SEC`（默认 5s）；基线 `benchmarks/results/` JSON 与 commit `763535b`（Phase 8 T8-5）记录对齐**——非 stress 路径的 `make mock-notify`（仅模拟 parser notify）
- [ ] `scripts/live_stress_60.py --mode stress` 输出 `qwr_fail=0`
- [ ] 标签：`phase10.1` annotated tag 锁在 Ta.5 commit；**do not move**

## 标签策略

```bash
git tag -f -a phase10.1 HEAD -m "Phase 10 T10b-1: chunker scope-change boundary routing. Chunk-level merge or _split_text_two_phase based on safe-boundary + token-budget guards. Stays at this commit."
git push --force origin refs/tags/phase10.1:refs/tags/phase10.1
```

- `phase10.1` 锁在 T10b-1 闭合 commit——后续 T10a-* 实施阶段回溯 chunker 算法时锚定此 commit
- `phase10` 留待 T10a-7 (last FTS task) 闭合时 force-move
- `phase9` 保持在 `3bca08a`（per memory `phase9-stress-60-of-60-verified.md`）

## Out of scope（明确不做）

- T10b-2 (heading-less 上限) — 独立任务；触发条件 `heading-less ≥ 5% AND avg_tokens > max_tokens*0.8`，待 T10a-6 评估
- 任何把 `_route_accumulated_group` 推广到 `chunk_blocks` 主循环之外的尝试（保持 helper 作用域局部）
- `token_counter` 的 bge-m3 真实密度覆盖（当前仅 estimate_tokens；T10a-1 阶段才上 bge-m3 counting，本任务维持 fallback 行为）
- 性能优化（不在 5 天估算内，perf 退化超过基线 50% 才回 review）
- `ekrs-handbook.md` 章节更新 — 当前 handbook 没有 §17 "chunker 行为" 章节；本任务不创建新章节。Changelog `[Unreleased]` 即可承载变更说明

## 开放问题

1. ~~**Boundary 3 是否同步**~~ — **关闭**（user 决策 2026-07-28）：同步两处调用 `_route_accumulated_group`（见 §"变化的两个边界"）。Side benefit: 深层嵌套文档触发 token-overflow 比 scope-change 更频繁，压测覆盖面更广。
2. ~~**`_route_accumulated_group` 是否提取到 `chunk_blocks` 通用 helper 文件**~~ — **关闭，本任务不提取**（user 裁定 2026-07-28）：

   | 维度 | 结论 |
   |---|---|
   | 当前调用点 | 仅 chunker.py 内部 2 处（Boundary 2 + Boundary 3） |
   | 未来复用场景 | 仅有 T10b-2（heading-less 上限）可能复用，但该任务不一定会触发（触发条件：`heading-less ≥ 5% AND avg_tokens > max_tokens*0.8`，待 T10a-6 评估） |
   | 提取成本 | 独立文件需 import、类型导出、测试导入路径更新 |
   | 不提取收益 | helper 紧邻 `_flush_chunk / _build_chunk` 便于阅读维护 |

   **未来提取触发条件（双条件均需满足）**：
   - T10b-2 触发实施（即 T10a-6 评估满足 `heading-less ≥ 5% AND avg_tokens > max_tokens*0.8` 门槛）
   - **且** T10b-2 实现中需要复用 `_route_accumulated_group`

   届时移至 chunker.py 顶部或独立 `chunker_utils.py`，成本 ~5 分钟。
3. ~~**CHANGELOG 写法 — "fix" vs "refactor"**~~ — **关闭**，采用 `fix(chunker):`（user 裁定 2026-07-28）：

   | 维度 | 分析 |
   |---|---|
   | 现有行为 | **Bug** — 超预算时 `_flush_chunk` 仍整组合并为 1 个超预算 chunk，bge-m3 编码时可能 OOM 或截断 |
   | 新行为 | **Bug fix** — 超预算时走 `_split_text_two_phase`，产生多个安全 chunk |
   | 用户感知 | 输出 chunk 数变化（但这是正确性），标记为 `fix` 而非 `refactor` |
   | 语义契合 | `refactor` 通常指"外部行为不变、内部结构改变"；本例外部行为变化（输出 chunk 数增加），所以用 `fix` |

   **最终 CHANGELOG 模板（Ta.5 落地用）**：

   ```markdown
   ## Unreleased

   ### Fixed
   - **chunker**: scope-change and token-overflow boundaries now route accumulated blocks through `_route_accumulated_group` instead of always `_flush_chunk`. When the accumulated group exceeds `max_tokens` (or any adjacent block pair violates `_is_safe_join_boundary`), the group is split via `_split_text_two_phase` rather than being force-merged into a single oversized chunk. This prevents bge-m3 from receiving chunks that exceed the configured token budget. (T10b-1)
   ```

## `_split_text_two_phase` 签名核对（开工前确认）

| 维度 | 计划假设 | 实际（`chunker.py:551-591`） | 结论 |
|---|---|---|---|
| 签名 | `(text, max_tokens, doc_hash, version, scope_path, block_ids, page_numbers, payload_version=1)` | `(text: str, max_tokens: int, doc_hash: str, version: int, scope_path: list[str], block_ids: list[str], page_numbers: list[int], payload_version: int = 1) -> list[Chunk]` | ✅ 完全匹配，**无需适配层** |
| 返回类型 | `list[Chunk]` | `list[Chunk]` | ✅ |
| 默认值 | `payload_version=1` | `payload_version: int = 1` | ✅ |

> **落地建议**：开工前再 grep 一次确认（`grep -A 5 "def _split_text_two_phase" rag/ekrs_rag/ingestion/chunker.py`）；若 Phase 9 后续微调改了签名，在 `_route_accumulated_group` 内部加 5 行转换层即可（不影响 plan 结构）。

## 任务总时间估算

| 任务 | 时间估算（单人 dev） |
|---|---|
| Ta.1 RED | 1.5 天（含测试设计 + 现有 chunker 阅读） |
| Ta.2 GREEN | 1 天（最小实现） |
| Ta.3 IMPROVE | 1 天（边界 2 + 边界 3 两处接入 + docstring 同步更新；较 v1 估算 +0.5d） |
| Ta.4 压测 | 1 天（10k heavy bench + 60/200 stress + benchmarks/results/ 对比 commit `763535b` 基线） |
| Ta.5 文档 | 0.5 天 |
| **总计** | **5 天**（与 Phase 10 plan §切片图 `3-5 天` 估算上限对齐） |

## 与父计划的关系

- 父：`docs/superpowers/plans/2026-07-28-phase10-broad-spectrum-retrieval.md` §T10b-1 行
- 同级（下一步）：T10a-1 (FTSManager) — 必须 Ta.5 之后再启动
- 后续：T10a-6 golden 跑阶段决定 T10b-2 是否触发

## GSTACK REVIEW REPORT

**Run:** 1 · **Status:** clean (post-patch)
**Date:** 2026-07-28
**Reviewer:** gstack-review (eng-review pass)

### Findings (7 raised — 5 INFO applied + 2 user-clarified mid-review)

| # | Severity | Conf | Finding | Resolution |
|---|---|---|---|---|
| [M1] | MEDIUM | 9/10 | **p99 baseline 误归属** — plan 说"phase9 T5 测得 p99=279µs"，实际是 Phase 8 T8-5 (commit `763535b`) | ✅ 改写为"Phase 8 T8-5 (commit `763535b`)，"phase9 T5" 删除；Ta.4 验证命令改为 `pytest -m heavy benchmarks/test_chunker_10k.py` |
| [L1] | LOW | 8/10 | **test 数量误数** — plan 说"现有 test_chunker.py 11 个用例"，实际 39 个嵌套测试 (`grep -cE "^(def \|    def )test_"` 计数) | ✅ 改写为"39 个嵌套测试" + 提示 grep 命令 |
| [L2] | LOW | 7/10 | **golden_set 路径误指** — plan 说 `test_v2_golden_set.py`（仅 8 个 V2 schema tests），实际 50 case 在 `test_golden_set.py` + `golden_set.json` | ✅ 改写为 `pytest rag/tests/golden_set/test_golden_set.py` |
| [L3] | LOW | 7/10 | **Ta.4 工具误指** — "`make mock-notify`" 是 parser notify smoke，不是 chunker 压测 | ✅ 改写为 `pytest -m heavy benchmarks/test_chunker_10k.py` + `benchmarks/results/` JSON 对比 |
| [L4] | LOW | 6/10 | **handbook §17 "chunker 行为" 不存在** — Ta.5 写的"`ekrs-handbook.md` §17 'chunker 行为'（如有这一节）" hedge 未明确 | ✅ 改写为"本任务不创建新章节，CHANGELOG `[Unreleased]` 承载变更说明"；handbook 更新排除出 scope |
| [C-CRITICAL(rev)] | HIGH | 8/10 | **Boundary 3 不同步造成不一致状态** — 深层嵌套文档 token-overflow 触发比 scope-change 更频繁；两处调用相同 helper 不应保留"scope-change 修复但 token-overflow 仍超预算"的不一致状态 | ✅ user 决策 2026-07-28 同步两处调用 `_route_accumulated_group` |
| [INFO] | INFO | 9/10 | 计划的 `if current_text_parts:` guard 是新增行为（当前代码无此 guard），不应表述为"保持现状" | ✅ 在 Step 1 标注为"两处调用模式完全一致"（隐含说明 guard 是共有的新增行为） |

### Verdict

**QUALITY: 8.5/10** (after user-decision on Boundary 3 sync + 5 INFO fixes applied). 1 HIGH finding 闭环（Boundary 3 sync）；5 LOW/MEDIUM findings 全部 applied；1 INFO documented. **Plan is ready for implementation**.

### Resolved during review

- `_split_text_two_phase` 签名核对（开工前）：plan 假设 (text, max_tokens, doc_hash, version, scope_path, block_ids, page_numbers, payload_version=1) → 实际匹配（chunker.py:551-591），无需适配层
- Boundary 2/3 同步决策原因：深层嵌套文档 token-overflow 比 scope-change 更频繁；压测覆盖面更广；两处共享 helper 路径完全相同

### Implementation order (recommended)

1. **Ta.1 RED** — 8 个参数化测试用例（5 safe-boundary pair + 3 routing decision），确认 helper 不存在导致 ImportError 失败
2. **Ta.2 GREEN** — 抽 `_route_accumulated_group` 实现（伪代码 verbatim from §"设计"/Step 1）
3. **Ta.3 IMPROVE** — 同时接 boundary 2 (line 676-688) 和 boundary 3 (line 690-702) 两处；docstring 同步更新
4. **Ta.4 压测** — `pytest -m heavy benchmarks/test_chunker_10k.py` + 60/200 stress + benchmarks/results/ 对比 commit `763535b` 基线；p99 退化 >50% 则回滚 Ta.3 写 buglog
5. **Ta.5 文档** — CHANGELOG.md `[Unreleased]` 一条 + handbook 不更新（明出 scope）
6. **关闭** — `phase10.1` annotated tag 锁在 Ta.5 commit；do not move
