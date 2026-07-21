---
title: "EKRS 对 doc-to-md 集成对接问题清单的书面回复"
date: 2026-07-20
category: docs/solutions/integration-issues
module: rag-integration
problem_type: cross_system_coordination_response
component: parser-rag-bridge
source_questions: /home/pangzy/code_project/doc-to-md/docs/solutions/integration-issues/ekrs-coordination-questions-2026-07-20.md
reviewed_revision: 97a7c63
status: answered_with_contract_gaps_then_doc_to_md_finalization
---

# EKRS 对 doc-to-md 集成对接问题清单的书面回复

> 本回复对应源清单 Q1-Q6。结论以 EKRS `master` commit `97a7c63`
> 的当前代码和测试为准；引用均为 EKRS 仓库相对路径。
>
> 按源清单说明，本回复写在 EKRS 仓库，不修改 doc-to-md 仓库。

## 执行结论

| Q | EKRS 当前实际行为 | 对接结论 |
|---|---|---|
| Q1 | `output_path` 被当作目录，并固定读取其下的 `data.jsonl` | 当前 `blocks.jsonl` 不兼容；`v<N>` 仅可由发送方作为精确目录传入 |
| Q2 | 核心字段/JSON/schema 错误 fail-fast；不检查 `.ready`，不逐块 skip | 不能假设 skip + warn；必须完整、原子落盘后再 notify |
| Q3 | `202` 后由 `BackgroundTasks` 执行；callback 共尝试 3 次；原样回传 body 中的 `trace_id` | EKRS 当前不发 `X-Parser-Token`，与 doc-to-md 强制鉴权直接冲突 |
| Q4 | 路由按 `(trace_id, doc_hash, version)` 去重；Qdrant 侧按 `doc_hash` 查询并比较 `version` | `content_hash` 不在通知模型、查询或 payload 中，对当前 RAG 幂等无作用 |
| Q5 | 旧版本删除 helper 存在但生产路径没有调用 | 运行时不删除旧版本；Qdrant 中多版本并存 |
| Q6 | 不读 `meta.json`，也不读 `index.json.doc_metadata` | 文档级元数据应放入 notify body 的 `metadata.doc_metadata` |

当前有两个会直接阻断真实往返测试的契约不一致：

1. doc-to-md 写 `blocks.jsonl`，EKRS 只读 `data.jsonl`。
2. doc-to-md callback 强制验证 `X-Parser-Token`，EKRS callback 当前不发送该头。

---

## Q1 答复 — `output_path` 如何读取 JSONL

### 实际行为

EKRS 不把 `output_path` 当作 JSONL 文件，也不会根据 `version` 自动进入
`v<N>` 子目录。当前逻辑等价于：

```python
output_path = Path(notification.output_path)
jsonl_path = output_path / "data.jsonl"
```

因此：

- `output_path=/parsed/doc/v3` → 读取 `/parsed/doc/v3/data.jsonl`；
- `output_path=/parsed/doc`，文件在 `v3/data.jsonl` → 找不到；
- `output_path=/parsed/doc/v3/data.jsonl` → 错误拼成 `.../data.jsonl/data.jsonl`；
- `blocks.jsonl` 没有 fallback，也没有可配置文件名。

`SHARED_STORAGE_PATH` 虽在应用启动时传给 pipeline，但当前读取逻辑直接使用通知中的
`output_path`，没有基于该配置重写或限制路径。

### 代码引用

- `rag/ekrs_rag/ingestion/pipeline.py:40-45,55-61`
- `rag/ekrs_rag/ingestion/ir_parser.py:78-97`
- `shared/ekrs_shared/models.py:199-207`
- `rag/ekrs_rag/main.py:199`
- `rag/tests/integration/test_ingestion.py:71-93`

### 决策

保留 doc-to-md 的版本目录决策，但当前对接必须满足：

```text
<output>/text/<doc_id>/v<N>/data.jsonl
notify.output_path = <output>/text/<doc_id>/v<N>/
```

RAG 不校验 `notification.version` 与目录名 `v<N>` 是否一致，发送方必须保证一致。
若 doc-to-md 必须继续以 `blocks.jsonl` 为唯一真理源，则需先修改 EKRS 文件名契约；
在此之前不能把 `blocks.jsonl` 路径直接发给当前 EKRS。

---

## Q2 答复 — 半写、缺字段和 schema 不匹配

### 实际行为

当前行为不是“skip 该 chunk + warn”，而是以下分层规则：

1. notify body 缺 `doc_hash`、`version` 或 `output_path`：FastAPI/Pydantic 在进入
   handler 前拒绝，通常返回 `422`。
2. JSONL 每行缺 `doc_id`、`block_id` 或 `type`：抛 `IRParseError`，第一处错误终止整批。
3. 非法 JSON 或 Pydantic schema 校验失败：包装成 `IRParseError`，整批失败。
4. 缺 `content` 或 `metadata`：当前 parser 会填宽松默认值；若最终没有可生成的 chunk，
   callback 返回 `rag_status="failed"`。
5. `numeric_hints` 不属于当前 `DocumentBlockIR` 字段。缺失不会报错；发送该额外字段时，
   Pydantic v2 默认会忽略它。当前 RAG 在查询阶段从 chunk 文本现场提取 hints，而不是从
   parser JSONL 读取预计算 hints。

半写方面，普通 notify 路径没有 `.ready` 检查、checksum、文件稳定性检查、读锁或解析重试：

- 尾部是半条 JSON → 整批 parse 失败；
- 读取时只有若干条完整 JSON 且已经遇到 EOF → 该完整前缀可能被当作完整文件成功入库；
- 空文件 → failed callback。

还有一个状态偏差：上述已知解析失败分支发送 failed callback 后正常 `return`；后台 wrapper
只按“是否抛异常”更新 TaskRepo，所以 callback 发送成功时，RAG 内部任务仍可能被标记为
`COMPLETED`。因此 status endpoint 目前不能单独作为解析成功的真理源。

### 代码引用

- `rag/ekrs_rag/ingestion/ir_parser.py:22-54,78-97`
- `rag/ekrs_rag/ingestion/pipeline.py:55-83`
- `rag/ekrs_rag/api/routes/ingestion.py:134-142`
- `shared/ekrs_shared/models.py:16-48,180-193`
- `rag/ekrs_rag/retrieval/retriever.py:72-73`
- `rag/tests/unit/test_ir_parser.py:56-88,152-203`

### 决策

- doc-to-md 必须在完整文件原子发布后再 notify；不能依赖 EKRS 检查 `.ready`。
- 不设置“缺 core 字段时降级 text-only”的灰度假设；发送前先按当前 IR 校验。
- doc-to-md 的 Numeric Hint 决策不是当前 EKRS ingestion 的前置条件。若要让 EKRS消费
  parser 预计算 hints，双方需要先扩展 `DocumentBlockIR`、chunker、payload 和测试契约。

---

## Q3 答复 — callback 时机、重试和 `trace_id`

### 实际行为

- notify handler 先返回 `202`，再通过 FastAPI `BackgroundTasks` 执行 ingestion；这不是
  独立持久队列，进程退出时没有 durable delivery 保证。
- 正常路径在 Qdrant upsert 成功后发送 `success` callback；幂等命中也会直接发送
  `success` callback。文件缺失、空文件、IR 错误、无 chunk、Qdrant 写失败会发送
  `failed` callback。
- `_send_callback` 使用 tenacity `stop_after_attempt(3)`：**3 次总尝试**，不是
  “首次 + 3 次重试”。按当前 `wait_exponential(min=2, max=10)` 参数，前两次失败后
  各等待约 2 秒；第 3 次失败立即抛出。单次 HTTP timeout 为 30 秒。
- 4xx、5xx 和网络异常当前都会进入同一重试机制。
- callback payload 原样使用 `notification.trace_id`。doc-to-md 当前若不在 notify body 中
  发送 `trace_id`，EKRS 模型默认值为空字符串，callback 不会自动补成 middleware 生成的 ID。
- **当前 callback 请求没有发送 `X-Parser-Token` 或其他鉴权头。**

### 代码引用

- `rag/ekrs_rag/api/routes/ingestion.py:134-145`
- `rag/ekrs_rag/ingestion/pipeline.py:48-95,129-163`
- `shared/ekrs_shared/models.py:199-207`
- `rag/ekrs_rag/observability/trace.py:14`
- doc-to-md 接收侧：`/home/pangzy/code_project/doc-to-md/rag/callback.py:34-39`

### 决策

- doc-to-md 保留 `X-Parser-Token` 强制校验，**不开放匿名 callback**。
- EKRS 侧须给 callback 增加 `X-Parser-Token`；在携带 secret 前，同时限制/校验
  `callback_url` 的 scheme 和目标 host，避免 SSRF 与 secret 外发。
- 401/403 应快速失败，不应重试；仅网络错误和可恢复 5xx 重试。
- doc-to-md CallbackHandler 继续按 `(doc_hash, version)` 幂等更新，并保留启动轮询兜底；
  当前补偿 handler 仍是 stub，不能替代该兜底。

**当前判定**：在 EKRS 补鉴权头前，启用非空 `PARSER_TOKEN` 的真实 callback 往返会被
 doc-to-md 返回 `403`；把 token 配为空不是修复，而是鉴权失效。

---

## Q4 答复 — ingestion 幂等键

### 实际行为

当前存在两层、都不含 `content_hash` 的幂等判断：

1. 路由层：`md5(trace_id | doc_hash | version)` 生成 `request_id`，TaskRepo 以
   `request_id` 主键去重；完全相同的三元组返回 `202 duplicate`。
2. Qdrant 前置检查：按 `doc_hash` 查询 ingestion status；若返回记录为 `success` 且
   `existing.version == notification.version`，跳过 upsert 并发送 success callback。

`IngestionNotification` 没有 `content_hash` 字段，Qdrant upsert payload 也不保存它，
`get_ingestion_status` 不按它过滤。因而同一 `doc_hash + version` 的内容发生变化时，当前
RAG 可能直接 skip，不能依赖 `content_hash` 触发重建。

### 代码引用

- `shared/ekrs_shared/idempotency.py:7-10`
- `rag/ekrs_rag/api/routes/ingestion.py:79-98`
- `rag/ekrs_rag/storage/task_repo.py:14-26,70-88`
- `rag/ekrs_rag/ingestion/pipeline.py:48-54`
- `rag/ekrs_rag/retrieval/qdrant_client.py:176-184,215-250`
- `shared/ekrs_shared/models.py:199-207`
- `shared/tests/test_idempotency.py:1-22`

### 决策

- doc-to-md 仍可把 `content_hash` 用作自身完整性和版本判定依据，但当前不要把它视为
  EKRS 已实现的去重键。
- 在 EKRS 补齐规范前，“内容改变”必须递增 `version`；同 version 不应承载不同内容。
- 若双方坚持规范中的 `doc_hash + content_hash` 幂等，EKRS 需同步修改通知模型、
  Qdrant payload、过滤条件和跨版本测试，不能只让 doc-to-md 增加字段。

---

## Q5 答复 — 旧版本删除策略

### 实际行为

运行时**没有旧版本删除**。

`QdrantManager.delete_old_versions(doc_hash, keep_version)` 虽已实现，但生产代码没有调用者。
该 helper 的过滤也不是 `version < new_version`，而是：

```text
doc_hash == X AND version != keep_version
```

若被陈旧任务调用，这个条件可能删除比 `keep_version` 更新的数据。该方法是同步调用，
配置为最多 3 次尝试；最终失败会记录 `qdrant_write_failed` 并重新抛出。但由于 ingestion
pipeline 从不调用它，当前 notify v2/v3 只会 upsert 新 point，v1/v2 继续留在 Qdrant。
point ID 包含 `(doc_hash, version, source_block_ids)`，不同版本天然并存。

### 代码引用

- `rag/ekrs_rag/retrieval/qdrant_client.py:170-184,317-348`
- `rag/ekrs_rag/ingestion/pipeline.py:85-95`
- `rag/tests/unit/test_qdrant_client.py:329-372,496-516`

### 决策

- doc-to-md 不得把本地 `version_cleanup.py` 的完成解释为“RAG 旧版本也已删除”；两者当前
  没有事务或 callback 协调。
- 旧 bundle 至少保留到对应版本收到明确 success callback；RAG 数据清理仍应由 EKRS 负责，
  不建议 parser 直接操作 Qdrant。
- EKRS 后续应在同文档锁内、成功 upsert 后接入安全的 `version < new_version` 删除，
  并为失败定义明确状态；当前 helper 的 `!= keep_version` 不能直接作为并发安全契约。

---

## Q6 答复 — `meta.json` 还是 `index.json.doc_metadata`

### 实际行为

两者都不读取。当前 runtime 只读取 `output_path/data.jsonl`。

文档级元数据的实际入口是 notify body：

```json
{
  "metadata": {
    "doc_metadata": {
      "doc_id": "...",
      "type": "...",
      "scope_path": "...",
      "status": "active"
    }
  }
}
```

该对象由 ingestion route 写入 `DocumentRepo`。没有 `metadata.doc_metadata` 时静默跳过；
写入失败时记录 `document_metadata_failed`，但不阻断 JSONL ingestion。每个块的页码、bbox、
heading_path 则来自 `data.jsonl` 行内的 `metadata`，与文档级 metadata 是两套来源。

### 代码引用

- `rag/ekrs_rag/api/routes/ingestion.py:102-132`
- `rag/ekrs_rag/ingestion/pipeline.py:55-66`
- `rag/ekrs_rag/ingestion/ir_parser.py:66-97`
- `shared/ekrs_shared/models.py:23-27,199-207`
- `rag/tests/integration/test_phase6_e2e.py:115-149`

### 决策

- doc-to-md 不需要为了当前 EKRS 拆出独立 `meta.json`。
- 现有 `index.json.doc_metadata` 也不会被 EKRS 自动消费；若要传文档元数据，应由
  `RAGClient.notify()` 显式映射到 `metadata.doc_metadata`。
- `index.json` 可继续作为 doc-to-md 内部 manifest，但不能把它当作 EKRS metadata 接口。

---

## 双方应冻结的对接契约

1. bundle：`<doc>/v<N>/data.jsonl`，notify 的 `output_path` 指向该版本目录。
2. notify：补发非空 `trace_id`；文档元数据使用 `metadata.doc_metadata`。
3. callback：EKRS 发送 `X-Parser-Token`，doc-to-md 保持 timing-safe 校验；限制 callback host。
4. Numeric Hint：当前由 EKRS 查询阶段提取；若改为 parser 预计算，先升级共享 IR 契约。
5. 幂等：当前以 trace/doc/version + doc/version 为准；内容变化必须升 version。
6. 清理：EKRS 未接入旧版本删除；在补齐前，不宣称 Qdrant 只保留最新版。

## doc-to-md 最终处置（2026-07-20 回合）

源清单 EKRS 侧回复后，doc-to-md 团队给出六项未决问题的最终处置。本节记录
doc-to-md 的最终立场；EKRS 侧负责的修复以"双方应冻结的对接契约"和
"尚未解决的问题"为准，不因 doc-to-md 的回退而撤销。

### 1. Callback 鉴权 / 重试过滤 / URL allowlist — 全部归责 EKRS

- doc-to-md 保留 `X-Parser-Token` 强制校验（timing-safe `compare_digest`），
  不为迁就当前 EKRS 实现开放匿名回调。
- 测试环境可通过 `CALLBACK_BYPASS_AUTH=true` 临时绕过鉴权；生产环境必须保留。
- EKRS 必须在 `_send_callback` 中实现以下三项才能落地：
  1. 发送 `X-Parser-Token` 头；
  2. 重试策略改为仅对 5xx 与网络超时生效，4xx（特别是 401/403）立即失败；
  3. 对 `callback_url` 限制 scheme（仅 https）与 host 白名单，避免 SSRF 场景下的
     token 外发。

### 2. JSONL 文件名 — doc-to-md 单方改名 `data.jsonl`

- doc-to-md 立即把 `backend/core/io/bundle_writer.py` 中的 `JSONL_FILENAME` 从
  `blocks.jsonl` 改为 `data.jsonl`，无协商空间。
- bundle 结构冻结为 `<output>/text/<doc_id>/v<N>/data.jsonl`，通知 `output_path`
  必须精确指向该版本目录。
- 在 EKRS 提供文件名配置化能力之前，doc-to-md 不再变动该文件名；任何反向协商
  须以新 RAG 版本发布为前提。

### 3. `content_hash` 幂等 — doc-to-md 降级为内部校验

- P1-2（`content_hash` 与 JSONL 字节不一致）降级为 P2，本 sprint 不投入修复。
- doc-to-md 的 `content_hash` 仅保留两种用途：
  - 本地重复解析检测；
  - 完整性校验（同 version 但内容变化时主动报错）。
- EKRS 侧不动；RAG 幂等继续以 `trace/doc/version` 与 `doc/version` 为键。
- 强制规则：内容变化必须递增 `version`，不得用 `content_hash` 触发 RAG 重建。

### 4. 旧版本 Qdrant 删除 — doc-to-md 彻底放弃，EKRS 全权负责

- `services/version_cleanup.py` 仅负责本地 bundle 目录的 `archived` 状态，
  不再尝试与 RAG 联动或承诺 "RAG 旧版本同步清理"。
- doc-to-md 不会直接操作 Qdrant；查询最新版本应通过 EKRS 查询参数控制，
  不依赖 parser 侧删除数据。
- 若 EKRS 后续接入清理，必须改用 `version < new_version` 过滤条件，并在同一
  `doc_hash` 的分布式锁内完成；当前 `version != keep_version` 条件存在误删风险。

### 5. TaskRepo / callback 状态不一致 — 纯 EKRS 内部 Bug，doc-to-md 加防御

- doc-to-md 在 `CallbackHandler` 增加防御：收到 `rag_status="failed"` 时无条件
  把本地 `parse_tasks.rag_status` 置为 `failed` 并记录 error 字段；不再假设
  EKRS TaskRepo 状态可信。
- EKRS 仍须修复 `ingestion/pipeline.py`：让 `callback` 的 `rag_status` 与
  `TaskRepo` 的 `status` 强一致（已知 ingestion 失败分支先发 failed callback
  再正常 return，导致 TaskRepo 可能标 `COMPLETED`）。
- doc-to-md 的防御是兜底，不能替代 EKRS 的状态机修复。

### 6. `.ready` 与 `SHARED_STORAGE_PATH` — 双侧各自修一处

- doc-to-md 保留 `.ready` 原子创建逻辑，但仅作为 parser 内部发布完成信号
  （完整性检查、调试、未来其他消费者），不再宣称 RAG 会读它。
- 双方各自修改规范文档：删除 `EKRS-RAG-AI_intergration.md` 中关于 "RAG 主动
  检测 `.ready`" 的描述；写入新条款 "RAG 不依赖 `.ready` 文件；parser 必须在
  JSONL 完整落盘、fsync 完成后才发送 notify"。
- `SHARED_STORAGE_PATH`：双方约定 `notify.output_path` 必须以 `SHARED_STORAGE_PATH`
  为根；EKRS 应在 ingestion 入口校验，越界则拒绝。在 EKRS 补齐校验前，
  doc-to-md 按当前约定保证路径合规。

---

## doc-to-md 立即执行的变更清单

| 项 | 调整后的行动 | 状态 |
|---|---|---|
| `blocks.jsonl` → `data.jsonl` | 立即改 `backend/core/io/bundle_writer.py` | 执行 |
| `content_hash` 修复（原 P1-2） | 降级为 P2，本 sprint 不做 | 推迟 |
| `version_cleanup.py` 与 RAG 联动 | 删除联动逻辑，仅做本地归档 | 执行 |
| `.ready` 原子创建 | 保留实现，更新文档说明 "仅 parser 内部使用" | 执行 |
| Callback token 校验 | 保留强制校验，测试环境可临时 `CALLBACK_BYPASS_AUTH=true` | 执行 |
| `notify.metadata.doc_metadata` 映射 | 必须实现，文档元数据走 notify body | 执行 |

---

## 尚未解决的问题

> 本节对应上一回合的"尚未解决的问题"。doc-to-md 已对 Q2/Q3/Q4/Q6 给出最终处置；
> 真正仍需 EKRS 侧修复的项已收窄。

### 仍归责 EKRS

1. **Callback 鉴权头**：`_send_callback` 须新增 `X-Parser-Token`；在携带 secret 前
   须先校验 `callback_url` 的 scheme 与 host 白名单（防 SSRF + token 外发）。
2. **4xx 重试过滤**：`_send_callback` 须将 401/403/4xx 划出重试集合，仅 5xx 与
   网络超时重试。
3. **TaskRepo / callback 状态机**：`ingestion/pipeline.py` 须把 callback 的
   `rag_status` 与 TaskRepo `status` 强一致；当前"先发 failed callback 再正常
   return"会让 TaskRepo 出现误标 `COMPLETED`。
4. **`SHARED_STORAGE_PATH` 路径校验**：ingestion 入口须拒绝 `output_path` 不在
   `SHARED_STORAGE_PATH` 之下的请求。
5. **旧版本安全删除**（如未来要实现）：须改用 `version < keep_version` 过滤，
   并在同 `doc_hash` 分布式锁内执行；当前 `!= keep_version` helper 不能直接复用。

### 已由 doc-to-md 单方处置，EKRS 侧无需动作

1. ~~JSONL 文件名~~：已冻结 `data.jsonl`；doc-to-md 单边改名生效。
2. ~~`content_hash` 幂等~~：已降级为内部校验；EKRS 幂等键不动。
3. ~~主动清理 RAG 旧版本~~：已撤回；doc-to-md 不再尝试与 RAG 联动。
4. ~~`.ready` 接口语义~~：双方各自修文档，明确 RAG 不读 `.ready`；doc-to-md
   内部继续保留原子创建。
