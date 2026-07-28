# Phase 9 文档交叉比对裁决记录

> **裁决文档 — Phase 9a 启动前的文档对齐基准。**
> 日期：2026-07-24
> 权威基准：[`2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md`](2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md)（[5]）
>
> 本文件记录对 6 份研究文档交叉比对发现的 3 处直接冲突 + 4 处潜在不一致的最终裁决。
> 所有后续开发以本文件裁决为准；受影响文档已标注 `[已修订见 ADR]`。

---

## 文档编号对照

| 编号 | 文档 | 简称 |
|------|------|------|
| [1] | `2026-07-24-mineru-explorer-feature-mapping.md` | feature-mapping |
| [2] | `2026-07-24-mineru-deep-dive-extensions.md` | deep-dive |
| [3] | `2026-07-24-ekrs-mineru-integration-feasibility.md` | integration-feasibility |
| [4] | `2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md` | **retrieval-port（权威基准）** |
| [5] | `2026-07-24-ekrs-enhanced-ui-design.md` | ui-design |
| [6] | `2026-07-24-ekrs-enhanced-logging-design.md` | logging |

---

## 🔴 直接冲突裁决

### 冲突 1：Reranker 在 Strict 模式下的行为

| 维度 | 内容 |
|------|------|
| **[2] deep-dive 表述** | Cross-encoder 重排序为"可选增强"，暗示 strict 模式下可能仍可配置开启 |
| **[4] retrieval-port 表述** | 明确规定"strict=True 时必须强制跳过重排序"，定义为非确定性操作 |
| **裁决** | **以 [4] 为准：strict=True 时强制跳过重排。不可配置覆盖。** |
| **裁决依据** | R2 铁律（求解器纯函数）是系统存在的根本约束。Cross-encoder 模型推理引入浮点级非确定性，与 R2 直接冲突。即使标记为"可选"，任何允许 strict+rerank 的代码路径都是 R2 违规的潜在入口。UI/API 层必须强制拦截，不提供配置开关。 |
| **影响范围** | [2] deep-dive §1.3.2、[5] ui-design §5.1 |
| **修订动作** | [2] 补充 strict 门控说明；[5] UI 约束查询界面 strict 复选框与重排开关互斥 |

### 冲突 2：双写一致性策略

| 维度 | 内容 |
|------|------|
| **[1] feature-mapping 表述** | 列出"同步双写"与"异步事件驱动"两个并列选项，未做最终决策 |
| **[4] retrieval-port 表述** | 明确选定"同步双写"方案，否决异步方案 |
| **裁决** | **以 [4] 为准：Phase 9 采用同步双写（Qdrant upsert + FTS5 insert 在同一摄取事务内）。** |
| **裁决依据** | Phase 9 的摄取吞吐需求不需要异步队列的复杂度。同步双写可以利用现有 AuditWriter 的原子写入模式（与 qdrant_write_failed 一致），FTS 写入失败通过 `fts_sync_failed` 审计事件记录，不阻断 Qdrant 写入。异步队列引入消息中间件依赖、乱序风险、重试幂等性问题，Phase 9 不值得。 |
| **影响范围** | [1] feature-mapping §1 |
| **修订动作** | [1] 标注异步方案为"Phase 10+ 候选，Phase 9 不采用" |

### 冲突 3：LLM 摘要功能的 Phase 归属

| 维度 | 内容 |
|------|------|
| **[1] feature-mapping 表述** | 明确将"LLM 查询扩展"推迟至 Phase 10+ |
| **[2] deep-dive 表述** | 在精读模块设计中包含"可选 LLM 摘要生成"接口，标注为 Phase 9 scope |
| **裁决** | **以 [1] 为准：LLM 摘要/查询扩展均推迟至 Phase 10+。Phase 9 不实现。** |
| **裁决依据** | Phase 9 的核心价值是 BM25+向量+RRF 的确定性广谱检索。LLM 摘要引入非确定性 + 延迟 + 模型依赖，与 Phase 9 的确定性保证目标矛盾。[2] 中的"可选 LLM 摘要生成"接口属于设计前瞻性探索，不应进入 Phase 9 实现范围。 |
| **影响范围** | [2] deep-dive §2（Deep Read 模块） |
| **修订动作** | [2] 将 LLM 摘要接口标注为"Phase 10+ 设计预留，Phase 9 不实现（no-op stub）" |

---

## 🟡 潜在不一致对齐

### 不一致 1：RRF 参数 k 的可配置性

| 维度 | 内容 |
|------|------|
| **[1] & [2]** | 将 k=60 作为固定常量引用（与 QMD 一致） |
| **[4]** | 提到 k 可在 40-80 间调优，暗示为配置项 |
| **裁决** | **Phase 9 硬编码 k=60（与 QMD 一致）。通过 config.yaml 暴露为可配置项但默认值 60。不在 API 请求参数中暴露。** |
| **裁决依据** | k=60 是 RRF 论文的推荐值，QMD 验证有效。Phase 9 的首要目标是验证混合检索的价值，不是参数调优。调优留到 Phase 9b golden set 回归后再评估是否需要暴露给 API 调用方。 |
| **实现方式** | `rrf_fusion.py` 中 `DEFAULT_K = 60`；`config.yaml` 新增 `retrieval.rrf_k: 60`（可选覆盖）；API 请求不暴露 k 参数。 |

### 不一致 2：中文分词管线缺失

| 维度 | 内容 |
|------|------|
| **[2] deep-dive** | 摄取管线设计中仅提及"文本清洗"和"Chunking"，未指定中文分词器 |
| **[4] retrieval-port** | 明确指出 SQLite FTS5 默认分词器不支持中文，建议使用 unicode61 或集成 jieba |
| **裁决** | **Phase 9 FTS5 表使用 `tokenize='unicode61 remove_diacritics 2'` 作为默认分词器。同时提供 jieba 前置分词的配置开关（默认关闭，CJK 文档比例高时启用）。** |
| **裁决依据** | `unicode61` 对中英文混排文档可以做基本的字符级分词（CJK 字符逐字切分），比默认的 `porter unicode61`（porter 词干提取对中文无效且可能破坏 CJK token）更适合工程文档。jieba 前置分词在摄取时对中文文本做词级切分，能显著提升 BM25 对中文工程术语（如"设计压力"、"腐蚀裕量"）的召回率，但增加摄取依赖（jieba 包）和处理延迟。Phase 9a 先用 unicode61 验证基线，Phase 9b 评估是否启用 jieba。 |
| **实现方式** | FTSManager 构建表时 `tokenize='unicode61 remove_diacritics 2'`；`config.yaml` 新增 `retrieval.fts_tokenizer: unicode61`（可选值：`unicode61`, `jieba`）；jieba 模式下摄取时对 chunk.text 做预分词再写入 FTS5。 |
| **修订动作** | [2] 补充分词步骤说明；[4] 确认 FTSManager 代码片段中的 tokenize 参数 |

### 不一致 3：日志回溯与数据过期的 UI 表达

| 维度 | 内容 |
|------|------|
| **[6] logging** | Track 2（search_trace.log）仅保留 7 天 |
| **[5] ui-design** | 设计了"历史检索回溯"功能，未区分 Track 1/2 的数据可用性 |
| **裁决** | **UI 回溯界面必须区分 Track 1（业务级，~1.8 年可用）和 Track 2（详细级，7 天内可用）。超过 7 天的回溯显示"详细检索数据已过期"提示。** |
| **裁决依据** | 用户查询 7 天前的 trace 时，Track 2 已轮转删除，`seek()` 返回空。UI 如果不区分显示，用户会以为"没有检索路径数据"而非"数据已过期"，造成困惑。 |
| **修订动作** | [5] ui-design §5.1 回溯界面增加数据可用性状态指示器 |

### 不一致 4：Gate 短路逻辑的可视化

| 维度 | 内容 |
|------|------|
| **[4] retrieval-port** | 定义了"强信号短路"机制（BM25 top score ≥0.85 且 gap ≥0.15 时跳过重排） |
| **[5] ui-design** | 检索路径追踪图仅展示标准 Gate 1→1.5→2→3 流程，未体现短路分支 |
| **裁决** | **UI 检索路径图必须包含短路分支的视觉标识。** |
| **裁决依据** | 短路是检索流水线的正常行为（不是错误），但用户如果看到"Gate 1.5 被跳过"却不理解原因，会质疑结果可靠性。 |
| **修订动作** | [5] ui-design §5.1 检索路径图增加短路虚线 + 原因标注 |

---

## 裁决执行清单

| 优先级 | 裁决项 | 影响文档 | 修订状态 |
|--------|--------|---------|---------|
| **P0** | 冲突 1：Reranker strict 强制跳过 | [2], [5] | ✅ 已修订 |
| **P0** | 冲突 2：双写策略统一为同步 | [1] | ✅ 已修订 |
| **P1** | 不一致 2：中文分词器 | [2], [4] | ✅ 已修订 |
| **P1** | 不一致 4：短路可视化 | [5] | ✅ 已修订 |
| **P2** | 冲突 3：LLM 摘要推迟 | [2] | ✅ 已修订 |
| **P2** | 不一致 1：RRF k 参数 | [4] | ✅ 已修订 |
| **P2** | 不一致 3：日志过期 UI | [5] | ✅ 已修订 |
