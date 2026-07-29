# doc-to-md → EKRS 协调报告: `metadata.heading_path` 未传播

**Date**: 2026-07-30
**From**: EKRS Phase 10 T10b-2 trigger test
**To**: doc-to-md 侧
**Severity**: HIGH (阻塞 R4 / R7 实际效用)

## EKRS-side schema contract (DRAFT, pending doc-to-md sign-off)

详见 companion spec: [`2026-07-30-ekrs-expected-heading-path-schema.md`](2026-07-30-ekrs-expected-heading-path-schema.md)

要点:
- **Type**: `Optional[List[str]]` — 已存在于 `shared/ekrs_shared/models.py:27`
- **Semantics**: heading hierarchy ONLY (root → leaf). doc-type classifier 是另一字段, 不归 `heading_path` 管 (§6 explicit warning)
- **Ordering**: root first, leaf last
- **Boundary case**: 嵌套 region 用 deepest enclosing heading
- **Normalization**: doc-to-md 不 normalize, EKRS consumer 自己处理
- **Verification**: 30-doc sample 期望 ≥80% blocks non-empty (现 0.1%); 50-case golden set 必须继续 pass

## TL;DR

`doc-to-md/output/text/<doc_id>/data.jsonl` 中所有 block 的 `metadata.heading_path` 字段**几乎全部为 `None`** (99.9%, 抽样 1556 blocks / 20 docs)，即便 `outline.json` 显示该 doc 有完整多级 heading 树 (90% doc, 19–2070 headings)。

EKRS chunker 依赖 `metadata.heading_path` 实现:
- **R4 scope priority** (国家/行业/企业/项目/参考五级)
- **R7 scope_path filter** (multi-branch 输出)
- **T10a-4 RRF 排序** (scope_priority 在 RRF 融合后过滤)
- **T10b-2 触发条件** (heading-less 文档占比)

`heading_path=None` → chunker fallback 到 `[]` → 所有 doc 被当成"无 heading" → scope-aware 检索**实际零效果**。

## 证据

### 抽样 1: heading_path 分布 (20 docs, 1556 blocks)

```
heading_path=None:           99.9%
heading_path=[] (empty list): 0.1%
heading_path non-empty:       0.0%
```

### 抽样 2: outline vs heading_path (30 docs)

```
docs with non-empty outline tree:   27/30 (90%)
docs with heading_path populated
  in ANY block (data.jsonl):        0/30  (0%)
```

27/30 docs 在 `outline.json` 有完整 heading 树 (depth ≥ 4，e.g. `ARTICLE > LEAK > TESTING > STANDARDS > ...`)，但 `data.jsonl` 的每个 block metadata.heading_path 都是 None。

### 抽样 3: 完整文档样本

Doc `0031b0753d6eb01c` (`ASME SEC V B SE-432 standard for leak test.pdf`):
- `outline.json`: 5-level heading tree (`ARTICLE` → `LEAK` → `TESTING` → `STANDARDS` → 推荐指南)
- `data.jsonl` block 0 (`block_id=4adea7c9-...`): `metadata.heading_path = None`
- `data.jsonl` block 1 (`block_id=340af4bd-...`): `metadata.heading_path = None`

### 可复现脚本

```bash
python3 - <<'EOF'
import json, os, random
CORPUS = "/home/pangzy/code_project/doc-to-md/output/text"
random.seed(42)
for d in random.sample(sorted(os.listdir(CORPUS)), 20):
    jf = f"{CORPUS}/{d}/data.jsonl"
    if not os.path.exists(jf): continue
    with open(jf) as f:
        for line in f:
            rec = json.loads(line)
            hp = rec.get("metadata", {}).get("heading_path")
            if hp: print(f"{d}  {rec['block_id']}  hp={hp}")
EOF
```

## 对 EKRS 的影响

| Iron Rule | 实际效果 |
|-----------|---------|
| R4 scope_priority | 所有 chunk scope_path = []，priority 全部退化到 default，无差异化 |
| R7 scope_path filter | `WHERE scope_path LIKE 'national/%'` 永远 0 hit |
| T10a-4 RRF | scope_priority 在融合后过滤的逻辑分支永不触发 |
| T10b-2 trigger | heading-less = 100% (数据问题，不是真实信号) |

**T10b-2 trigger test (2026-07-30)**:
- cond#1 heading-less %: MET (100%)
- cond#2 avg tokens > 614: NOT MET (mean 484.6)
- **DECISION: CLOSE CANDIDATE** — 结论本身正确 (Phase 10 chunker 通过 T10b-1 `_split_large_block` 已守住预算)，但 cond#1 触发是 false-positive，掩盖了 IR 数据缺口

## 建议修复 (doc-to-md 侧)

在每个 block 写入 `data.jsonl` 时，从 `outline.json` 推导并填充 `metadata.heading_path`。

最小实现草案:

```python
# doc-to-md 端: data.jsonl 写入前
def build_heading_path(block, outline_tree):
    """Map a block to its heading path via outline tree position.
    
    outline_tree: list of {id, title, level, parent_id, start_block_id, end_block_id, ...}
    Returns list[str] of heading titles from root to leaf, or [] if no enclosing heading.
    """
    block_id = block.block_id
    for heading in outline_tree:
        if heading["start_block_id"] <= block_id <= heading["end_block_id"]:
            # walk up the parent chain to build path
            path = []
            cur = heading
            while cur is not None:
                path.append(cur["title"])
                cur = next((h for h in outline_tree if h["id"] == cur["parent_id"]), None)
            return list(reversed(path))
    return []  # block not enclosed by any heading
```

期望产出: `data.jsonl` 每个 block 的 `metadata.heading_path` 形如 `["ARTICLE 27", "LEAK TESTING STANDARDS"]` (深度按 outline tree 决定)。

## 验证期望

修复后, EKRS 侧 (Phase 12+) 应能验证:

1. **heading_path 分布**: 抽样 30 docs，预期 ≥ 80% blocks 的 heading_path 非空 (跟 outline 覆盖率对齐)
2. **chunk scope_path 非空率**: chunker 输出 chunks 中 scope_path=[] 的占比从 ~100% 降到 ~30% 以下 (取决于 doc 结构)
3. **golden set 影响**: 50 case 应保持全过 (scope_path 是额外信号, 不是 hard requirement); 验证 R4 priority ordering 真实生效
4. **T10b-2 trigger re-test**: heading-less % 应该从 100% 降到 < 50%, 触发条件可能从 MET → NOT MET (更真实信号)

## 待协调项

| # | 项 | 优先级 | 提议 owner |
|---|----|--------|------------|
| 1 | **scope_path 比对定义**: EKRS 期望 `[root, level1, ..., leaf]` (深度可变)；doc-to-md 当前 `outline.json` 只有 tree 节点, 需确认推导语义一致 | P0 | doc-to-md |
| 2 | **block 在多个 heading 区间时的归属**: 若 block_id 落在 heading A 和 heading B 的 [start,end] 重叠区 (e.g. nested heading), 用最深 (level 最大) 还是浅路径? | P1 | doc-to-md + EKRS |
| 3 | **outline 不存在的 doc**: 当前抽样 10% docs 无 outline tree, heading_path 全部为 `[]` 是预期行为, 不需要修 | P2 (no action) | — |
| 4 | **heading title normalize**: outline 中 `ARTICLE 27` vs `## ARTICLE 27` vs `## ARTICLE 27 LEAK TESTING STANDARDS` (full text); chunker 期望 normalized title (去掉编号前缀?) | P1 | doc-to-md + EKRS |
| 5 | **历史 batch 修复策略**: 已入库的 745 docs 是否需要 re-process? 还是新 batch 修即可 (历史 chunk 已在 Qdrant 里, 重处理需触发 T8-3b ingestion smoke) | P2 | EKRS (决定 re-ingest 策略) |
| 6 | **schema 校验**: doc-to-md 端 `DocumentBlockIR` schema 应将 `heading_path: Optional[List[str]]` 标记为 "应该被填充" (warning if None for text blocks where outline exists) | P3 | doc-to-md |

## 已裁决项 (user 2026-07-30)

### Q1 (doc-type classifier 字段): 方案 B — ingest 时基于 source filename 推断

不新增 `metadata.scope_classifier` 字段。在 pipeline 增加 `_classify_doc_type(source_filename: str) -> str` 静态函数 + 可配置映射规则 (e.g. `r"^GB[/-T]"` → national_standard, `r"^SA-"` → project_spec)。映射规则可独立调整, 不重新部署 doc-to-md。

执行时机: heading_path 修复验证通过后, Phase 12 单独 task (不捆绑本轮修复)。

### Q5 (历史 745 docs re-ingest): 必须执行, 三段式 (修复 → 验证 → 决策 → 执行)

**触发条件 (4 验证标准全过才执行)**:
1. 30-doc sample: ≥80% blocks non-empty heading_path
2. chunker output: ≥50% chunks `scope_path != []`
3. 50-case golden set: 全过
4. T10b-2 trigger re-test: cond#1 heading-less % 从 100% 降到 < 50%

**执行方式**: 批量 re-ingest 脚本按 doc_hash 列表逐文档重新摄取, 触发 Qdrant 重建 (upsert 覆盖) + FTS re-sync。

**风险窗口**: re-ingest 期间 Qdrant + FTS5 索引变更, 外部查询可能读到中间状态。**低流量窗口执行**, 提前通过 CHANGELOG / 手册通知。

**验收**: re-ingest 后跑 golden set 全量 + T10b-2 重测, 0 退化。

执行时机: heading_path 修复 + Q1 doc-type classifier 落地后, Phase 12 独立步骤。

## 触发本报告的工作

- Plan: `docs/superpowers/plans/2026-07-28-phase10-broad-spectrum-retrieval.md` §T10b-2
- Test harness: `~/.claude/jobs/0347ef33/tmp/t10b2_trigger_test.py` (reusable for re-test after fix)
- Memory: `~/.claude/projects/.../memory/phase10-t10b2-closed.md`

## 期望回复时间

本协调项 #1, #2, #4 是 doc-to-md schema/语义决策, 阻塞 EKRS Phase 12 后续 scope-aware 优化 (R4 / R7 实际效用解锁). 期望 doc-to-md 侧在下一 sprint 评估 + 给出 outline→heading_path 映射实现方案.

EKRS 侧可在收到修复后 (预计 Phase 12 kickoff 后第 2 个 task 起) 跑 re-test + golden regression.