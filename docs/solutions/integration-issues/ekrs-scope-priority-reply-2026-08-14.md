---
title: "EKRS scope_priority + heading_path boost 协调回复 (Q3 §9.6 last mile)"
date: 2026-08-14
category: docs/solutions/integration-issues
module: rag-integration
problem_type: cross_system_coordination
component: block-assigner + scope-classifier + rag-bridge
related_plan: /home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-doc-to-md-heading-path-coordination.md
related_request: /home/pangzy/code_project/doc-to-md/docs/solutions/integration-issues/ekrs-scope-priority-confirmation-2026-08-14.md
related_solution: ../integration-issues/parse-markdown-form-extractor-integration.md
target_audience: doc-to-md development team
status: 建议1_accepted_T1_T5_scheduled_2026-08-18_to_08-20
ekrs_decision: option_C_with_reuse_existing_fields_optimization
ekrs_actions: retriever_FTS5_consume_form_fields_column_headers_no_new_metadata_required
ekrs_internal_resolutions: [item_1_internal_inference, item_2_hardcode_weights, item_3_shortcircuit_no_boost, item_4_strict_no_block, item_5_10to15_bundles_from_doc_to_md_list, item_6_defer_to_v3_data]
acceptance_doc: ekrs-scope-priority-acceptance-2026-08-14.md
acceptance_status: doc-to-md_accepted_建议1_zero_doc_to_md_changes_required
bundle_list_delivered: scripts/long_tail_lot_check_152.json (331 zero-cov + 15 recommended-first)
pending_doc_to_md_input: []
---

# EKRS scope_priority + heading_path boost 协调回复

> 对 doc-to-md [`ekrs-scope-priority-confirmation-2026-08-14.md`](file:///home/pangzy/code_project/doc-to-md/docs/solutions/integration-issues/ekrs-scope-priority-confirmation-2026-08-14.md) §二 4 个核心问题 + §三 三选项, 给出 EKRS 答复与裁决.

---

## 一、答复总览

| 问题 | 答复 | 备注 |
|---|---|---|
| **Q1** heading_path 当前消费形态 | (a) 扁平 `list[str]` | 所有消费者均按 string 数组处理 (见 §二 Q1) |
| **Q2** scope_priority 是否已存在 | (c) 尚未支持 (作为 form 语义加权字段) | 概念已落地为 `_SCOPE_PRIORITY_MAP` 代码常量, 非存储 schema; `metadata.scope_classifier` 在 doc-to-md 输出中存在但 EKRS 未消费 (方案 B 内部推断) |
| **Q3** R4 索引方式 | (c) 混合, 但权重仅作用于顶层 | 全字符串进 FTS5 keyword; 仅 `scope_path[0]` 参与 doc-type 权重 |
| **Q4** heading_path 是否兼容新格式 | (a) 保留旧字段 + 新增 | heading_path 不动, 新需求走独立通道 |
| **§三 倾向选项** | **C**, 但建议先评估"复用已有字段"路径 | 见 §三 |
| **期望联调时间窗** | 2026-08-18 ~ 2026-08-20 | 等 EKRS 完成 Q5 re-ingest 验证 |

---

## 二、Q1-Q4 详细答复

### Q1. heading_path 当前消费形态 — (a) 扁平 `list[str]`

**当前 EKRS 所有消费者均按 `List[str]` 处理**:

| 消费者 | 代码 | 处理方式 |
|---|---|---|
| chunker | `rag/ekrs_rag/ingestion/chunker.py:79` `_get_scope_path()` | `return block.metadata.heading_path or []` 原样返回 |
| retriever | `rag/ekrs_rag/retrieval/retriever.py:240-241` `_scope_priority()` | `chunk.scope_path[0].lower()` 取顶层做 doc-type 权重映射 |
| FTS5 | `rag/ekrs_rag/retrieval/fts_manager.py:64, 249` | `" ".join(chunk.scope_path)` 作为全文索引字符串 |
| Qdrant payload | `rag/ekrs_rag/retrieval/qdrant_client.py:219` | 存储为 `scope_path` 数组字段 |

**没有任何消费者支持结构化对象或带 type 标签的元素.** heading_path 在整条 ingest → index → retrieve 链路是 `list[str]` 不可变契约 (与协调项 #1 ship 一致).

### Q2. scope_priority 字段是否已存在 — (c) 尚未支持

需要先澄清两个概念, 避免混淆:

**A. `metadata.scope_classifier` (doc-to-md 已 ship, EKRS 未消费)**:
- 来源: doc-to-md commit `f3a6a36` (Phase 12 Q1 P0), filename 静态分类 → 5 类 (`national/industry/enterprise/project/reference/unknown`)
- EKRS 侧消费情况: **未消费**. EKRS Phase 12 方案 B 决策 (`/home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-doc-to-md-heading-path-coordination.md:144`): "不新增 metadata.scope_classifier 字段. 在 pipeline 增加 `_classify_doc_type(source_filename)` 静态函数 + 可配置映射规则". 全仓 grep 零命中.
- 用途: 文档级 doc-type 分类 (哪个标准/规范类别), 喂给 R4 `scope_path[0]` 权重. 这是 **文档级别** 的.

**B. `metadata.scope_priority` (doc-to-md 本次提议的 form 语义加权字段)**:
- EKRS 现状: 无 Pydantic schema 定义; 无 Qdrant payload 字段; 无 FTS5 列.
- 仅存在内部代码常量 `_SCOPE_PRIORITY_MAP` (`retriever.py:26-28`): `{"national":100, "industry":80, "enterprise":60, "project":40, "reference":20}`. 这是 **派生计算**, 不是数据字段.
- 用途 (本次提议): 块级 form 字段 / 列头语义加权 (LOT/CHECK/STATUS 文档锚点).

**结论**: 若新增 `metadata.scope_priority` (用于 form 语义), EKRS 端需同步修改:
- `shared/ekrs_shared/models.py:Metadata` 新字段 (Optional)
- `rag/ekrs_rag/ingestion/ir_parser.py` 透传
- `rag/ekrs_rag/ingestion/chunker.py` 是否参与 scope 派生 (见 Q3 关键影响)
- `rag/ekrs_rag/retrieval/retriever.py:_scope_priority()` 读取新字段

### Q3. R4 scope-aware retrieval 的索引方式 — (c) 混合, 但权重仅作用于顶层

**当前实现**:

| 维度 | 实现 |
|---|---|
| 全字符串拼接 | `fts_manager.py:64` `" ".join(chunk.scope_path)` 进 FTS5 keyword index (R7 scope 过滤) |
| 顶层元素独立 boost | `retriever.py:240-241` 仅 `scope_path[0].lower()` 命中 `_SCOPE_PRIORITY_MAP` → national=1.0 / industry=0.8 / enterprise=0.6 / project=0.4 / reference=0.2 |
| 权重公式 | `retriever.py:277` `final_score = vec * (1 + scope)` 在 RRF 之后 |
| 无 level-based 加权 | `scope_path[1:]` 仅作为 FTS 检索词, 不参与权重计算 |

**关键影响** (对选项 B 致命):

> 如果 form 字段注入 heading_path (选项 B), 它们会成为 scope_path 的一部分, **可能污染 `scope_path[0]`** (如果 form 字段排在首位), 破坏 doc-type 权重; 即使排在后面, **也无法获得独立加权** (因为只有 `[0]` 被 boost). 因此选项 B 对 R4 **不友好**, 选项 A 也类似.

要让 form_field / column_header 真正参与 R4 boost, 必须:
- 走独立字段 (选项 C 基础), **或**
- 注入到 `scope_path[0]` 位置 (破坏 doc-type 权重, 不可接受)

### Q4. heading_path 改造是否兼容新格式 — (a) 保留旧字段 + 新增

**EKRS 强烈要求 heading_path 保持 `list[str]` 不变**:

- 选项 A (结构化对象): 触发 chunker / FTS5 / Qdrant payload / retriever 4 处同步改造, 周期 1-2 周, 且破坏 R7 scope_filter 字符串语义.
- 选项 B (字符串前缀): 破坏 `_scope_priority` 的 `scope_path[0]` 映射 (前缀 `FORM:`/`COL:` 不在 `_SCOPE_PRIORITY_MAP` 内 → fallback 到 default 40); 同时 FTS5 tokenize 后前缀语义丢失.
- 选项 C 的渐进迁移 (union 类型): Pydantic v2 不友好 (`discriminated_union` 维护成本); EKRS 侧缺乏 schema version 路由基础设施.

EKRS 倾向新增独立字段, 与 heading_path 完全隔离. heading_path 是已 ship 契约, 不动摇.

---

## 三、§三 选项裁决

**EKRS 选择: 选项 C** — 新增独立 `scope_priority` 字段 (同意 doc-to-md 倾向)

但 EKRS 在 C 的基础上提出两个**优化建议**, 建议 doc-to-md 先评估:

### 建议 1 (强推荐): 直接复用已有的 `metadata.form_fields` / `metadata.column_headers`

doc-to-md 已在 Q3 §9.6 ship 这两个字段 (commits `e4fbb36`+`023b45d`+`f9deae5`). **EKRS 可以直接在 retriever 和 FTS 索引中读取这两个字段**, 而**不需要 doc-to-md 再额外打包生成 `scope_priority` 数组**.

**优点**:
- 避免数据冗余 (form_fields 既在 form_fields 里, 又在 scope_priority 里)
- doc-to-md 无需新增字段生成逻辑 (Step 3-5 工作量降为 0)
- EKRS 可以直接按字段类型设计权重, 无需同步新 schema
- FTS5 索引可立即扩展: 将 `form_fields.key` / `form_fields.value` / `column_headers.header` 作为独立索引词

**实现路径 (EKRS 端)**:
1. `chunker.py`: 在 chunk 阶段读取 block 的 `metadata.form_fields` 和 `metadata.column_headers`, 存入 `Chunk.form_fields` / `Chunk.column_headers` 新字段
2. `qdrant_client.py:upsert_chunks`: payload 新增 `form_fields` / `column_headers` 数组字段
3. `fts_manager.py`: schema 新增 `form_fields TEXT` / `column_headers TEXT` 列, 索引 key+value+header
4. `retriever.py:_scope_priority`: 扩展逻辑读取新字段, 按 type 加权 (form_field=0.9 / column_header=0.7 / heading=1.0)

**doc-to-md 端工作**: 0 (已 ship)

### 建议 2 (备选): 若 doc-to-md 仍希望新增 `scope_priority` 字段

最终 schema 草案:

```python
# data.jsonl blocks[].metadata
{
  "heading_path": ["NCR Status Report", "Lot 49"],       # 不变 (协调项 #1 契约)
  "form_fields": [{"key": "SYSTEM NO", "value": "Lot 49"}],   # 已有 (Q3 §9.6)
  "column_headers": [{"index": 0, "header": "A105"}],         # 已有 (Q3 §9.6)
  "scope_priority": [   # 新增, 可选
    {"type": "form_field", "key": "SYSTEM NO", "weight": 0.9},
    {"type": "column_header", "value": "A105", "weight": 0.7}
  ]
}
```

**EKRS 侧将**:
- `retriever._scope_priority` 读取 `metadata.scope_priority`, 作为额外 boost 叠加到 doc-type 权重之上
- FTS5 索引: `form_fields` / `column_headers` 独立索引词 (不进入 heading_path)
- heading_path 既有消费逻辑完全不变

**doc-to-md 端工作**: Step 3-5 (parser emit + orchestrator 回写 + E2E), 周期 3-5 天

---

## 四、行动项与联调时间窗

### EKRS 侧 (建议 1 路径)

| 步骤 | 内容 | 周期 |
|---|---|---|
| T1 | `Chunk` 模型新增 `form_fields` / `column_headers` 字段 (Optional) | 0.5 天 |
| T2 | `chunker.py` 透传, `qdrant_client.py` payload 写入 | 1 天 |
| T3 | `fts_manager.py` schema 迁移 + 索引扩展 | 1 天 |
| T4 | `retriever._scope_priority` 扩展权重公式 (`max(base, weight)`) | 0.5 天 |
| T5 | 测试: 单元 (新字段 round-trip) + golden set 回归 + Boundary 2 frequency | 1 天 |

合计 **4 天**, 在 Phase 12 follow-up 周次执行.

### EKRS 侧 (建议 2 路径)

T1-T5 + `metadata.scope_priority` schema 引入 + 模型字段添加, 合计 **5-7 天**.

### 联调时间窗

**2026-08-18 ~ 2026-08-20**

- 8/18-8/19: EKRS 端实施 (T1-T5)
- 8/20: EKRS Q5 re-ingest 后 FTS5/Qdrant drift 验证完成 → 联调窗口
- 依赖: doc-to-md 在 8/14-8/17 完成 schema 协商 (建议 1 无需, 建议 2 需)

### 前置条件

doc-to-md 需先回答 (在 8/15 前回):
> **是否接受"EKRS 直接消费已有 form_fields / column_headers"作为首选方案 (建议 1), 而 scope_priority 仅作为可选优化 (建议 2)?**

---

## 五、EKRS 内部裁决 (2026-08-14)

### 问题 3: T10b-3 短路路径下 form_field boost 是否生效 — **裁决: 不生效, 且这是正确行为, 不是缺陷**

**理由**:
- T10b-3 短路是**检索策略优化** — 当精确匹配命中时, 绕过 RRF 直接返回匹配结果. 短路发生在 RRF **之前**.
- form_field boost 是 RRF **排序阶段**的权重修正, 与短路在管线不同位置.
- 短路语义: 精确匹配 (如用户查询 "Lot 49" 直接命中 `SYSTEM NO: Lot 49`) 本身就是**最强检索信号**, 不需要额外语义加权调整排序.
- 多精确命中 chunks 排序: 由其他**确定性信号**决定 (如 block 顺序, doc_hash 字母序), 不依赖 form_field 权重.

**验证补充**: golden set 增加用例, 验证短路命中 form_field value 时, 返回结果排序确定性 (与 Phase 10 baseline 一致), 不依赖 form_field boost.

### 问题 4: R6 strict mode 下 form_field boost 是否被阻断 — **裁决: 不阻断, strict 模式下照常生效**

**理由**:
- R6 禁止的是**推断** (LLM, cross-encoder 等非确定性组件).
- form_field boost 是**确定性 scope 权重计算**, 与 doc-type 权重 (`_scope_priority`) 属于**同一类别** — 都是基于文档结构元数据的确定性加权.
- Strict 模式下, doc-type 权重照常生效 (来自 `metadata.scope_classifier` 或 filename 推断, 确定性); form_field boost 同样照常生效. 两者都不涉及推断, 不违反 R6.

**验证补充**: golden set strict=true 用例, 验证 form_field boost 不改变求解器纯函数行为 (R2), 即求解器输入/输出与 Phase 10 baseline 一致.

### 问题 1: scope_classifier 整合 — **裁决: 维持现状 (EKRS 内部推断)**

doc-to-md 的 `metadata.scope_classifier` 字段是**冗余信息源**, 内部自治更简单. Phase 12 方案 B 内部 `_classify_doc_type()` 已工作, 切换消费 doc-to-md 字段需要 Pydantic 模型改动 + 回填风险, ROI 不明确. 待有明确需求 (如跨 doc-type 联合统计) 再切换.

### 问题 2: scope_priority[].weight 默认值 — **裁决: 采纳 EKRS 建议值, 硬编码**

采纳 `heading=1.0 / form_field=0.9 / column_header=0.7`, **硬编码即可**. config-driven 是过度设计, 等有数据驱动调优需求再考虑. (若采用建议 2 引入 scope_priority 字段, 权重在 EKRS `_scope_priority()` 内常量定义.)

### 问题 5: LOT/CHECK 真实 bundle 抽样集 — **裁决: doc-to-md 提供清单, EKRS 选 10-15 个**

doc-to-md 提供 152 个长尾 bundle 清单 (Q3 调查识别). EKRS 从中选 **10-15 个** 作为跨方验证样本. 抽样标准: 覆盖 5 个 doc-type 各 ≥2 个, 优先选含 form_field 的 LOT/CHECK/STATUS doc.

### 问题 6: heading_path 链路缩短影响 (Phase 13 PDF filters) — **裁决: 等数据再评估**

Phase 13 commit `1685ca3` (PDF heading Phase 4 filters) 让 outline 节点大幅减少 (e.g. GB50019 root 137→4, RP0492 160→4). 对 R4 scope-aware 检索的精度影响待 V3 (golden set 50 case 回归) **量化数据**出来后再评估. **不提前动作.**

---

## 六、Acceptance (2026-08-14) ✅

doc-to-md 接受建议 1 (跨方协调闭环):

| 项 | 状态 |
|---|---|
| §四 行动项 — 接受建议 1 | ✅ accepted — EKRS 直接消费 `metadata.form_fields` / `metadata.column_headers`, doc-to-md 端 **0 改动** |
| §四 行动项 — 建议 2 不采用 | ✅ dropped — `scope_priority` 字段不引入, heading_path 契约 (`list[str]`) 不动摇 |
| §五 问题 5 — 152 bundle 清单 | ✅ delivered 2026-08-14 (提前 1 天, 实际给 331 zero-cov + 15 recommended-first) |
| §五 问题 1-4 + 6 EKRS 裁决 | ✅ doc-to-md 全接受, 无异议 |

**联调时间窗** (双方对齐):
- 2026-08-18 ~ 08-19: EKRS T1-T5 实施 (Chunk 模型 / chunker 透传 / Qdrant payload / FTS5 schema / retriever 扩展)
- 2026-08-20: Q5 re-ingest 完成后 FTS5/Qdrant drift 验证 + 联调窗口 (doc-to-md 跑 `scripts/verify_reingest.py` LOT/CHECK 抽样验证)
- 2026-08-20 联调前 doc-to-md **无需出新代码**

详细 acceptance 内容见 [`ekrs-scope-priority-acceptance-2026-08-14.md`](ekrs-scope-priority-acceptance-2026-08-14.md).

§七 中保留的 5 项未解决问题 (Q5 re-ingest 覆盖 / warning log 量 / 端到端抽样 / 扫描脚本固化) 转入 8/20 联调执行阶段.

---

## 七、回复联系人

- EKRS 侧 owner: Phase 12 scope-aware 优化解锁由本文档承接
- 后续 cross-repo 协调: 走 EKRS `docs/solutions/integration-issues/` ↔ doc-to-md 同目录双向 reply

---

**附录 A — 实施 commit 索引 (待定)**

| Commit | 日期 | 范围 | 标题 |
|---|---|---|---|
| (TBD) | 2026-08-18 ~ 08-20 | Phase 12 follow-up T1-T5 | feat(retriever): consume form_fields + column_headers for R4 boost |

**附录 B — EKRS 侧相关代码定位**

```
shared/ekrs_shared/models.py:24-28       Metadata.heading_path 定义
shared/ekrs_shared/models.py:71-76       Priority IntEnum (NATIONAL=100..REFERENCE=20)
rag/ekrs_rag/ingestion/chunker.py:77-79  _get_scope_path() 直接消费 heading_path
rag/ekrs_rag/retrieval/retriever.py:26-28  _SCOPE_PRIORITY_MAP 派生常量
rag/ekrs_rag/retrieval/retriever.py:237-241  _scope_priority() 从 scope_path[0] 派生
rag/ekrs_rag/retrieval/retriever.py:273-281  _rank_by_scope() 权重公式 vec * (1 + scope)
rag/ekrs_rag/retrieval/fts_manager.py:64, 249  " ".join(scope_path) 索引
rag/ekrs_rag/retrieval/qdrant_client.py:219  payload scope_path 存储
```