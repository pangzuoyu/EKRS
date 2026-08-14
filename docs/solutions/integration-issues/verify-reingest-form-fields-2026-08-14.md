---
title: "verify_reingest.py 扩展条件 gate — form_fields/column_headers 覆盖 (2026-08-14)"
date: 2026-08-14
category: docs/solutions/integration-issues
module: doc-to-md-verification
problem_type: cross_system_coordination
component: verify_reingest + form_field_extractor + form_table_extractor
related_plan: /home/pangzy/code_project/EKRS/docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md
related_request: /home/pangzy/code_project/doc-to-md/docs/solutions/integration-issues/ekrs-scope-priority-confirmation-2026-08-14.md
related_solution: parse-markdown-form-extractor-integration.md
target_audience: doc-to-md + EKRS development teams
status: investigation_complete_4step_P2_fix_in_flight
severity: P2 — 不阻塞 EKRS T1-T5, 但阻塞 8/20 联调 emit 验证
doc_to_md_commits: e4fbb36 + 023b45d + f9deae5 (Q3 §9.6 form_field/table_extractor)
verify_reingest_commit: 736fd3b (Phase 12 P1, 2026-07-30, 早于新字段 9 天)
---

# verify_reingest.py 扩展条件 gate — form_fields/column_headers 覆盖

> 2026-08-14 调查: `doc-to-md/scripts/verify_reingest.py` 不覆盖 Q3 §9.6 新字段 (`form_fields` / `column_headers`). 4 步 P2 修复方案 (条件 gate + 0% floor + 分布报告) 已设计, 重 ingest 8/14 今晚跑, P3 solution doc 同步写.

---

## 一、调查结论

**scripts/verify_reingest.py 不覆盖 form_fields/column_headers. 三层证据:**

### 1. 代码层证据

| 位置 | 当前实现 | 新字段覆盖 |
|---|---|---|
| `check_one_doc` (line 108-132) | 仅读 `metadata.heading_path` (line 121) + `_scope_path_for_chunk()` (line 122) | ❌ 无 form_fields/column_headers 引用 |
| `evaluate_gates` (line 179-203) | 3 gate: heading_path / scope_path / heading_less | ❌ 无 form/table gate |
| 模块级常量 (line 38-40) | `HEADING_PATH_THRESHOLD` / `SCOPE_PATH_THRESHOLD` / `HEADING_LESS_THRESHOLD` | ❌ 无新阈值 |

### 2. 数据层证据 (2026-08-14 抽样)

| 指标 | 值 |
|---|---|
| Recent (7d) bundles 扫描 | 9 |
| Recent (30d) bundles | 3582 |
| Recent (7d) blocks 总数 | 1843 |
| Blocks with form_fields | 0/1843 |
| Blocks with column_headers | 0/1843 |
| 推荐 15 bundles file_type | 14 × .doc + 1 × .eml |
| 331 全量 file_type | 329 × .doc + 1 × .doc + 1 × .eml |

### 3. 根因

Q3 §9.6 commits (`e4fbb36`+`023b45d`+`f9deae5`) 2026-08-14 今日才 ship, 任何现存 `data.jsonl` 都未经过新代码路径. `verify_reingest.py` 写于 2026-07-30 (Phase 12 P1 commit `736fd3b`), 早于新字段 9 天.

---

## 二、Gate 设计原则 (2026-08-14 决策)

**核心原则**: **条件 gate + 0% floor + 分布报告**, 不设猜测阈值 (30% / 50%).

### 为什么不能用 30% / 50% 硬阈值

`form_fields` / `column_headers` **只在 form-like 文档中存在语义** (LOT/CHECK/STATUS/NCR/form/checklist). 对标准 / 规范 / 报告类文档, 缺失是**正常且预期**的. 直接设全量阈值会产生大量 false-positive (例如 GB/T 标准文档天然无 form_field).

数据不足时设精确阈值是**猜测** — 用 0% floor 捕获"字段完全丢失"的灾难性情况, 用分布报告让 8/20 联调时根据实际数据决定是否需要正式阈值.

### 伪代码

```python
# 关键: 仅对 form-like 文档检查, 非 form 文档不参与
FORM_LIKE_PATTERN = re.compile(
    r'(lot|check|status|list|pta|checklist|ncr|form)', re.IGNORECASE
)

def _is_form_like(filename: str) -> bool:
    return bool(FORM_LIKE_PATTERN.search(filename))

# 对每个 doc 的验证逻辑
if _is_form_like(doc.filename):
    form_fields_ratio = count_non_empty(blocks, 'form_fields') / len(blocks)
    column_headers_ratio = count_non_empty(blocks, 'column_headers') / len(blocks)

    # 极宽松 floor: 仅捕获"字段完全丢失"的灾难性情况
    if form_fields_ratio == 0.0 and len(blocks) >= 5:
        warnings.append(f"{doc.filename}: form_fields 完全缺失")
    if column_headers_ratio == 0.0 and len(blocks) >= 5:
        warnings.append(f"{doc.filename}: column_headers 完全缺失")

    # 输出分布数据供后续调整 (统计报告, 不参与 gate)
    stats.append({
        'filename': doc.filename,
        'n_blocks': len(blocks),
        'form_fields_ratio': form_fields_ratio,
        'column_headers_ratio': column_headers_ratio,
    })
else:
    # 非 form-like 文档: 跳过这些 gate, 不报告
    pass
```

### 关键设计点

| 决策 | 理由 |
|---|---|
| **filter by filename pattern** | form 字段语义只在 form-like 文档中有效. 标准/规范/报告类文档天然无 form_field, 不应触发 gate |
| **floor = 0%** | 仅捕获"字段完全丢失"的灾难性情况 (e.g. extractor 全 fail). 5-block 阈值 (≥5) 避免小 sample 假阳性 |
| **分布报告, 不 pass/fail** | 数据不足时设精确阈值是猜测. 分布报告给 8/20 联调时决定是否需要正式阈值 |
| **`--include-form-fields` 开关 (默认 off)** | 不影响现有 Q5 gate 调用方. opt-in 验证 |
| **emit ratio** 不参与 retry / re-ingest | 仅 warning, 跟 `_warn_missing_heading_paths` (协调项 #6 P3) 同模式 |

---

## 三、修复 4 步 (doc-to-md 侧, P2 修复)

| Step | 行动 | 周期 | 优先级 |
|---|---|---|---|
| 1 | TDD 扩展 `verify_reingest.py` 加 2 条件 gate + `--include-form-fields` 开关 | 0.5 day | P2 |
| 2 | Re-ingest `scripts/long_tail_lot_check_152.json` §recommended_first 15 个 bundle (8/14 今晚低流量窗口, 与 Q5 re-ingest 脚本同路径, idempotent) | 0.5 hour | P2 |
| 3 | 跑扩展 gate check: `python scripts/verify_reingest.py --gate-check --bundle-dir output/text --include-form-fields --sample-size 15` | 0.5 hour | P2 |
| 4 | 写 `docs/solutions/integration-issues/verify-reingest-form-fields-2026-08-14.md` P3 solution doc + mirror 到 EKRS 侧同目录 (本文档) | 0.5 hour | P3 |

**总工作量**: ~1.5 hours (主要 Step 1 单元测试) + 0.5 hour re-ingest + 0.5 hour verify + 0.5 hour doc.

**Re-ingest 命令** (Step 2):
```bash
python main.py <bundle_id> --output-dir output/text/<bundle_id> --force
# 跑 15 次, 每次 1 bundle, 总耗时 ~10 min (Q5 re-ingest 速率)
```

**Verify 命令** (Step 3):
```bash
python scripts/verify_reingest.py --gate-check \
    --bundle-dir output/text --include-form-fields --sample-size 15
# 期望: 15 bundles 全部通过 (0% floor, 极宽松); 分布报告输出 form_fields_ratio / column_headers_ratio
```

---

## 四、影响评估

| 维度 | 影响 |
|---|---|
| **EKRS T1-T5** | **不阻塞**. EKRS schema 改造仅依赖 doc-to-md 已 ship 字段, 不依赖 verify |
| **8/20 联调 emit 验证** | **阻塞**. 修复后三层 gate (code-level / data-level / statistical) 可正交定位 EKRS schema bug vs doc-to-md emit 缺失 |
| **现状 (未修复)** | Q5 旧 3 gate 仍能 PASS, 但 Q3 §9.6 字段验证 = 零信号. 8/20 联调时如发现 RAG 检索质量未提升, 无法区分 EKRS bug vs emit 缺失 |
| **修复后** | 条件 gate + 分布报告可定位: (a) EKRS schema 未声明 (0 chunks hit) (b) IR parser 未透传 (0% in distribution) (c) emit 部分失败 (低 ratio) (d) 正常 emit (高 ratio) |

### EKRS 端跨方协调影响

- **§七 Item 3 75-query recall@10 baseline** 仍按计划跑. 8/20 联调时如果 baseline 出现"form_field 命中但 boost 无效" 模式, 三层 gate 输出可定位具体环节
- **§七 Item 4 清单扫描脚本固化** 仍按 P3 低优, 不影响本修复
- **§七 Item 5 Metadata 模型必含** (EKRS T1 隐藏前置) 跟本修复正交, 不变

---

## 五、Acceptance 验收

| 项 | 状态 |
|---|---|
| Step 1 `verify_reingest.py` 扩展 | ⏳ in-fight (8/14 今晚) |
| Step 2 15 bundles re-ingest | ⏳ 8/14 今晚低流量窗口 |
| Step 3 扩展 gate check | ⏳ 8/15 上午 |
| Step 4 P3 solution doc + mirror | ✅ closed (本文档) |

**8/20 联调前 precondition**: Step 1-3 全部完成, 8/18 前最终确认 (per parent plan §五前置).

---

## 六、相关文件

### doc-to-md 侧 (修复范围)
```
scripts/verify_reingest.py                           # Step 1: 加 2 条件 gate + FLAG
scripts/verify_reingest.py                           # Step 1: 单元测试 test_conditional_form_fields_gate.py
output/text/<bundle_id>/                             # Step 2: re-ingest 15 bundles
docs/solutions/integration-issues/verify-reingest-form-fields-2026-08-14.md  # Step 4: 源 doc
```

### EKRS 侧 (mirror + 跨方记录)
```
docs/solutions/integration-issues/verify-reingest-form-fields-2026-08-14.md  # 本文档 (mirror)
docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md            # §七 Item 1 详细修复
```

### 跨方协调文档
```
doc-to-md: docs/solutions/integration-issues/ekrs-scope-priority-acceptance-2026-08-14.md
EKRS:      docs/solutions/integration-issues/ekrs-scope-priority-reply-2026-08-14.md
```

---

## 七、回复联系人

- doc-to-md 侧 owner: verify_reingest.py 扩展 + re-ingest 脚本
- EKRS 侧 owner: P3 solution doc mirror + 8/20 联调时 gate 输出解读
- 后续跨方协调: 8/20 联调窗口验收, 走 EKRS `docs/solutions/integration-issues/` ↔ doc-to-md 同目录双向 reply
