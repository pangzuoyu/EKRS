---
title: "Phase 12 Q3 §9.6 last-mile — form_field/column_header → R4 boost"
date: 2026-08-14
category: docs/superpowers/plans
module: rag-integration
phase: 12
parent_plan: /home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-doc-to-md-heading-path-coordination.md
status: closed_建议1_accepted_implementation_scheduled
target_window: 2026-08-18 ~ 2026-08-20
ekrs_owner: Phase 12 follow-up team
doc_to_md_owner: Phase 12 P0/P1 + Q3 §9.6 team
---

# Phase 12 Q3 §9.6 last-mile — form_field/column_header → R4 boost

> Phase 12 Q3 §9.6 last-mile 跨方协调记录. doc-to-md **接受建议 1** (2026-08-14): EKRS 直接消费 `metadata.form_fields` + `metadata.column_headers`, doc-to-md 端 **0 改动**. 实施窗口 8/18-8/19, 联调 8/20.

---

## 一、背景

doc-to-md Q3 §9.6 已 ship 两个新 metadata 字段:

| 字段 | 来源 | doc-to-md commit |
|---|---|---|
| `metadata.form_fields` | form_field_extractor LABEL:VALUE 提取 | `e4fbb36` + `023b45d` |
| `metadata.column_headers` | form_table_extractor 列头提取 | `f9deae5` + `a46f348` |

LOT/CHECK/STATUS 等 form-like 文档含大量结构化字段, 但 EKRS R4 scope-aware 检索当前**未消费这两个字段**. 本计划让 R4 能利用 form 语义提升优先级:

- **目标场景 1**: 用户查询 `"Lot 49"` → 命中含 `SYSTEM NO: Lot 49` 的 NCR 报告
- **目标场景 2**: 用户查询 `"A105 material"` → 命中含 `column_header=A105` 的 LOT 表

---

## 二、跨方协调闭环 (2026-08-14)

**协调链** (完整 3 轮往返):

```
doc-to-md                                    EKRS
│                                            │
│  ekrs-scope-priority-confirmation ──────►  Q1-Q4 答复 + §三 选项 C + 建议 1
│  (Q3 §9.6 last-mile 请求)                    ekrs-scope-priority-reply
│                                             (commit 0d34d8e + cfabd28)
│  ◄──── 接受建议 1 + 152 清单 ────  §五 6 项内部裁决
│  ekrs-scope-priority-acceptance
│  + 331 zero-cov + 15 recommended
│                                             §六 Acceptance ✅
│                                             (commit 1cc1a7f)
└─ 8/20 联调窗口                              └─ EKRS T1-T5 (8/18-8/19)
```

**关键发现** (Q1-Q4 答复):

- **Q1**: heading_path 是 `(a) 扁平 list[str]` — chunker / FTS5 / Qdrant / retriever ALL 消费 list[str]; 不可变契约
- **Q2**: `scope_priority` 字段 EKRS 无 schema; `Priority` IntEnum + `_SCOPE_PRIORITY_MAP` (retriever.py:26-28) 从 `scope_path[0]` 派生 (计算非存储). `metadata.scope_classifier` (doc-to-md emit f3a6a36) EKRS **未消费** (Phase 12 方案 B = 内部 `_classify_doc_type()`)
- **Q3**: R4 索引 `(c) 混合` — 全字符串进 FTS5 keyword, boost 仅作用于 `scope_path[0]`. **选项 B (prefix injection) 破坏 scope_path[0] doc-type 权重 = anti-pattern**
- **Q4**: heading_path 保持 `list[str]` 不动, 不引入结构化对象

**裁决**: 选项 C + **建议 1 强推荐** — EKRS 直接消费已有 `metadata.form_fields` / `metadata.column_headers`, doc-to-md **0 改动**. 建议 2 (新增 scope_priority 字段) **不采用**.

**6 项 EKRS 内部裁决** (doc-to-md 全接受, 无异议):

| 项 | 裁决 |
|---|---|
| Item 1 scope_classifier 整合 | 维持 EKRS 内部 `_classify_doc_type` 推断 (Phase 12 方案 B) |
| Item 2 weight 默认值 | 硬编码 `heading=1.0` / `form_field=0.9` / `column_header=0.7` (config-driven = 过度设计) |
| Item 3 T10b-3 短路 parity | form_field boost **不生效**, 这是正确行为 (短路在 RRF 之前; 精确匹配 = 最强信号; 多命中排序由确定性信号 block_order / doc_hash 决定) |
| Item 4 R6 strict mode parity | form_field boost **不阻断** (与 doc-type 权重同属确定性 scope 计算, 非推断) |
| Item 5 抽样集 | doc-to-md 提供 152 清单 → 实际交付 331 zero-cov + 15 recommended-first (full corpus 3582 / 1500 sample = 2.4× ratio, 9.2% vs 10.1%) |
| Item 6 heading_path 链路缩短 (Phase 13 PDF filters) | 等 V3 golden set 回归数据量化, 不提前动作 |

---

## 三、实施计划: T1-T5 (2026-08-18 ~ 2026-08-19)

| ID | 内容 | 周期 | 文件 |
|---|---|---|---|
| **T1** | Chunk 模型新增 `form_fields: Optional[list]` / `column_headers: Optional[list]` Optional 字段 | 0.5 day | `shared/ekrs_shared/models.py:Chunk` |
| **T2** | chunker 透传 (block → chunk) + Qdrant payload 写入 `form_fields` / `column_headers` 数组字段 | 1 day | `rag/ekrs_rag/ingestion/chunker.py` + `rag/ekrs_rag/retrieval/qdrant_client.py` |
| **T3** | FTS5 schema 迁移 (新增 `form_fields TEXT` / `column_headers TEXT` 列) + 索引扩展 (key + value + header 拼接) | 1 day | `rag/ekrs_rag/retrieval/fts_manager.py` |
| **T4** | `retriever._scope_priority()` 扩展: 读取新字段, 按 type 加权 (`max(base, weight)` 叠加, 避免堆叠放大) | 0.5 day | `rag/ekrs_rag/retrieval/retriever.py:_scope_priority()` |
| **T5** | 测试 (单元: 新字段 round-trip) + golden set 50 case 回归 + Boundary 2 frequency check | 1 day | `tests/unit/` + `tests/golden_set/` |

**合计 4 天**, 8/18-8/19 完成.

---

## 四、权重公式设计

```python
# retriever._scope_priority(chunk, form_fields, column_headers) -> float
def _scope_priority(chunk, form_fields=None, column_headers=None):
    # 基础: 现有 _SCOPE_PRIORITY_MAP 从 scope_path[0] 派生
    base = _legacy_scope_priority_from_path(chunk.scope_path)

    # 叠加: form_field / column_header 仅作 max 提升 (不累加)
    if form_fields:
        base = max(base, FORM_FIELD_WEIGHT)   # 0.9
    if column_headers:
        base = max(base, COLUMN_HEADER_WEIGHT)  # 0.7
    return base
```

**关键决策**:
- `max()` 而非 `+=` — 避免堆叠放大 (e.g. 同时含 form_field + column_header + heading 时, 不该 = 1.0 + 0.9 + 0.7)
- `form_field > column_header` — form_field 是用户直接锚点 (Lot 49 → SYSTEM NO=Lot 49), column_header 是语义索引 (A105 出现在多个 row)
- 硬编码常量 (`FORM_FIELD_WEIGHT = 0.9`, `COLUMN_HEADER_WEIGHT = 0.7`) — 不引入 config 层

---

## 五、8/20 联调计划

| 时段 | 行动方 | 内容 |
|---|---|---|
| 8/20 上午 | doc-to-md | 跑 `scripts/verify_reingest.py` LOT/CHECK 抽样 (从 `long_tail_lot_check_152.json` 推荐 15 选 10-15) 验证 form_fields/column_headers 经 EKRS 全链路 |
| 8/20 上午 | EKRS | 验证 FTS5/Qdrant drift (T10a-2 `ConsistencyChecker` 检测) — form_fields/column_headers 双写一致性 |
| 8/20 下午 | 双方 | 端到端抽样 recall@10 baseline 对比 (form_field boost 启用前后) |
| 8/20 下午 | EKRS | golden set 50 case 回归 (V3) — 量化 heading_path 链路缩短 (Phase 13 PDF filters) 对 R4 检索精度的影响 |

---

## 六、Bundle 抽样资源

**清单文件**: `doc-to-md/scripts/long_tail_lot_check_152.json` (72KB, git tracked)

**结构**:
```json
{
  "summary": {
    "total_candidates": 331,
    "by_doc_type": {"lot": 321, "list": 3, "check": 5, "status": 2},
    "recommended_sample_size": 15,
    "filter_criteria": [
      "filename matches (lot|check|status|list|pta|checklist)",
      "bundle heading_path coverage == 0% (per data.jsonl)"
    ]
  },
  "recommended_first": [15 个高优先级样本 (lot=8 / check=3 / status=2 / list=2)],
  "full_list": [331 全部 zero-cov long-tail bundle]
}
```

**单条格式**:
```json
{
  "bundle_id": "000151778ca35475",
  "filename": "7-Lot00 NCR Status Report.doc",
  "doc_type": "lot",
  "n_blocks": 7,
  "coverage_pct": 0.0,
  "sample_priority": "high"   // 仅 recommended_first 有此字段
}
```

**抽样建议**:
- 从 `recommended_first` 15 个起步 (覆盖 4 doc-type)
- 优先选 `n_blocks >= 5` 的 bundle (block 多 → form_field / column_header 期望产出更显著)
- 若发现新模式 (e.g. 部分表单识别失败), 从 `full_list` 按 filename 关键词额外取 5-10 个

---

## 七、未解决问题 (8/20 联调执行)

| # | 项 | 优先级 | 来源 |
|---|---|---|---|
| 1 | Q5 re-ingest (commit `736fd3b`) verify script 是否覆盖新 form_fields/column_headers 字段? 需检查 `scripts/verify_reingest.py` 是否更新 | P1 | doc-to-md §七 |
| 2 | 协调项 #6 P3 ship (commit `58cc527`) 后, 长尾 bundle 首次 re-ingest 触发 warning log 量未测. 若超日志可读阈值需 rate-limit | P2 | doc-to-md §七 |
| 3 | 端到端抽样 recall@10 baseline 对比 — form_field boost 启用前后真实效果量化 | P1 | doc-to-md §七 |
| 4 | 清单扫描脚本固化 (临时 `/tmp/scan_long_tail.py` + `/tmp/scan_zero_cov.py` 进 `scripts/` 目录) | P3 低优 | doc-to-md §七 |

---

## 八、相关文件

### EKRS 侧 (本计划实施范围)
```
shared/ekrs_shared/models.py:Chunk                    # T1
rag/ekrs_rag/ingestion/chunker.py:_get_scope_path     # T2
rag/ekrs_rag/retrieval/qdrant_client.py:upsert_chunks  # T2
rag/ekrs_rag/retrieval/fts_manager.py                 # T3
rag/ekrs_rag/retrieval/retriever.py:_scope_priority   # T4
rag/ekrs_rag/retrieval/retriever.py:_rank_by_scope    # T4 (权重公式接入)
tests/unit/test_chunker.py + test_retriever.py        # T5
tests/golden_set/                                     # T5
```

### doc-to-md 侧 (本计划不改)
```
parsers/form_field_extractor.py             # 已 ship, 不动
parsers/form_table_extractor.py             # 已 ship, 不动
scripts/verify_reingest.py                  # §七 项 1 需检查更新
scripts/long_tail_lot_check_152.json        # 抽样源
```

### 跨方协调文档
```
doc-to-md: docs/solutions/integration-issues/ekrs-scope-priority-confirmation-2026-08-14.md
doc-to-md: docs/solutions/integration-issues/ekrs-scope-priority-acceptance-2026-08-14.md
EKRS:      docs/solutions/integration-issues/ekrs-scope-priority-reply-2026-08-14.md
```

---

## 九、未解决问题 (协调层)

1. ✅ **所有跨方协调问题已关闭** (2026-08-14): 建议 1 接受, 152 清单交付, 6 项内部裁决被 doc-to-md 接受
2. **§七 4 项** 转入 8/20 联调执行阶段

---

## 十、变更日志

- 2026-08-14: 计划创建 (协调闭环记录 + T1-T5 实施步骤 + 8/20 联调计划)
- 2026-08-14: doc-to-md 接受建议 1 (commit `9e88914` mirror in doc-to-md repo)
- 2026-08-14: EKRS 内部裁决 6 项记录 (commit `cfabd28`)
- 2026-08-14: reply §六 Acceptance ✅ (commit `1cc1a7f`)

---

**附录 A — 实施 commit 索引**

| Commit (预期) | 日期 | 范围 | 标题 |
|---|---|---|---|
| (TBD) | 2026-08-18 | T1 | feat(models): Chunk 新增 form_fields / column_headers Optional 字段 |
| (TBD) | 2026-08-18 ~ 19 | T2 | feat(ingestion): chunker + Qdrant 透传 form_fields/column_headers |
| (TBD) | 2026-08-19 | T3 | feat(fts): schema 迁移 + form_fields/column_headers 索引扩展 |
| (TBD) | 2026-08-19 | T4 | feat(retriever): _scope_priority 扩展 form_field/column_header weight |
| (TBD) | 2026-08-19 | T5 | test: 新字段 round-trip + golden 50 回归 + Boundary 2 frequency |
| (TBD) | 2026-08-20 | 联调 | docs(coordination): Phase 12 form_field r4 boost 联调完成 |

**附录 B — 相关跨方 commit (doc-to-md)**

| Commit | 日期 | 范围 |
|---|---|---|
| `e4fbb36` | 2026-08-14 | feat(form_field_extractor): LABEL:VALUE 提取 |
| `023b45d` | 2026-08-14 | feat(pipeline): form_field_extractor 集成进 docx parser |
| `f9deae5` | 2026-08-14 | feat(form_table_extractor): 列头提取 |
| `a46f348` | 2026-08-14 | (form_table_extractor 后续 commit, 推测) |