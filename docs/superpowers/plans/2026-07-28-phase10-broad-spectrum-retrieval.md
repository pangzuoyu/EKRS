# Phase 10 — 广谱检索增强 (Broad-Spectrum Retrieval)

## Context

Phase 9 已交付（commit ~`3bca08a`）：chunker 二阶段重构 + live_stress_60.py 三模式（含 offline / retry-failed）。`phase9` 标签已 force-move，CHANGELOG.md 已填。

`docs/superpowers/research/2026-07-24-*.md` 这批研究文档虽然是 Phase 9 期间写的、标题也称"Phase 9"，但其描述的工作（BM25 + RRF + cross-encoder + MCP）**实际从未交付**——Phase 9 当时只走了 chunker 路径 + 部署加固（rate-limit / secret rotation / smoke canary 等）。所以这批研究**自然落到 Phase 10 范围**。

跨文档裁决（[`2026-07-24-phase9-cross-doc-adjudication.md`](../research/2026-07-24-phase9-adjudication.md) 已锁定结论）：
- 存储：Phase 10 保留 Qdrant + 新增 SQLite FTS5；Zvec 替换推 Phase 11+（独立工程，不与检索增强混合）。
- RRF：k=60 硬编码，不暴露 API；调优留 10b golden 回归后评估。
- FTS5 tokenizer：`unicode61 remove_diacritics 2` 默认；jieba 可选 follow-up。
- Cross-encoder：strict=true 时**强制跳过**（R2 纯函数 + R6 strict 不可推断）。
- 不做：LLM query expansion（移交 Phase 11 via MCP `structured_search`）。

## 推荐：Phase 10a (FTS5 + RRF 核心) 优先启动

最小成本验证广谱检索价值。完成后即可测量 recall@K 是否优于纯向量，决定 10c 重排是否值得做。

**时序依赖提醒**：T10b-1（heading-as-unit merge）**必须先于 T10a-1** 完成 —— 否则 FTSManager 写的是被改算法前的 chunk，FTS 行需要重做一次。

| 任务 | 描述 | 验收 |
|---|---|---|
| **T10b-1** ★**前置**★ | **chunker.py:676 内联重构**：scope-change 时先评估累计组 token 数；超预算走现有 `_split_text_two_phase`，否则整组合并为 1 chunk。**不引入 Step 0 边界层**（避免双重边界系统）。阈值 = `token_counter` (bge-m3 真实密度)，fallback 到 `estimate_tokens`。**安全钩子**：在 `_flush_chunk` 整组合并调用前，对 `current_text_parts` 每对相邻块 `(last_n, first_{n+1})` 跑 `_is_safe_join_boundary`；**任一对不安全则强制不合并**，走 `_split_text_two_phase` 路径。新增单元测试 "block A 末尾 `350` + block B 开头 `℃` → 强制不合并" | 金集 50 case 不退化 + ≥3 个 heading-spanning 用例（"同 heading 多 block → 整组合并为 1 chunk"）+ 1 个跨 block number/unit 边界安全测试；60/200 压测 fixture 复用 |
| **T10b-3** | **强信号短路检测**：精确匹配查询（用户查询子串 = Qdrant payload 中某 block_id 的 `chunk.text`）跳过 RRF 直接返回目标 chunk。**短路逻辑全局启用，不门控于 strict 模式**。原因：精确匹配短路是确定性操作，不是推断，与 R6 无冲突；strict 模式下短路仍然可用（且结果更确定）。 | (a) 加 `is_exact_match(query, chunks)` 谓词 + golden set 精确匹配用例 (b) latency 测试：短路路径 p99 < 标准路径的 50%（决定 10c 价值）（c）**strict 模式验证**：短路仍在 strict 模式下工作，返回集合与 RRF 一致（只影响性能，不影响结果集） |
| T10a-1 | FTSManager — 建表/CRUD/BM25 归一化（tokenizer = `unicode61 remove_diacritics 2`；不启用 porter，避免破坏牌号完整性如 `stainless → stain`） | 单元测试 + smoketest 用 T10a-6 的 3 个精确标识符查（`A312-TP316` / `GB/T 12459` / `1.6MPa`），验证 token 不会被拆碎 |
| T10a-2 | 摄入流水线同步写 FTS；**对账机制：低频后台任务（5min 间隔）仅比对 FTS vs Qdrant 总数，更新指标 `index_consistency_drift`；发现漂移时只告警 + 发射 `fts_consistency_drift` 审计事件，不自动修复**（避免误删） | FTS 行数 = Qdrant 点数；漂移时审计可重建；指标可观察 |
| T10a-3 | reciprocal_rank_fusion 纯函数 + `FusionStats` 数据类（`vector_hits` / `fts_hits` / `both_hits`）一并返回 | 单元 + 边界（k=60、单/双列表、空列表）；FusionStats 字段供 T10a-7 审计事件直接取用，retriever 层不重复算 |
| T10a-4 | retriever 并行检索 + RRF；退化模式 `FTS=None` **byte-level 等于 Phase 9**（比较范围：仅 `retriever.retrieve()` 返回的 `RetrievalResult`，不含后续求解器——求解器是 R2 纯函数，一致输入即一致输出） | 退化模式对比器（基于 `pytest --snapshot` 或等价）通过；现有 346+ 测试不退化 |
| T10a-5 | block_id EKRS 侧生成 + 双向映射（FTS↔Qdrant）。**生成规则：分块完成后、Qdrant 写入前由 EKRS 生成，格式 `{doc_hash[:8]}-{chunk_index:04d}`，同时写入 Qdrant payload 和 FTS5 表，不依赖 Qdrant 返回的 point ID。映射存 FTS 行 JSON column**（避免独立 lookup 表的同步问题）。**注意命名空间共存**：Qdrant payload 已有 `block_id` 字段（来自 ir_parser 的 UUID，不可重写）；新字段命名 `chunk_id`（并存于 payload + FTS 行 JSON），不替换 `block_id` | round-trip 一致（FTS 行 → Qdrant point 唯一，反之亦然）；回归测试覆盖已有 `block_id` payload 路径 |
| T10a-6 | golden set 50 case 全过 + ≥3 工程标识符精确查（`A312-TP316` / `GB/T 12459` / `1.6MPa`） | recall@10 ≥ Phase 9 baseline；**同时记录 3 个工程标识符的 BM25-only recall@1**（作为 10c 是否值得做的决策依据）|
| T10a-7 | 审计事件 `fts_synced` / `fts_searched`（**不进入 `IngestionOutcome` enum**——它们是中间步骤而非最终态；`IngestionOutcome` 保持 ingestion 完成时最终态集合） | audit.log 回放确定性保留；`fts_synced` 在 chunk upsert 后 emit；`fts_searched` 在 retriever 调用 RRF 时 emit，附 `FusionStats` |


## Phase 10 完整切片（含 T10b-1 前置依赖）

```
T10b-1 (chunker 内联重构)   3-5 天  ★ 前置依赖，时序上必须先做
    ↓
T10a-1..7 (FTS+RRF)        2-3 周  ★ 推荐起点
    ├── T10b-2 (heading-less 上限)  候选，触发条件：heading-less 文档 ≥5% 且平均 tokens > max_tokens*0.8，待 T10a-6 分析后定夺  ← 不是承诺 1 周
    ├── T10b-3 (强信号短路)        <1 周  ← T10a recall 后可选
    ├── T10c (cross-encoder)       2-3 周  ← 门控于 10a recall 数据
    └── T10d (MCP adapter)         2-3 周  ← 仅依赖 10a search 能力，可并行
```

总预估：7-11 周完整周期。T10b-1 是必做且必须先做；T10a 是必做；T10b-2/3、T10c、T10d 是评估后的可选项。

## 10b 现状评估（合并到 T10a-6 内部）

刚刚完成的 chunker 二阶段重构对应 10b 的 *boundary safe-join* 部分，但 [research 设计](../research/2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md) 中的 T9b-1 `break-point 评分系统`（代码块不被切分）和 T9b-3 `强信号短路检测`（精确匹配查跳过重排）尚未实现。

**判定方法**（作为 T10a-6 golden set 结果的附录当场写）：
- recall@10 ≥ baseline **且** 工程标识符命中 ≥ 2/3 → 10b 压缩为 "只补强信号短路"（<1 周）
- recall@10 退化 **或** 标识符 < 2/3 → 10b 需完整实施（break-point 评分）

**T10b-1 锁定设计**（用户最终决策，2026-07-28）：
- 内联重构 `chunker.py:676`（不引入 Step 0）
- 阈值：`token_counter` (bge-m3 真实密度) > `estimate_tokens` 粗估
- 安全钩子：合并前 `_is_safe_join_boundary` 验证跨 block 边界
- 时序：**先于 T10a-1** —— 否则 FTS 写入被废弃

**T10b-2 候选（heading-less 文档处理）**：
chunker.py:704-709 现有累积逻辑只在 (a) scope change / (b) 单 block 超额 / (c) 循环结束时 flush。如果某文档**完全没有 heading_path**（或只有一个 root heading），那么 scope-change 永不触发，所有 block 累积到循环末尾被一次性 flush — 这可能突破 `max_tokens` 预算。**这与 T10b-1 内联合并独立**：T10b-1 在 scope 变化时优化合并；T10b-2 在 scope 不变化时保证 token 上限。两者正交。**建议在 T10a-6 评估时同时分析** heading-less 文档的 chunk size 分布，决定是否实施 T10b-2。

**触发实施条件**（在 T10a-6 golden set 运行阶段产出）：
- heading-less 文档占比 ≥ **5%**
- 同时其 chunk 平均 tokens > `max_tokens * 0.8`

满足两个条件同时才实施 T10b-2；任一不满足则跳过、关闭候选。**实施时增加 chunker 累计块数 / 累计 tokens 双阈值 flush**（沿用 Phase 9 hard cut + 20% lookback 的安全策略）。该条件以追加段写入 T10a-6 golden 结果的附录 A，并在交叉裁决文档 §"Phase 10 closure"中列出最终采纳/不采纳的决定。

## 关联研究交付物（按重要性）

| 研究 | 状态 | 10a 是否直接引用 |
|---|---|---|
| [broad-spectrum-retrieval-port-design](../research/2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md) | 主设计 | ★ 直接 |
| [enhanced-logging-design](../research/2026-07-24-ekrs-enhanced-logging-design.md) | 三轨分层设计 | 仅在 10a-7 引用（审计事件 schema），完整三轨改造建议 Phase 11+ |
| [enhanced-ui-design](../research/2026-07-24-ekrs-enhanced-ui-design.md) | React/TanStack 生产 UI | 不在 10a 范围；dev_ui 维持 Streamlit |
| [mineru-integration-feasibility](../research/2026-07-24-ekrs-mineru-integration-feasibility.md) | 外部集成否决 | ★ 关键裁决：不引入 mineru-explorer daemon，按需移植设计模式 |
| [mineru-deep-dive-extensions](../research/2026-07-24-mineru-deep-dive-extensions.md) | MinerU 内部实现深读 | 参考但不需要逐字移植 |
| [mineru-explorer-feature-mapping](../research/2026-07-24-mineru-explorer-feature-mapping.md) | MinerU ↔ EKRS 功能映射 | 标识符精确查用例 (T10a-6) 借鉴自此 |
| [phase9-cross-doc-adjudication](../research/2026-07-24-phase9-cross-doc-adjudication.md) | 3 CRITICAL + 4 不一致裁决 | ★ 决策 baseline |

## Iron Rules 合规校验

| Rule | 10a 影响 | 合规方式 |
|---|---|---|
| R1 source_span/block_id/context_window | 无变化（chunk 层） | — |
| R2 求解器纯函数 | 无变化（10a 只动 retriever） | retriever 退化模式 == Phase 9 |
| R3 三闸门 recall→extract→solve | 闸门 1 增强 | 闸门 2/3 不动 |
| R4 Context 优先级 | **仅 FTS=None 时无变化；FTS 有结果时 RRF 重新排序，scope_priority 在 RRF 融合后过滤** | T10a-4 加对比测试：FTS=None vs FTS=[] vs FTS=[distant results] 三档 → 排序差异文档化 |
| R5 仅 entity-overlap KG | 无变化 | — |
| R6 strict=true 禁止推断 | **★ cross-encoder 强制 skip** | 10a 不引入重排；10c 实现时 `skip_rerank=True` if strict |
| R7 scope_path filter | 无变化 | FTS5 WHERE 仅过滤 `status='illegal'` |
| R8 Index 仅过滤非法 | 同上 | — |

## 验证闸门（Phase 10 闭合条件）

- [ ] 现有 Phase 9 测试套件（346+）不退化
- [ ] golden set 50 case 全过 + ≥3 新精确标识符 case
- [ ] retriever 退化模式 (FTS=None) 与 Phase 9 完全一致（byte-level 对比器）；FTS=[]/FTS=distant 三档排序差异文档化（R4）
- [ ] audit.log 回放：10a-7 事件完整可重建 10a 检索路径
- [ ] 内存增量 ≤ 10MB（FTS5），磁盘增量 ≈ Qdrant payload 副本
- [ ] mypy 干净（49/49 文件标准未变）
- [ ] 标签：`phase10` force-move 到最后一个任务闭合 commit（参照 `phase8`/`phase9` precedent）

## 标签策略（Tag strategy）

参照 [`2026-07-23-phase8-scope.md` §Tag force-move](../plans/2026-07-23-phase8-scope.md) 决策：

- **`phase10`**: annotated tag force-move 到 phase 整体闭合 commit（Task 8 / T10a-7 的最终 commit）。代表 *delivered state* 而非 snapshot time。
- **`phase10.1`**: 锁定 T10b-1（chunker 内联重构）完成 commit，作为历史 anchor。**Do not move**。后续 T10a-* 任务若需回溯 chunker 算法时锚定 `phase10.1`。
- **`phase9`**: 留在 phase9 原位置（commit `3bca08a` per memory `phase9-stress-60-of-60-verified.md`）。
- **`phase9.1`**: 留在原位（如有）。

Tag force-move 命令：

```bash
git tag -f -a phase10 HEAD -m "Phase 10: FTS5+RRF + chunker heading-as-unit merge. Force-moved from T10a-7 closure commit. phase9 stays at <phase9-tag>; phase10.1 stays at <T10b-1 closure commit>."
git push --force origin refs/tags/phase10:refs/tags/phase10
```

## Out of scope（明确不做）

- Zvec / Turbovec 存储替换（Phase 11+ 独立工程）
- LLM-based query expansion（移交 Phase 11 via MCP）
- dev_ui 替换为 React 生产 UI（独立 phase）
- 三轨分层日志完整实施（Track 2 `search_trace.log` + Track 3 Prometheus 标签细化）——可作为 Phase 10 后续微任务，不阻塞 10a
- `query_mode` (auto / strict_only / semantic_only) 多模态切换（golden 数据驱动，10c 之后决策）

## 风险 + 缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| FTS5 同步与 Qdrant 写入非原子 | 短暂不一致 | T10a-2 增对账任务（rate-limit 防漂移） + 审计 `fts_synced` 独立回放 |
| BM25 与 bge-m3 维度的 scale 差异 | RRF 排序偏差 | `broad-spectrum-retrieval-port-design §4.3` 已给出归一化公式；T10a-3 单元覆盖 |
| 工程标识符查（型号/标准号）依赖 tokenizer | 召回不稳 | unicode61 base + jieba (CJK) follow-up；T10a-6 用例即是为暴露这个问题 |
| 内存压力（Qdrant + FTS5 + bge-m3 onnx） | OOM | FTS5 仅 ~10MB；bge-m3 已 vendored；当前 RAG 容器资源充足 |

## GSTACK REVIEW REPORT

**Run:** 3 (rev) · **Status:** clean (post-patch)
**Date:** 2026-07-28
**Reviewer:** gstack-review (eng-review pass)
**Patch来源:** Run 2 found 2 项新发现 ([C3-NEW] T10b-3 strict 门控逻辑矛盾 + [M3-NEW] T10b-2 量化触发条件缺失)；2 项已 patch（采纳选项 A：短路全局启用；加入 ≥5% & avg_tokens>max*0.8 双阈值）

### Run 2 (前次验证) — 2 项新发现（均已在 Run 3 内应用 patch）

| # | Severity | Conf | Finding | Patch status |
|---|---|---|---|---|
| [C3-NEW] | CRITICAL(rev) | 7/10 | T10b-3 仍写"门控于 R6 strict=false 模式" — 与 R6 规则语义矛盾（短路是确定性优化，与 strict-mode 推断无关） | ✅ 选项 A：短路逻辑全局启用，strict 模式下与 RRF 结果集一致，只影响性能 |
| [M3-NEW] | MEDIUM(rev) | 6/10 | T10b-2 仅写"候选，待 T10a-6 分析后定夺" — 缺量化决策门槛 → 实施阶段主观决策风险 | ✅ 新增"触发实施条件"段：heading-less 文档占比 ≥ 5% **且** chunk 平均 tokens > max_tokens * 0.8 双重阈值 |

### Run 2 → Run 3 验证已通过的 patch

- ✅ T10b-3 任务描述从"门控于 R6 strict=false 模式"改为"短路逻辑全局启用，不门控于 strict 模式" + 接受标准 (c) 改写为"strict 模式验证：短路仍在 strict 模式下工作，返回集合与 RRF 一致（只影响性能，不影响结果集）"
- ✅ 切片图 line 41 从"候选，待 T10a-6 分析后定夺"改为"候选，触发条件：heading-less 文档 ≥5% 且平均 tokens > max_tokens*0.8，待 T10a-6 分析后定夺"
- ✅ T10b-2 候选段新增"触发实施条件"段，明确 ≥5% 占比 + avg tokens > max*0.8 双重阈值 + 实施沿用 Phase 9 hard cut + 20% lookback + 关闭候选的单边决策路径

### Run 3 净发现 — 无

patch 内容与找出的问题一一对应，无遗漏。Run 2 两项新发现均闭环。

### Run 3 Verdict

**QUALITY: 9.0/10** (up from Run 2 8.5/10)。Run 2 发现的两项问题均以具体 patch 闭环，无新增 CRITICAL/HIGH/MED 项。**计划进入可实施状态**。

Run 1 patch来源存档（reference only; superseded by Run 3）：
**Patch来源:** Run 1 found 7 项 (2 CRIT + 2 HIGH + 2 MED + 1 INFO)；6 项已 patch ([C1][C2][H1][H2][M1][M2])，[I1] commit ref 精确化保留为 info-level backlog (不阻塞)

### Run 1 (initial review) — 7 项发现

| # | Severity | Conf | Finding | Patch status |
|---|---|---|---|---|
| [C1] | CRITICAL | 8/10 | R4 误标"无变化"+ T10a-4 byte-level claim 只覆盖一半 | ✅ R4 矩阵行改写；T10a-4 验证闸门加 3 档对比 |
| [C2] | CRITICAL | 7/10 | T10b-2 时序矛盾；T10b-3 缺任务描述 | ✅ T10b-3 加完整任务行；切片图 T10b-2 改 "候选" |
| [H1] | HIGH | 9/10 | "## GSTACK REVIEW" 是 self-declared placeholder | ✅ 替换为实际 review report（本节） |
| [H2] | HIGH | 7/10 | T10b-1 安全钩子实现位置未规约 | ✅ T10b-1 任务描述加 `_is_safe_join_boundary` 跨 block 强制不合并规约 + 单测用例 |
| [M1] | MEDIUM | 8/10 | Phase 10 tag discipline 未固化 | ✅ 新增"标签策略"小节（`phase10` + `phase10.1`） |
| [M2] | MEDIUM | 6/10 | `chunk_id` 与已有 `block_id` 命名空间冲突 | ✅ T10a-5 任务描述加命名空间共存注 |
| [I1] | INFO | 6/10 | plan line 5 commit reference "~3bca08a" 不精确 | ⏸ info-level backlog；T10b-1 开工前在 commit 时核对确切 SHA |

### Run 2 验证已通过的 patch

- ✅ R4 Iron Rules 矩阵行改写为 "仅 FTS=None 时无变化；FTS 有结果时 RRF 重新排序"
- ✅ 切片图 T10b-2 措辞从 "1 周" 改为 "候选，待 T10a-6 分析后定夺"
- ✅ T10b-3 新增完整任务行（谓词 + 3 项验收 + strict 跳过）
- ✅ T10b-1 任务描述加 `_is_safe_join_boundary` 强制不合并规约
- ✅ 新增"标签策略"小节，含 `phase10` + `phase10.1` 命令样例
- ✅ T10a-5 加 `block_id` vs `chunk_id` 共存注
- ✅ 验证闸门行加 "FTS=[]/FTS=distant 三档排序差异文档化"

### Run 2 新发现 — 无

行扫描 + diff 复核确认无新增 CRITICAL/HIGH 项；剩余 1 项 INFO backlog（[I1] commit reference）不阻塞，可在 T10b-1 开工 commit 时一并精确化。

### Verdict

**QUALITY: 8.5/10** (up from 6.5/10)。全部 2 项 CRITICAL + 2 项 HIGH 已闭环；2 项 MEDIUM 已 patch；1 项 INFO 保留为 backlog。**计划进入可实施状态** —— 启动 T10b-1 之前用 `git show phase9 --no-patch --format="%H"` 核 phase9 SHA 后替换 ~3bca08a 占位即可。

## 开放问题（实施前关闭）

1. ~~**FTS5 文件位置**~~ — **关闭**：10a 放容器内（`/app/rag/fts.sqlite`），bind-mount 是 10b+ 的 trivial change。
2. ~~**审计回放兼容性**~~ — **关闭**：`fts_synced/fts_searched` 仅作为新事件类型，**不**进入 `IngestionOutcome` enum（中间步骤，不是 ingestion 最终态）。见 T10a-7 任务描述。
3. ~~**BM25 词权重调试 flag**~~ — **关闭**：默认关闭（`EKRS_DEBUG_BM25`），golden set 失败时通过环境变量临时开启，不暴露生产 API。
4. ~~**Phase 10b 现状评估触发条件**~~ — **关闭**：合并到 T10a-6 内部，golden set 结果附录里写三段式（评估 / 决策 / 记录）。
5. ~~**FTS 集成测试形式**~~ — **关闭**：tmpfile，与现有 Qdrant mock + 真实两轨测试模式一致。
6. ~~**block_id 双向映射存储形式**~~ — **关闭**：FTS 行 JSON column，不开独立 lookup 表。

---

## 实施前剩余关注点

- **T10a-1 smoketest** 直接用 T10a-6 的 3 个精确标识符查（`A312-TP316` / `GB/T 12459` / `1.6MPa`），验证 tokenizer 不拆碎。
- **T10a-4 byte-level 对比器范围**：仅 `retriever.retrieve()` 返回的 `RetrievalResult`，不含后续求解器。
- **T10a-6 BM25-only recall@1**：3 个工程标识符在纯 BM25 路径下的命中率，作为 10c 重排是否值得做的硬决策依据。
- **跨文档对齐**：研究 doc 的 Track 1 `broad_search_completed` 审计事件需要的 `vector_hits/fts_hits/both_hits` 聚合数字，已通过 T10a-3 的 `FusionStats` 数据类一并返回，retriever 层不重复计算。
