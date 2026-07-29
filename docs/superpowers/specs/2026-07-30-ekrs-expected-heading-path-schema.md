# EKRS 期望 schema: `DocumentBlockIR.metadata.heading_path`

**日期**: 2026-07-30
**状态**: 已锁定 — 待 doc-to-md 执行
**关联**: [2026-07-30-doc-to-md-heading-path-coordination.md](2026-07-30-doc-to-md-heading-path-coordination.md)

## 1. 字段定义

`metadata.heading_path` 是一个已存在的字段 (`shared/ekrs_shared/models.py:36`)，类型为 `Optional[List[str]]`。**无需任何 Pydantic 模型改动**。doc-to-md 只需按此契约填充值。

**语义**: `heading_path` 仅承载**标题层级**——从根标题到直接包含当前块的叶标题的有序标题列表。它**不是** doc-type classifier (用于 R4 scope_priority 的文档类型分类器)；该分类是独立缺口, 不得通过修改标题文本来模拟。

类型契约:

- `None` — 文档无大纲树 (当前 ~10% 文档的行为)
- `[]` — 文档有大纲树, 但当前块不被任何标题包裹
- `["A", "B", "C"]` — 块被标题 A → B → C 完整包裹 (根→叶)

chunker 内部使用 `block.metadata.heading_path or []` 消费, 所以 `None` 和 `[]` 等价, 无需刻意区分。

## 2. 排序与深度规则

**排序**: 根标题在前, 叶标题在后。

深度语义:

- **最小深度**: 1 (块直接位于顶级标题下)
- **最大深度**: 无上限 (与 `outline.json` 深度一致)
- **嵌套重叠**: 若多个标题共同包含同一块 (如父标题横跨整节, 子标题横跨子节), 取**最深** (最具体) 的路径。这为 chunker 的 Boundary 2 提供最细粒度的 scope-change 信号。

## 3. 边界案例

| 场景 | heading_path 预期值 | 说明 |
|------|---------------------|------|
| 任何标题之前的块 | `[]` 或 `None` | 标题页、前言等 |
| 直接位于一级标题下 | `["Top Heading"]` | 单元素路径 |
| 五层嵌套段落 | `["A", "B", "C", "D", "E"]` | 完整链路 |
| 多层标题重叠区域 | 取最深路径 | 见 §2 嵌套重叠规则 |
| 文档无 `outline.json` | 全部块: `None` | 维持现状 |
| 块本身就是标题 | 标题自身的路径 | 顶级标题为 `[]`, 子标题取父路径 |

## 4. 消费者代码引用

| 消费者 | 位置 | 行为 |
|--------|------|------|
| Chunker Boundary 2 | `rag/ekrs_rag/ingestion/chunker.py:660-690` | `heading_path` 变化时触发 flush |
| `_get_scope_path` | `rag/ekrs_rag/ingestion/chunker.py:77-79` | 单一 fallback 点: `heading_path or []` |
| `_extract_provision_id` | `rag/ekrs_rag/constraint_engine/evidence_builder.py:118-130` | 扫描标题文本中的条款号正则 |
| `_scope_priority` | `rag/ekrs_rag/retrieval/retriever.py:237-243` | 映射 `scope_path[0]` 到文档类型权重 |
| FTS5 索引列 | `rag/ekrs_rag/retrieval/fts_manager.py:64, 249` | `" ".join(scope_path)` 用于限定列 MATCH |
| Qdrant payload | `rag/ekrs_rag/retrieval/qdrant_client.py:219` | 存储为 `scope_path` 数组 |

## 5. 非目标 (本 schema 明确不承载)

**Doc-type classifier (R4 scope_priority)**: `_scope_priority` 期望 `scope_path[0]` 匹配 `national/industry/enterprise/project/reference`, 但当前始终 fallback 到 default 40。该分类应来自独立信号 (如文档级 `scope_classifier` 字段), **不得在 `heading_path` 中 prepend `national/` 等工作区**。doc-type classifier 由 Phase 12 Task C 独立处理。

**标题文本规范化**: EKRS 不规范化标题。保留原始文本, 因为 `_extract_provision_id` 依赖原标题中的条款号。规范化是消费者行为, 不是生产者职责。

## 6. 给 doc-to-md 的执行指引

- **映射算法**: 推荐基于范围的映射 (`block_id ∈ [start_block_id, end_block_id]`), 对每个块 O(n), 优于树遍历。
- **嵌套重叠处理**: 使用最深路径 (见 §2)。
- **数据质量**: 非嵌套重叠 (如标题 A 跨 10-50, 标题 B 跨 30-40) 属 doc-to-md 数据质量问题, 标记修复, **无需 EKRS 侧做歧义消除**。
- **标题文本**: 保持 `outline.json` 中的原始标题文本, **不做前缀剥离或格式化**。

## 7. 验证标准 (doc-to-md 修复后, EKRS 侧执行)

1. **覆盖率**: 30-doc 随机样本中, ≥80% 的块 `heading_path` 非空 (当前 0.1%)
2. **Chunk 传递率**: 分块后 ≥50% 的 chunk 有非空 `scope_path` (当前 ~0%)
3. **Golden 回归**: 50-case golden set 全过
4. **Boundary 2 频率**: scope-change flush 从恒为 0 变为 >0
5. **T10b-2 重测**: heading-less 占比从 100% 降至 <50%, cond#2 可能成为新的决胜条件

## 8. 变更策略

- 本 spec 是 EKRS 的契约文档, 变更需协调, **不能单方面修改**
- `metadata` 新字段添加需走 Pydantic 模型 + R1 铁律审查
- chunker 的 `or []` fallback 是有意宽松设计, **不得收紧为强制 []**

## 9. 版本记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-07-30 | v1.0 | 初始锁定, 待 doc-to-md 签收 |