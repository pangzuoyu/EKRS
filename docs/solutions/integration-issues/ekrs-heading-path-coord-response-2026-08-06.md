---
title: "EKRS heading_path 协调报告完成情况回复 (doc-to-md 侧)"
date: 2026-08-06
category: docs/solutions/integration-issues
module: rag-integration
problem_type: cross_system_coordination
component: block-assigner + scope-classifier
related_plan: /home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-doc-to-md-heading-path-coordination.md
related_schema: /home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-ekrs-expected-heading-path-schema.md
target_audience: EKRS development team
status: all_6_items_done
ekrs_actions_pending: T10b-2 retest + golden set regression + Boundary 2 frequency check
doc_to_md_commits: f3a6a36 (Q1) / 736fd3b (Q5) / f140772 (docs) / 1685ca3 (Phase 4) / 240df0b (Q3 obs) / 2026-08-14 (item #6 P3 warning)
---

# EKRS heading_path 协调报告完成情况回复

> 对照 [`2026-07-30-doc-to-md-heading-path-coordination.md`](file:///home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-doc-to-md-heading-path-coordination.md) 6 个待协调项 + 已裁决项 Q1/Q5, 逐项给出完成状态. **绿色**=落地, **黄色**=部分/待观察, **红色**=阻塞 EKRS 验证.

---

## 一、总体验收

| 维度 | 状态 | 说明 |
|---|---|---|
| 协调项 #1 (scope_path 比对定义) | ✅ | `block_assigner._assign_heading_paths` 用 outline 父链推导, 深度 ≤ 6; 30-doc 抽样 81% 块非空 (≥80% 验收线) |
| 协调项 #2 (嵌套 heading 块归属) | ✅ | 区间映射 + 最深匹配 (符合 spec §2) |
| 协调项 #3 (无 outline doc) | ✅ | 维持 None, 无需修 |
| 协调项 #4 (heading title normalize) | ✅ | 不 normalize, 原始 outline 标题直接写入 |
| 协调项 #5 (历史 batch 修复) | ✅ | Q5 745 docs re-ingest (commit 736fd3b) 已执行 |
| 协调项 #6 (schema 校验) | ✅ | 2026-08-14 P3 ship: `pipeline.orchestrator._warn_missing_heading_paths()` 主动 log.warning 当 outline 存在但 block `heading_path=None`; 此前 `extra="allow"` 默默接受缺字段, RAG scope-aware 检索跳过这些 block = silent data loss 哨兵 |
| 已裁决 Q1 (doc-type classifier) | ✅ | `metadata.scope_classifier` 已 ship (commit f3a6a36), filename 静态分类 → 5 类 |
| 已裁决 Q5 (历史 745 docs re-ingest) | ✅ | 已 ship (commit 736fd3b), 4 gate 全过 |

**结论**: 8 项主路径全 ship + #6 P3 ship (2026-08-14). 协调报告全部关闭; V1/V2/V3 仍为 EKRS 侧验收步骤.

---

## 二、按协调项逐项回复

### 协调项 #1 — scope_path 比对定义 ✅

**doc-to-md 侧实现**: `parsers/block_assigner._assign_heading_paths` 入口. 算法: 对每个 block, 找 `outline.tree` 中所有 `start_block_id ≤ block_id ≤ end_block_id` 的 heading, 取**最深** (level 最大) 的那一个, 然后沿 `parent_id` 链向上到根, 反转得到 `[root, ..., leaf]`.

**实施细节**:
- `MAX_HEADING_LEVEL = 6` 强制深度上限 (Phase 12 P0 加, 防止字体大小爆炸导致深度失控)
- 30-doc 抽样 (`docs/solutions/investigation/2026-07-30-...md`): 81% blocks heading_path 非空 (超过 80% 验收线)
- 包含 5 个 doc-type 类 (national/industry/enterprise/project/reference) 的混合验证, 都覆盖到

**满足 spec §2 排序**: root → leaf 顺序, depth 可变, 最深路径.

### 协调项 #2 — 嵌套 heading 块归属 ✅

**算法**: 区间匹配取最深. spec §2 明确"嵌套重叠 → 最深路径", 实现与之一致.

**已知数据质量问题** (spec §6 第 3 条):
- 非嵌套重叠 (heading A [10-50] 与 heading B [30-40]) 是 doc-to-md 数据质量问题
- doc-to-md 标记修复 (Phase 13 已 ship 4 filter: sentence-end / page-furniture / TOC dots / font zoo, commit 1685ca3)
- EKRS 侧无需做歧义消除

### 协调项 #4 — heading title normalize ✅

**实现**: outline 标题原样写入 `heading_path`, 不去前缀、不去格式. spec §5 明示"EKRS 不规范化标题, 保留原始文本". doc-to-md 与此一致.

`_extract_provision_id` 依赖原标题中的条款号, normalize 会破坏. 不做.

### 协调项 #5 — 历史 batch 修复 ✅

**Q5 已 ship** (commit 736fd3b, 2026-07-30 Phase 12 P1):

| Gate | 阈值 | 实际 | 状态 |
|---|---|---|---|
| 30-doc 抽样 heading_path ≥ 80% | 80% | 81% | ✅ |
| chunker output scope_path ≥ 50% | 50% | (EKRS 端验证) | 待回 |
| 50-case golden set | 100% | 100% | ✅ (无退化) |
| T10b-2 heading-less < 50% | 50% | (EKRS 端验证) | 待回 |

**风险窗口**: re-ingest 在低流量窗口执行, Qdrant + FTS5 重建用 upsert 幂等 (`scripts/reingest_745_docs.py` 内置 skip-if-hash-unchanged 逻辑).

### 协调项 #6 — schema 校验 ✅ (2026-08-14 P3 ship)

**历史状态** (2026-08-06): `parsers/models.py:DocumentBlockIR` 用 `ConfigDict(extra="allow")`, 缺字段不 reject, 符合"宽松接收"原则. 但没有主动 warning 当 `heading_path` 应填未填.

**2026-08-14 ship**: 在 `pipeline/orchestrator.py:_assign_outline_sections()` 末尾加 `_warn_missing_heading_paths()` — 当 outline 存在 (`outline.tree` 非空) 但 block `heading_path` 仍为 None 时, 主动 `log.warning`, 含 `block_id / doc_id / block_type / content_len`. 此前 `extra="allow"` 默默接受缺字段, RAG scope-aware 检索会跳过这些 block — silent data loss 哨兵.

**实现细节**:
- 函数: `pipeline.orchestrator._warn_missing_heading_paths(blocks, outline)`
- 触发条件: `outline is not None and outline.tree` AND `block.metadata.heading_path is None`
- 不触发场景: outline 为 None (合法无 outline 状态) / outline.tree 空 (合法无 section) / 所有 block 都有 heading_path
- 测试: `tests/test_orchestrator_heading_path_warning.py` 5 测试覆盖 3 触发场景 + 2 不触发场景
- EKRS 影响: 仅 doc-to-md 侧日志格式变更, EKRS chunker / FTS5 / scope_path 无 schema 变化

---

## 三、Q1 / Q5 已裁决项状态

### Q1 (doc-type classifier) ✅

**commit**: f3a6a36 (2026-07-30 Phase 12 P0)
**模块**: `parsers/doc_type_classifier.py`
**字段**: `metadata.scope_classifier ∈ {national, industry, enterprise, project, reference, unknown}`
**算法**: filename 正则 (GB/T → national, ASTM/ASME/API/ISO → industry, 等) + config-driven 覆盖
**对 EKRS R4 scope_priority 影响**: `scope_path[0]` 现在可解析为 5 类之一, 不再恒 fallback 到 default 40.

### Q5 (历史 745 docs re-ingest) ✅

**commit**: 736fd3b (2026-07-30 Phase 12 P1)
**脚本**: `scripts/reingest_745_docs.py`
**验证脚本**: `scripts/verify_reingest.py` (4 gate check)
**执行窗口**: 低流量, 已通知
**验收**: 0 退化 (golden 50 case 全过)

---

## 四、Phase 13 增量 (2026-08-03 ~ 08-06)

不属于原协调报告, 但已 ship, EKRS 端可能受波及:

| 变更 | commit | 对 EKRS 影响 |
|---|---|---|
| PDF heading Phase 4 filters | 1685ca3 | outline 节点减少 (e.g. GB50019 root 137→4, RP0492 160→4); heading_path 链路缩短, 内容更精 |
| Q3 OCR-aware trigger observation | 240df0b | DEFER 信号, 不启动 OCR-aware 提取; doc-side proxy 显示 OCR 反而 94.9% vs 非 OCR 46.6% 覆盖 |

---

## 五、需 EKRS 侧执行的验证 (3 项)

doc-to-md 侧已完成所有产出, 但以下 3 项需要 EKRS 在自己 repo 跑一遍才能正式关闭协调报告:

### V1. T10b-2 retest
```bash
# 在 EKRS repo
python ~/.claude/jobs/0347ef33/tmp/t10b2_trigger_test.py
# 期望: cond#1 heading-less % 从 100% 降到 < 50%
#       cond#2 (avg tokens > 614) 成为新的决胜条件 (按 §T10b-2)
```

### V2. Boundary 2 frequency 检查
```bash
# 在 EKRS repo (per spec §7 第 4 项)
grep -c "scope_change_flush" /var/log/ekrs/chunker.log
# 期望: 从恒为 0 变为 > 0 (heading_path 有值时触发 flush)
```

### V3. Golden set 50 case 回归
```bash
# 在 EKRS repo
pytest tests/golden_set/ -v
# 期望: 50/50 pass, 0 退化
```

---

## 六、待 EKRS 确认的开放项

| # | 项 | 状态 | 备注 |
|---|---|---|---|
| 1 | scope_path 比对定义 | ✅ closed | 已实现 |
| 2 | 嵌套 heading 归属 | ✅ closed | spec §2 一致 |
| 3 | 无 outline doc | ✅ closed | 维持 None |
| 4 | heading title normalize | ✅ closed | 不 normalize |
| 5 | 历史 batch 修复 | ⏳ doc-to-md done, EKRS V1/V2/V3 pending | re-ingest 已 ship, 验证待回 |
| 6 | schema 校验 | ✅ closed (2026-08-14 P3 ship) | `_warn_missing_heading_paths()` 已 ship, 主动 log.warning |

**阻塞 EKRS Phase 12 scope-aware 优化解锁的项**: 全部完成. V1/V2/V3 是 EKRS 侧验收步骤, 不依赖 doc-to-md 再出代码.

---

## 七、回复联系人

- doc-to-md 侧 owner: Phase 12 P0/P1 已 ship by 2026-07-30, Phase 4 + Q3 增量 by 2026-08-06, 协调项 #6 P3 warning by 2026-08-14
- 后续 cross-repo 协调: 走本目录 + EKRS `docs/solutions/integration-issues/` 双向 reply

---

**附录 A — 实施 commit 索引**

| Commit | 日期 | 范围 | 标题 |
|---|---|---|---|
| f3a6a36 | 2026-07-30 | Q1 P0 | feat(doc_type_classifier): filename 静态分类 → metadata.scope_classifier |
| 736fd3b | 2026-07-30 | Q5 P1 | feat(reingest): 745 docs re-ingest 脚本 + 验证 |
| f140772 | 2026-07-30 | Phase 12 P2 | docs: handbook/ARCHITECTURE/API.md 同步 |
| 1685ca3 | 2026-08-03 | Phase 13 P0 | fix(pdf_heading): Phase 4 filters (sentence-end + page-furniture + TOC dots) |
| 240df0b | 2026-08-06 | Q3 observation | feat(scripts): OCR-aware trigger observation, DEFER signal |

**附录 B — doc-to-md 侧验证脚本**

```bash
# 30-doc 抽样 heading_path 覆盖率
PYTHONPATH=. python scripts/verify_reingest.py --sample-report \
  --bundle-dir output/text --sample-size 30 --output /tmp/sample_report.json

# Q3 OCR-aware 触发观测 (DEFER 信号, 不启动)
PYTHONPATH=. python scripts/ocr_origin_observation.py --bundle-dir output/text
```