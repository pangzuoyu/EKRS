---
title: "doc-to-md 接受 EKRS 建议 1 — form_fields/column_headers 直接消费 (Q3 §9.6 last mile)"
date: 2026-08-14
category: docs/solutions/integration-issues
module: rag-integration
problem_type: cross_system_coordination
component: block-assigner + scope-classifier + rag-bridge
related_plan: /home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-doc-to-md-heading-path-coordination.md
related_request: ekrs-scope-priority-confirmation-2026-08-14.md
related_reply: ekrs-scope-priority-reply-2026-08-14.md
related_solution: parse-markdown-form-extractor-integration.md
target_audience: EKRS development team
status: accepted_建议1_bundle_list_delivered
doc_to_md_commit_required: none (Q3 §9.6 已 ship 全部必要字段)
ekrs_pending_actions: T1-T5 (2026-08-18 ~ 08-20 联调窗口)
pending_doc_to_md_input: []
bundle_list_artifact: scripts/long_tail_lot_check_152.json (331 zero-cov + 15 recommended-first)
bundle_list_delivered: 2026-08-14
---

# doc-to-md 接受 EKRS 建议 1 (Q3 §9.6 last mile)

> 对 [`ekrs-scope-priority-reply-2026-08-14.md`](ekrs-scope-priority-reply-2026-08-14.md) §三 裁决与建议, doc-to-md 侧正式答复.

---

## 一、§四 行动项答复

**doc-to-md 接受 EKRS 建议 1** — EKRS 在 retriever / FTS5 / Qdrant 直接消费 `metadata.form_fields` + `metadata.column_headers`, doc-to-md 端 **0 改动**.

**建议 2 不采用**: `scope_priority` 字段不引入, heading_path 契约 (`list[str]`) 不动摇, 不重复打包生成 (form_fields 既在 form_fields 里又在 scope_priority 里是冗余).

---

## 二、doc-to-md 端工作归零说明

原 [`ekrs-scope-priority-confirmation-2026-08-14.md`](ekrs-scope-priority-confirmation-2026-08-14.md) §四 Step 3-5 (设计 + 实施 + E2E) **全部取消**. 已 ship 字段:

| 字段 | 来源 | doc-to-md commit |
|---|---|---|
| `metadata.form_fields` | Q3 §9.6 form_field_extractor | `e4fbb36` + `023b45d` (2026-08-14) |
| `metadata.column_headers` | Q3 §9.6 form_table_extractor | `f9deae5` + `a46f348` (2026-08-14) |
| `metadata.heading_path` | 协调项 #1 outline 父链推导 | `heading-path-ekrs-fix` memory (2026-07-30) |
| `metadata.scope_classifier` | Q1 P0 filename 静态分类 | `f3a6a36` (2026-07-30, EKRS 未消费) |

EKRS 端只需按 reply §三 建议 1 的实现路径 (T1-T5) 扩展消费即可.

---

## 三、152 长尾 bundle 清单 (item 5 答复) ✅ 已交付

**状态**: 已交付 2026-08-14 (提前 1 天).

**清单文件**: [`scripts/long_tail_lot_check_152.json`](../../../scripts/long_tail_lot_check_152.json) (72KB, git tracked)

**结构**:
```json
{
  "summary": {
    "total_candidates": 331,
    "by_doc_type": {"lot": 321, "list": 3, "check": 5, "status": 2},
    "recommended_sample_size": 15,
    "derivation": "Q3 §9.6 zero-cov long-tail (full corpus 3582 vs Q3 sample 1500)",
    "filter_criteria": [
      "filename matches (lot|check|status|list|pta|checklist)",
      "bundle heading_path coverage == 0% (per data.jsonl)"
    ],
    "ekrs_action": "EKRS 选 10-15 个作为跨方验证样本, 抽样标准见 acceptance doc §三"
  },
  "recommended_first": [15 个高优先级样本 (lot=8 / check=3 / status=2 / list=2)],
  "full_list": [331 全部 zero-cov long-tail bundle]
}
```

**数量差异说明** (331 vs Q3 提到的 152):
- Q3 调查 doc `q3-subgroup-non-ocr-longtail-2026-08-14.md` §Finding 2 基于 **1500 bundle 抽样**, 推导 152 个长尾 (146 zero + 6 high-cov 同模式)
- 本次扫描基于**完整 corpus 3582 bundle** (3582 / 1500 ≈ 2.4×) + 严格 zero-cov 过滤 → 331 个
- 比例一致: 331 / 3582 = 9.2% vs Q3 抽样 152 / 1500 ≈ 10.1% (一致, 在合理抽样误差内)
- doc-to-md 决定:**提供全部 331 个**而不是收缩到 152, 让 EKRS 有更宽抽样池子; EKRS 仍只选 10-15 个 (按 §五问题 5)

**单条格式** (符合 Q3 要求):
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

**派生路径**:
1. 扫描 `output/text/*/full.md` 收集 filename 含 `(lot|check|status|list|pta|checklist)` 的 bundle → 367 candidates
2. 对每个 bundle 读 `output/text/<id>/data.jsonl`, 计算 `n_blocks_with_heading_path / total_n_blocks` (heading_path coverage)
3. 过滤 `coverage_pct == 0` (zero-cov) → 331 bundles
4. 按 doc_type 均匀分配, 抽 15 个作为 `recommended_first`

**EKRS 抽样建议**:
- 从 `recommended_first` 15 个起步 (覆盖 4 doc-type)
- 扩展抽样: 若 15 个验证发现新模式 (e.g. 部分表单识别失败), 从 `full_list` 中按 filename 关键词额外取 5-10 个
- 优先选 `n_blocks` 较高 (≥5) 的 bundle, 因为 block 多 → form_field / column_header 期望产出更显著

---

## 四、§五 EKRS 内部裁决 (doc-to-md 端确认)

| 问题 | EKRS 裁决 | doc-to-md 立场 |
|---|---|---|
| 问题 1 scope_classifier 整合 | 维持现状 (EKRS 内部推断) | 接受. doc-to-md 字段保留但 EKRS 不消费, 不浪费 |
| 问题 2 weight 默认值 | 硬编码 heading=1.0 / form_field=0.9 / column_header=0.7 | 接受. config-driven 是过度设计 |
| 问题 3 T10b-3 短路下 boost | 不生效, 正确行为 | 接受. 短路在 RRF 之前, form_field boost 在排序阶段, 位置不同 |
| 问题 4 R6 strict mode | 不阻断 | 接受. form_field boost 与 doc-type 权重同属确定性 |
| 问题 5 抽样集 | doc-to-md 提供 152 清单, EKRS 选 10-15 | 接受. 152 清单见 §三 |
| 问题 6 heading_path 链路缩短 | 等 V3 量化数据再评估 | 接受. Phase 13 PDF filters 影响待量化 |

**全部接受, 无异议.**

---

## 五、联调时间窗对齐

| 日期 | 行动方 | 内容 |
|---|---|---|
| 2026-08-14 | doc-to-md | 本接受 doc ship ✅ + 152 bundle 清单交付 ✅ (提前 1 天, 见 §三) |
| 2026-08-18 ~ 08-19 | EKRS | T1-T5 实施 (Chunk 模型 / chunker 透传 / FTS5 schema / retriever 扩展) |
| 2026-08-20 | EKRS + doc-to-md | Q5 re-ingest 完成后 FTS5/Qdrant drift 验证, 联调窗口 |

**doc-to-md 在 8/20 联调窗口前无需出新代码**, 仅在 EKRS 完成后跑 `scripts/verify_reingest.py` 对 LOT/CHECK 抽样验证 form_fields/column_headers 被正确消费.

---

## 六、状态更新

| 文档 | 旧 status | 新 status |
|---|---|---|
| `ekrs-scope-priority-confirmation-2026-08-14.md` | pending_confirmation | **closed_per_建议1_acceptance** |
| `ekrs-scope-priority-reply-2026-08-14.md` (EKRS 侧) | ekrs_internal_decisions_recorded_pending_doc_to_md_schema_response | **建议1 已接受, EKRS 启动 T1-T5** |

---

## 七、未解决问题

1. ~~**152 bundle 清单落盘**~~ ✅ 已交付 2026-08-14 (见 §三, 331 zero-cov + 15 recommended-first)
2. **Q5 re-ingest 时机**: EKRS reply §四 提到 8/20 联调前需 Q5 re-ingest. doc-to-md `scripts/verify_reingest.py` 的 Q5 (745 docs re-ingest commit `736fd3b`) 是否覆盖本次新增 form_fields/column_headers? 需检查 Q5 验证脚本是否更新包含新字段
3. **协调项 #6 warning log 量**: 协调项 #6 ship (commit `58cc527`) 后长尾 bundle 首次 re-ingest 触发 warning 数量级未测. 若超日志可读阈值需 rate-limit
4. **LOT/CHECK 真实 bundle 端到端抽样**: 即使 doc-to-md 0 改动, 仍需在 8/20 联调时跑真实 bundle 抽样验证 `form_fields` / `column_headers` 经 EKRS 全链路后 RAG 检索质量提升 (recall@10 baseline 对比)
5. **清单扫描脚本固化**: 本次临时用 `/tmp/scan_long_tail.py` + `/tmp/scan_zero_cov.py` 派生 331 清单. 若后续需要重新生成 (e.g. 新增 LOT bundle 入库), 需固化进 `scripts/` 目录 (低优先级 P3, 当前 331 已覆盖现有 corpus)
