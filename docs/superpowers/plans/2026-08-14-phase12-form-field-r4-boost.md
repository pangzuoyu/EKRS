---
title: "Phase 12 Q3 §9.6 last-mile — form_field/column_header → R4 boost"
date: 2026-08-14
category: docs/superpowers/plans
module: rag-integration
phase: 12
parent_plan: /home/pangzy/code_project/EKRS/docs/superpowers/specs/2026-07-30-doc-to-md-heading-path-coordination.md
status: closed_建议1_accepted_plan_v3_gstack_eng_review_applied
target_window: 2026-08-18 ~ 2026-08-20
pre_check_deadline: 2026-08-18 (verify_reingest.py 状态确认)
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
| **T1** | **模型扩展**: `Chunk` + `Metadata` 同步新增 `form_fields` / `column_headers` 字段, **默认值 `default_factory=list` (空列表, 非 None)** — 避免下游 retriever / FTS5 重复 None-check (gstack D4) | 0.5 day | `shared/ekrs_shared/models.py:Chunk` + `Metadata` |
| **T2a** | **IR parser 透传验证**: 跑 `test_ir_parser_loads_form_fields.py` 验证 `rag/ekrs_rag/ingestion/ir_parser.py` 已从 data.jsonl 加载 `metadata.form_fields` / `column_headers` 到 `BlockMetadata` (T1 已加 Optional 字段的前提下) | 0.25 day | `rag/ekrs_rag/ingestion/ir_parser.py` (verify) + `tests/unit/test_ir_parser_loads_form_fields.py` |
| **T2b** | **IR parser 修复 (T2a failed 才执行)**: 修 `ir_parser.py` 加载 + 增加 fallback warn log (与协调项 #6 同模式), 然后 chunker 透传 (block → chunk) + Qdrant payload 写入 `form_fields` / `column_headers` 数组字段 | 0.5-1 day (conditional) | `rag/ekrs_rag/ingestion/ir_parser.py` (if needed) + `chunker.py` + `qdrant_client.py` |
| **T3** | **FTS5 schema 全量重建** (FTS5 不支持 ALTER TABLE ADD COLUMN, 见 §三.1 详细策略) + **30s drain + 3-attempt retry decorator** (D3) + **ConsistencyChecker 重建期间抑制 drift audit** (D1) | 0.5-1 day (refined from 1.5-2 days, D7) | `rag/ekrs_rag/retrieval/fts_manager.py` |
| **T4** | `retriever._scope_priority()` 扩展: 读取新字段, 按 type 加权 (`max(base, weight)` **在函数内部计算**, 不动下游 `vec * (1 + scope)` 公式). **单一 touchpoint** — T10b-3 short-circuit 不读 form_fields (per parent §25, D5) | 0.5 day | `rag/ekrs_rag/retrieval/retriever.py:_scope_priority()` |
| **T5** | 测试: 6 个命名 test files (D6) + golden set 50 case 回归 + Boundary 2 frequency check + 75-query recall@10 baseline | 1 day | `tests/unit/` + `tests/golden_set/` |

**T5 6 个命名 test files** (D6):
- `tests/unit/test_models_form_fields.py` — Pydantic round-trip
  - `test_chunk_default_factory_yields_empty_list()`
  - `test_metadata_default_factory_yields_empty_list()`
  - `test_chunk_roundtrip_with_form_fields_column_headers()`
  - `test_metadata_empty_lists_serialize_to_json_array()`
- `tests/unit/test_ir_parser_loads_form_fields.py` — T2a 验证
  - `test_ir_parser_loads_form_fields_from_data_jsonl()`
  - `test_ir_parser_loads_column_headers_from_data_jsonl()`
  - `test_ir_parser_loads_both_fields_concurrently()`
  - `test_ir_parser_does_not_silently_drop_unknown_fields()` (Pydantic extra='ignore' 守卫)
- `tests/unit/test_chunker_form_fields_passthrough.py` — T2 chunker
  - `test_chunker_copies_form_fields_block_to_chunk()`
  - `test_chunker_copies_column_headers_block_to_chunk()`
  - `test_chunker_preserves_field_order_for_fns5_round_trip()`
  - `test_chunker_handles_missing_fields_default_to_empty_list()`
- `tests/unit/test_fts_v2_schema.py` — T3 schema
  - `test_fts_chunks_v2_contains_form_fields_column()`
  - `test_fts_chunks_v2_contains_column_headers_column()`
  - `test_fts_v2_chunk_id_still_primary_key()`
  - `test_fts_v2_payload_json_unindexed_backward_compat()`
- `tests/unit/test_fts_v2_indexing.py` — T3 召回
  - `test_fts_v2_recall_form_field_lot_49()`
  - `test_fts_v2_recall_column_header_a105()`
  - `test_fts_v2_recall_normal_text_not_corrupted_by_form_fields()`
  - `test_fts_v2_rebuild_from_qdrant_payload_does_not_lose_chunks()`
- `tests/unit/test_retriever_form_field_boost.py` — T4 weighting
  - `test_scope_priority_max_with_form_field_weight()`
  - `test_scope_priority_max_with_column_header_weight()`
  - `test_scope_priority_no_stacking_heading_plus_form_field()`
  - `test_scope_priority_base_floor_when_no_form_fields()`
  - `test_retriever_short_circuit_unaffected_by_form_field_boost()` (T10b-3 parity)

**合计 3-4 天**, 8/18-8/19 完成 (T3 因 D7 时间估算修正, 实际预算内).

### §三.0 IR parser 静默丢字段风险 (T1/T2 隐藏前置)

**风险**: `shared/ekrs_shared/models.py:Metadata` 当前**未定义** `form_fields` / `column_headers` 字段. Pydantic `extra='ignore'` (默认) 会**静默丢弃**未声明字段, 与 [OvisOCR2 静默丢字段同类问题](#).

**T1 必须同时** (D4 default_factory=list):
- `Chunk` 新增 `form_fields: List[Dict[str, Any]] = Field(default_factory=list)` / `column_headers: List[Dict[str, Any]] = Field(default_factory=list)`
- `Metadata` **同步**新增 `form_fields: List[Dict[str, Any]] = Field(default_factory=list)` / `column_headers: List[Dict[str, Any]] = Field(default_factory=list)` (让 IR parser 加载到 BlockMetadata)
- **为什么 default_factory=list 而不是 Optional[list] = None**: 下游 retriever `_scope_priority()` 和 FTS5 string builder 不需要 None-check; `[].append(...)` 安全; Chunk model 既有约定（如 `source_block_ids` 用 `Field(default_factory=list)`).

**T2a 验证步骤** (2h):
- 单元测试: `tests/unit/test_ir_parser_loads_form_fields.py` — data.jsonl 含 `metadata.form_fields=[{key, value}]` 时, IR 解析后 `block.metadata.form_fields` 非空
- 验证 3 个 case: form_fields_only / column_headers_only / both
- 通过 → 直接进入 T3; 失败 → T2b (修复)

**T2b 修复步骤** (conditional, 4h):
- 修 `ir_parser.py` 加载路径 (典型: BlockMetadata 模型未声明字段 → 加声明, 或 load_from_jsonl 路径未透传)
- 加 fallback warn log (与协调项 #6 `_warn_missing_heading_paths` 同模式): outline 存在但 field 为空时主动 log.warning
- 端到端: chunker debug log 中 `form_fields` / `column_headers` 非空 (从 `000151778ca35475` bundle 验证)

**T2 总预算**: 0.25 天 (T2a) + 0.5-1 天 (T2b, conditional) = 0.25-1.25 天

### §三.1 T3 详细: FTS5 schema 迁移策略

**关键约束**: SQLite FTS5 虚拟表**不支持** `ALTER TABLE ADD COLUMN`. 任何 schema 变更需要**全量重建**.

**迁移步骤** (3 阶段, 顺序不可换):

1. **新建 FTS5 表** (`fts_chunks_v2`):
   ```sql
   CREATE VIRTUAL TABLE fts_chunks_v2 USING fts5(
     chunk_id, block_id, text, scope_path, status, doc_hash,
     form_fields,          -- 新增: 拼接 form_fields.key + form_fields.value (空格分隔)
     column_headers,       -- 新增: 拼接 column_headers.header (空格分隔)
     payload_json UNINDEXED
   );
   ```

2. **全量重新索引** (从 Qdrant payload 重建, **不从 data.jsonl 重新摄取**):
   ```python
   # 遍历 Qdrant 所有 points, scroll + 读 payload.form_fields / column_headers
   # 写入 fts_chunks_v2 (批量 INSERT, 1000/批)
   for offset in qdrant.scroll_all(batch_size=1000):
       for point in offset:
           fts_row = build_fts_row_from_qdrant_payload(point.payload)
           fts_manager_v2.insert(fts_row)
   ```
   **估算** (D7 refined): Phase 9 stress 验证 +268 chunks/batch + ~600ms/batch 端到端. 按 50k chunks total = 200 batches × 600ms ≈ **30 min**. 加 milestone 阶段 (bench + rebuild + verify) = **0.5-1 day** 实际预算. T3 估算从原 1.5-2 天下修.

**2.5 子步骤**: **bench 验证 (10 min)** — 在 1k chunks 子集先跑一次, 确认 600ms/batch 估算准确后再启动全量 rebuild. 失败则回归 1.5-2 天估算.

3. **原子切换表名** (in transaction + **D3 30s drain + retry**):
   ```sql
   BEGIN TRANSACTION;
   DROP TABLE fts_chunks;
   ALTER TABLE fts_chunks_v2 RENAME TO fts_chunks;
   COMMIT;
   ```
   简化方案: DROP 旧表 + RENAME 新表 (事务内, 失败回滚). **不需要保留旧表** (Qdrant 是 source of truth, FTS 仅是 secondary index).

   **D3 30s drain + retry decorator**:
   - **Drain (30s)**: 重命名前, RAG 服务进入 drain mode: 拒绝新的 `/v1/ingestion/notify` (返 503); 等待 30s 让 in-flight FTS5 read 调用自然完成. 借助 Phase 8 rate-limit 模式或新增 env var `EKRS_DRAIN=true`.
   - **Retry decorator**: retriever FTS5 调用包 `@retry_on_sqlite_busy(max_attempts=3, backoff_ms=100)` — 第 1 次 SQLITE_BUSY → 100ms → 第 2 次 BUSY → 200ms → 第 3 次 BUSY → fail with proper error. 文档写在 `rag/ekrs_rag/retrieval/fts_manager.py` docstring.
   - **D1 ConsistencyChecker 抑制**: T3 启动时设 env var `EKRS_FTS_MIGRATION_IN_PROGRESS=true`; `ConsistencyChecker.run()` 入口检查此 flag, 命中则 short-circuit (不 emit drift audit, 不 increment counter). T3 完成后 (atomic rename 后) flag clear. 失败时 caddy-up: flag 自动 1h expiry via filelock timestamp.

**回滚策略**:
- 迁移前 snapshot Qdrant collection alias (Phase 8 vendored)
- 失败时: 重建 fts_chunks (旧 schema), 重新从 Qdrant payload 跑旧索引逻辑 (无 form_fields/column_headers)
- 回滚不丢数据 (Qdrant 不动)
- **D3 retry decorator 失败兜底**: 3 attempts 后仍 SQLITE_BUSY → 抛 `FTSSchemaChangeError` (新异常类型, `rag/ekrs_rag/retrieval/fts_manager.py`); HTTP 503 配合 `Retry-After: 5` header.

**T3 单元测试要求**:
- `test_fts_v2_schema.py` — 验证新表结构含 form_fields / column_headers 列
- `test_fts_v2_indexing.py` — 验证 form_field "Lot 49" 经 FTS5 召回 hit
- `test_fts_v2_backward_compat.py` — 验证 chunk_id 仍为 PK, block_id / scope_path / payload_json 仍可查询

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

**前置 (8/18 实施前必查, P1 缺口)**:
- ⚠️ **8/18 之前**确认 `doc-to-md/scripts/verify_reingest.py` **是否已更新以验证 form_fields / column_headers 传递**. 不覆盖则:
  - 选项 A (优先): doc-to-md 在 8/18 前先更新脚本 (低工作量, doc-to-md 接受建议 1 = 0 改动的前提下, 这是 doc-to-md 唯一动作)
  - 选项 B: EKRS 自己写 `scripts/verify_reingest_ekrs_fields.py` 独立验证脚本
- 此项**不应留到联调当天才发现** (否则 8/20 上午无法验证字段是否真正到达 EKRS 侧)

| 时段 | 行动方 | 内容 |
|---|---|---|
| 8/20 上午 | doc-to-md | 跑 `scripts/verify_reingest.py` LOT/CHECK 抽样 (从 `long_tail_lot_check_152.json` 推荐 15 选 10-15) 验证 form_fields/column_headers 经 EKRS 全链路 |
| 8/20 上午 | EKRS | 验证 FTS5/Qdrant drift (T10a-2 `ConsistencyChecker` 检测) — form_fields/column_headers 双写一致性 (T3 全量 rebuild 后必查) |
| 8/20 下午 | 双方 | **端到端抽样 recall@10 baseline 对比 (P1, 必须产出量化数据)** — form_field boost 启用前后, 从推荐 15 抽样各 5 query (form_field 锚点 + column_header 锚点 + 普通 heading 锚点 各覆盖) |
| 8/20 下午 | EKRS | golden set 50 case 回归 (V3) — 量化 heading_path 链路缩短 (Phase 13 PDF filters) 对 R4 检索精度的影响 |

**§七 问题 3 验收门槛**: recall@10 baseline 对比**必须产出量化数据**. 若 form_field boost 启用后无显著提升 (或反而下降), 需重新评估:
- 权重设计 (0.9 / 0.7 是否过强 / 过弱)
- 是否需要进一步特征工程 (e.g. form_field key vs value 加权区分)
- 选项 C 是否优于选项 A/B (历史决策回顾)

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

| # | 项 | 优先级 | 处理建议 | 来源 |
|---|---|---|---|---|
| 1 | `scripts/verify_reingest.py` 是否覆盖新 form_fields/column_headers 字段 | **P1** | **8/18 前确认, 不覆盖则先更新脚本** (doc-to-md 唯一动作或 EKRS 写独立脚本) — §五前置已展开 | doc-to-md §七 |
| 2 | 协调项 #6 P3 ship (commit `58cc527`) 后, 长尾 bundle 首次 re-ingest 触发 warning log 量未测 | P2 | 联调时监控, 超阈值再加 rate-limit (非阻塞) | doc-to-md §七 |
| 3 | **端到端抽样 recall@10 baseline 对比** (form_field boost 启用前后) | **P1** | **本次工作核心验收指标, 必须产出量化数据**. 5 query × 3 锚点类型 (form_field / column_header / heading) × 推荐 15 抽样 = 75 query 集. 无显著提升或反而下降需重新评估权重设计 | doc-to-md §七 + §五验收门槛 |
| 4 | 清单扫描脚本固化 (临时 `/tmp/scan_long_tail.py` + `/tmp/scan_zero_cov.py` 进 `scripts/` 目录) | P3 低优 | 不阻塞, 后续处理 | doc-to-md §七 |

**§七 隐藏前置** (本计划新增, 不在 doc-to-md 列表):
| # | 项 | 优先级 | 处理建议 |
|---|---|---|---|
| 5 | `shared/ekrs_shared/models.py:Metadata` 新增 `form_fields` / `column_headers` Optional 字段 (T1 隐藏前置, 避免 IR parser 静默丢字段) | P0 | **T1 必含**, 单元测试断言 IR parser 加载到 BlockMetadata |

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
- 2026-08-14: v2 — 应用 user engineer review (D1-D6 from user: IR parser 静默丢字段风险 + FTS5 迁移详细策略 + 优先级重排)
- 2026-08-14: v3 — 应用 gstack-plan-eng-review (D1-FTS5-suppression + D2-T2-split + D3-drain+retry + D4-default_factory + D5-single-touchpoint + D6-named-test-files + D7-estimate-refine)

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

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/plan-eng-review` v3 | Architecture & tests (required) | 1 | CLEAR | 7 issues found, 0 critical gaps, 0 unresolved |
| CEO Review | — | Scope & strategy | 0 | — | N/A (coordinator plan, not product) |
| Codex Review | — | Independent 2nd opinion | 0 | — | Skipped per user (D8 outside voice declined) |
| Design Review | — | UI/UX gaps | 0 | — | N/A (backend only) |
| DX Review | — | Developer experience gaps | 0 | — | N/A (internal coordination) |

**UNRESOLVED:** 0
**VERDICT:** ENG CLEARED — ready to implement on 2026-08-18. 7 decisions (D1-D7) applied to plan v3.

### Findings summary (D1-D7)

| # | Section | Severity | Decision | Resolution |
|---|---|---|---|---|
| D1 | Architecture | High | T3 FTS5 rebuild → ConsistencyChecker drift audits flood | Suppress via `EKRS_FTS_MIGRATION_IN_PROGRESS=true` flag + 1h expiry |
| D2 | Architecture | High | T2 verify-then-fix has no defined branch | Split T2 → T2a (verify, 0.25d) + T2b (fix-if-needed, 0.5-1d) |
| D3 | Architecture | High | T3 atomic rename races in-flight FTS5 reads | Add 30s drain + 3-attempt retry decorator + `FTSSchemaChangeError` 503 |
| D4 | Code Quality | Medium | Pydantic `Optional[list] = None` forces downstream None-checks | Switch to `default_factory=list` (D4) — matches existing `source_block_ids` convention |
| D5 | Code Quality | Low | DRY risk: weighting logic could sprawl | Single touchpoint: `_scope_priority()` only; T10b-3 short-circuit unaffected (parent §25) |
| D6 | Test | High | T5 vague ("新字段 round-trip + golden 50") | Expand to 6 named test files with 22 specific test methods |
| D7 | Performance | Medium | T3 estimate "数小时" over-estimates by 1-2 orders | Refine to 30 min based on Phase 9 +268 chunks/batch × 600ms; add bench-validate step |