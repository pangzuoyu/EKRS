# 协调：doc-to-md 端需修复的结构性问题（影响 RAG ingestion）

> **Audience**: doc-to-md 团队（pipeline / parsers / ocr 模块维护者）
> **Author**: EKRS（Phase 13c-C13 re-ingest 联调）
> **Date**: 2026-08-27
> **Source failure data**: `/tmp/failed_bundles_classified.json`（3809 bundles, 96 failed）
> **Failed bundle manifest**: `deployment/phase13c-c13-failed-bundles-manifest.json`（96 entries，doc_hash + file_name + category + reason）

## TL;DR

Phase 13c-C13 把 `/mnt/disk/text/` 下的 3809 个 bundle 全量 re-ingest 进了 EKRS Qdrant（3497 ingest 成功，Qdrant 累计 319,896 chunks，v=2）。剩余 **96 bundles（2.7%）chunk 数 = 0**，pipeline 全部走到 `no_chunks` 终态。

**核心结论**：96 个失败**不是 EKRS chunker 的 bug**，是 doc-to-md 输出结构问题让 chunker 拿不到可消化的输入**。三类问题占比 92%：

| 类别 | 数量 | 占比 | EKRS chunker 视角的根因 |
|------|-----:|-----:|------------------------|
| `single_table_monolith` | 61 | 63.5% | doc-to-md 把多行 markdown 表压成 1 个 `content.structured` 行（仅标题），chunkerr 的 structured-row 路径产生 0 chunks |
| `mixed_type_large` | 13 | 13.5% | `text/image/table` 混合 + 多 block 但有质量瑕疵，OCR/分类在边界 case 上分类错位 |
| `tiny_content_fragmented` | 11 | 11.5% | OCR 颗粒度太细（avg 7-48 chars/block），单 block 上下文不足以构成可检索 chunk |
| `single_block_small` | 5 | 5.2% | 单 block 但 raw 长度 4K-5K（介于 chunk 上限附近），走 text 路径被 split 后有效 chunks < MIN_RECALL_CHUNKS |
| `few_blocks_any` | 4 | 4.2% | block 数过少但每个 block 又有结构问题 |
| `oversized_image_block` | 2 | 2.1% | image-dominated 文档，OCR 漏召 |

**期望产出**：doc-to-md 端按本文档的 **输出契约（§3）** 修复 → 我们重跑这 96 个 bundle → 期望 ≥ 90% 成功 ingest（暂定 acceptance threshold，留待讨论）。

---

## 1. 背景：EKRS chunker 接受什么输入

doc-to-md 当前输出 schema（per block）：`{ doc_id, block_id, type, content: { raw, structured, md_preview, ... }, metadata: { page_number, bbox, ... } }`。

EKRS chunker 对 table block 的处理路径（`rag/ekrs_rag/ingestion/chunker.py:622-732`）：

```
type == "table":
    headers = extract_table_headers(block)       # 取 structured[0] 或 md_preview 首行
    if block.content.structured (非空 list):
        rows = structured
        header_row = rows[0]
        data_rows  = rows[1:]
        # ... 按 row 累积到 max_tokens（默认 768 tokens ≈ 3072 chars）然后 _emit_buffer
        # 最终 flush：if current_parts and len(current_parts) > (1 if header_prefix else 0)
        #             _emit_buffer(current_parts)
    else:
        # 走 _split_text_two_phase(text=content.raw, max_tokens*4=3072 chars/cut)
        # — 多 chunk 输出
```

**关键陷阱**：当 `content.structured` 是非空 list 但 `data_rows = []` 时（例如 structured 只有一个标题行），for 循环不执行，最终 flush 条件 `len(current_parts) > 1` 不成立 → chunks = [] → block-level no_chunks 触发 → 整个 bundle 走到 `no_chunks` 终态。**chunkerr 没有"structured 退化到 md_preview"的兜底**，因为 structured truthy 就走结构化路径。

## 2. 失败分类详细数据

数据来源：磁盘结构 + `data.jsonl` 内联检查（GPU container 日志在 `docker compose ... down` 后已丢失，无法走 audit 路径）。

#### 2.1 `single_table_monolith`（61 bundles, 63.5%）

**形态**：1 个 block，type=table，content.raw 8253-14000+ chars（远超 3072 chars/cut 上限），`content.structured` 仅 1 行（标题 cell），`content.md_preview` 是完整 markdown 表但因为 structured truthy 走了结构化路径。

**典型样例**（`000150f86cdbc3c1` / PTA_EL-SYS-TF2-F.doc）：
```
content.raw = 10852 chars  ← markdown 表 + 重复列内容（pymupdf 解析异常）
content.structured = [["Electrical—Systems—High Potential DC Test - 5kV/15kV Cable"]]  ← 只有标题
content.md_preview = 10852 chars  ← 完整 markdown 表
```
chunkerr 行为：`headers = [title_cell]` → `header_prefix = title + "\n"` → `data_rows = []` → loop 不执行 → `len(current_parts) == 1`，not `> 1` → **0 chunks emitted**。

样例 doc_hashes：
```
000150f86cdbc3c1 (10852 chars)
018c941f9b1ee965 (11274 chars)
060f92ee18b68afc (8253 chars)
... 共 61 个
```

**doc-to-md 端根因**：`parsers/pdf_parser.py` + `backend/engine/renderer.py` 把多行 markdown 表折叠成单行 structured 时丢失了数据行（仅保留标题）。需要回溯：(a) pymupdf4llm layout 模式输出的表格 cell 解析；(b) structured 二维数组的填充逻辑。

**重要：根因不在表格"太大"**，单 block 内 14K chars 完全可以被 chunkerr 走 text fallback 切成多个 chunk。真正的失败原因是 structured 输出不完整——doc-to-md 输出了一个仅含标题的占位符 list，触发 chunkerr 的 0 chunks 终态。修复方向不是"拆分表格"，而是**修正 structured 输出契约**（详见 §3.3）。

#### 2.2 `tiny_content_fragmented`（11 bundles, 11.5%）

**形态**：5K-12K 个 block，每个 block 平均 7-48 chars。

**典型样例**（`01efb45935791d2c` / 危险化学品名录 2002版）：
```
content.raw[0] = "危险化学品名录（2002版）"  (14 chars)
total_blocks = 11882, avg_chars = 7
```
chunkerr 行为：每个 block 太短，无法形成有效 chunk → recall gate `MIN_RECALL_CHUNKS=1` 通过但有效 chunks < 实际块数 → ingest 整体 chunks = 0（threshold-driven）。

样例 doc_hashes：
```
01efb45935791d2c (11882 blocks, avg=7 chars)
19156a9af21c3d8b (9524 blocks, avg=48 chars)
3222bc05da12abe5 (5028 blocks, avg=22 chars)
... 共 11 个
```

**doc-to-md 端根因**：`ocr/client.py` 5 级降级链（glm-ocr → paddleocr-vl → mineru → odl → paddleocr）颗粒度切得太碎。需要在 corpus level 做合并：连续 N 个 block 且类型相同 + 文本总长 < 512 chars 时合并；或 OCR 后处理阶段加 `coalesce_fragments` 步骤。

#### 2.3 `mixed_type_large`（13 bundles, 13.5%）

**形态**：几百到几千个 block，type 分布混杂（`text + image + table`），单 block 内容正常但 block 分类边界有问题。

**典型样例**（`04740bb9fe2d20a3` / 工艺系统目录）：
```
total_blocks = 2071
types = {text: 1005, image: 1021, table: 45}
```
chunkerr 行为：走标准路径产生 chunks，但分类歧义导致检索时 precision 退化（不是 0 chunks 但属于误分类）。

样例 doc_hashes：
```
04740bb9fe2d20a3 (2071 blocks, text=1005 image=1021 table=45)
259d8d0a8d49e686 (6311 blocks, header=384 text=5789 table=97 image=41)
48affd58c599001e (247 blocks, image=18 text=229)
... 共 13 个
```

**doc-to-md 端根因**：image classification 阈值/启发式在中文 PDF 上有偏（1021 个 image 但大部分应该是 text-block OCR artifact）。

#### 2.4 其他（10 bundles, 10.4%）

- `single_block_small`（5）：raw 3946-4795 chars，单 block，text 路径 split 后有效 chunks < MIN_RECALL_CHUNKS。
- `few_blocks_any`（4）：block 数 3-1321 但每个 block 有结构问题。
- `oversized_image_block`（2）：`8ab548bb51c076d0`（3186 blocks, image=1984）+ `ad58aff523d8d880`（14451 blocks, image=9624），image-dominated doc，OCR 漏召。

---

## 3. doc-to-md 输出契约（建议新版本 ≥ v1.1）

为了避免 EKRS 端再遇到类似问题，建议 doc-to-md 输出符合以下契约。可作为 EKRS ↔ doc-to-md 接口的 §data contract 草案。

### 3.1 Per-block 契约

| 字段 | 要求 | 说明 |
|------|------|------|
| `content.raw` | 字符串，UTF-8 | 不再是 EKRS 主路径（structured 优先），但保留作为 fallback 完整性 |
| `content.structured` | `list[list[str]]` 或 None | **必须**包含表格的所有数据行。**禁止**只填标题行 |
| `content.md_preview` | 字符串 | 与 structured 一致；markdown 表首行必须是真正的列头（`\|---\|` 分隔线之上的那行），不能是标题 cell |
| `type` | `text` / `image` / `table` / `header` / `kv` | 类型必须与 structured 形态一致 |
| `metadata.token_count` | int | 建议填，EKRS 内部 `estimate_tokens` 近似 |

### 3.2 单 block 长度上限

- **`content.raw` ≤ 3072 chars**（EKRS `max_tokens * 4`）：超出时 doc-to-md 必须按行 split 成多个 block，每个 block 维持结构完整性（不能把一个完整 markdown 表 cell 截断到两个 block 里）。
- **`content.structured` 行数无上限**，但每 cell 内容 ≤ 1024 chars；超长 cell 在 raw/md_preview 里保留全量，但 structured 里按段落截断。

### 3.3 表格 block 硬约束

1. **`structured[0]` 必须是真正的列头行**（不是标题/合并 caption）。
2. **`structured[1:]` 必须是数据行**；如果有 caption/标题，请在 `metadata.caption` 字段单独存。
3. **若 structured 无法解析**，请把 `content.structured = None`（让 chunkerr 走 `_split_text_two_phase(md_preview)` fallback），不要强行填一个不完整的 list。
4. **若 markdown 表的列数 < 2**（单列布局的"伪表"），请把 block type 改成 `text`，让 chunkerr 走通用文本路径。

### 3.4 碎片化合并

- 当连续 N≥3 个 block 满足 `block.type` 相同 + 总 chars < 512 时，doc-to-md 应在输出前**合并**成一个 block。
- 在 `metadata.merged_from_block_ids` 字段保留来源 trace（EKRS 端无需解析此字段，但保 traceability）。

### 3.5 mixed-type bundle

- 单 bundle 内 `image` 类型 block 占比 > 60% 时，doc-to-md 应在 `index.json.doc_metadata` 加 `ocr_quality_warning: "image_dominant"` 标记。
- 单 bundle 内 `text/image/table` 三类**同时存在**且每类 ≥ 5% 时，加 `mixed_content: true` 标记。

---

## 4. 验证计划

### 4.1 doc-to-md 端

1. 按 §3 契约回填 96 个失败 bundle 的 `data.jsonl`（优先顺序：`single_table_monolith` 61 → `tiny_content_fragmented` 11 → `mixed_type_large` 13 → 其他 11）。
2. 写 1 个 corpus-level 校验脚本（`doc-to-md/scripts/validate_against_ekrs_contract.py` 或类似），在每次 batch 输出后跑一次，统计 `single_table_monolith_like` 数量。
4. bundle-level 单元测试：构造契约违反样例（`structured = [[title]]` / `raw > 3072` / 碎片 block），断言输出被拒绝或自动修复。

### 4.2 EKRS 端（联调）

1. doc-to-md 输出新版本后，**不重置 Qdrant**，用 `pipeline.ingest(version=3)` 把 96 个 bundle 增量入进去（pipeline 自动 dedup by `doc_hash + version`）。
2. 接受条件（acceptance threshold，按类别分级）：
   - **`single_table_monolith` 类别（61）**：必须 100%（61/61）成功 ingest。根因明确（structured 占位符），可全部修复。
   - **`tiny_content_fragmented` 类别（11）**：≥ 9/11（81%）成功 ingest。合并启发式可能有 edge case。
   - **`mixed_type_large` 类别（13）**：≥ 11/13（85%）。
   - **`single_block_small` + `few_blocks_any` + `oversized_image_block`（11）**：≥ 9/11（82%）。
   - **总体**：≥ 90/96（93.7%）成功 ingest；任何一个类别未达上面目标需逐 doc 复盘。
3. 验证脚本：`scripts/c13_failed_bundles_verify.py`（待写），输入 96 个 doc_hash，输出每个的 ingest status + 按类别聚合。
4. 不要求 EKRS 端改 chunker：本批失败是 doc-to-md 输出契约违反，不是 EKRS bug。

### 4.3 灰度路径

- Phase 14（或单独补丁）：doc-to-md 新版本输出后，**先用一个 50-bundle 试跑 bundle subset**（推荐先用 `single_table_monolith` 的前 10 个 + `tiny_content_fragmented` 前 5 个）→ 验证 EKRS ingest 成功 → 再扩到全 96。
- 不动 GPU 部署：96 bundles 走 CPU ingestion 足够（chunkerr 路径和 GPU 无关；GPU 只在 encode 阶段）。

---

## 5. 待讨论问题

1. **§3.4 碎片合并阈值 512 chars** 是否合理？EKRS 端 `MIN_RECALL_CHUNKS=1` 是个粗阈值，更倾向 doc-to-md 端做内容合并。
2. **§3.3.4 "伪表 → text"** 的判断规则（列数 < 2）够不够？需要 doc-to-md 看更多失败样本才能确认。
3. **§4.2 acceptance threshold 93.7%** 是不是过松？按现状 `single_table_monolith` 100% 是必须的；剩下 35 个容许 ≤ 3 个失败。
4. **是否需要 EKRS 端加 `structured_rows == 1` fallback**？决策点（依赖 doc-to-md 修复时间承诺）：
   - **若 doc-to-md 短期（≤ 1 周）能修复** §3.3 的 structured 输出契约 → EKRS 端**不加** fallback，让根因在源头解决。
   - **若 doc-to-md 修复延期或优先级低** → EKRS 端**加临时 fallback**：`if block.type == "table" and len(structured) == 1: treat as text and fall through to _split_text_two_phase`。明确标 `quality_warning="structured_degraded_to_text"`，document 这不是根治而是临时 work-around，等 doc-to-md 修复后**应回滚**。
   - **决策时机**：doc-to-md 在评审本文档后给出修复时间估计，EKRS 根据该估计点决定是否动 chunker。
5. **`oversized_image_block` 2 个** 类别（image-dominated OCR 漏召）是 doc-to-md OCR 端的问题，不是输出契约问题。是否单独立项跟踪？

---

## 6. 时间线与责任

| 节点 | 责任方 | 内容 |
|------|-------|------|
| 2026-08-27 (today) | EKRS | 本协调文档提交 |
| TBD | doc-to-md | 评审 §3 契约 + 回复 §5 问题 |
| TBD | doc-to-md | 修复 `single_table_monolith` 类别（61 个 bundle 的根因，最优先） |
| TBD | doc-to-md | 输出 v1.1 schema + validation script |
| TBD | 联调 | 灰度 ingest 50-bundle subset |
| TBD | 联调 | 全 96 重 ingest + verify script |
| TBD | EKRS | Qdrant v=3 入库确认（pipeline 自动 dedup） |

## 7. 失败 bundle 明细（要求 doc-to-md 修复并重新解析）

**重要**：以下 96 个具体文件 doc-to-md 需按本文档 §3 契约**修复并重新解析**（不只是给样例）—— 修复后用 v1.1 schema 输出，EKRS 端用 `version=3` 增量 ingest。

完整清单已导出到：

**`deployment/phase13c-c13-failed-bundles-manifest.json`**（gitignored，95 KB）

每条记录字段：`doc_hash`, `file_name`, `file_type`, `total_blocks`, `doc_type`, `category`, `reason`。

按类别速览（文件名供识别）：

#### 7.1 single_table_monolith（61 个）— 优先级最高

修复方向：§3.3 表格 block 硬约束（structured 必须包含完整数据行）。

```
PTA_EL-SYS-TF2-F.doc                                         1 block,   10852 chars
PTA_PS-FES-00-D.doc                                          1 block,   11274 chars
PTA_PS-FES-PA-F.doc                                          1 block,    8253 chars
PTA_MM-PROC-FO-09-A.doc                                      1 block,   13720 chars
...（共 61 个，全部 .doc / .pdf / .docx，1 个 block）
```

完整 manifest 文件 `categories.single_table_monolith[*]` 列出了全部 61 个 doc_hash + file_name，doc-to-md 可直接遍历。

#### 7.2 mixed_type_large（13 个）

修复方向：image classification 阈值（image 占比疑似偏高）+ §3.5 mixed-type 标记。

```
石油化工土建等工程图解英汉词汇说明.pdf     2071 blocks  text=1005 image=1021 table=45
API RP 684-2005 (2010)英文原版标准.pdf     6311 blocks  header=384 text=5789 table=97 image=41
2017 API Catalog.pdf                       247 blocks   image=18 text=229
...（共 13 个）
```

#### 7.3 tiny_content_fragmented（11 个）

修复方向：§3.4 碎片合并（连续 N≥3 同类型 block、总长 < 512 chars 时合并）。

```
Hazardous  Chemical Cataglogue.doc                  11882 blocks  avg=7 chars
python cookbook(第3版)高清中文完整版(###).pdf       9524 blocks   avg=48 chars
VEN-LOGIC-06   UTILITY & INSTRUMENT AIR COMPRESSOR  5028 blocks  avg=22 chars
...（共 11 个，多为 OCR-heavy 中文 PDF）
```

#### 7.4 其他（11 个）

- `single_block_small`（5）：raw 3946-4795 chars，建议§3.2 上限检查时把 text-path block 也纳入（当前只对 table 有效）。
- `few_blocks_any`（4）：block 数少且每个有结构问题，需逐 doc 复盘。
- `oversized_image_block`（2）：OCR 端漏召，建议**单独立项**跟进，不在本文档契约范围。

## 8. 交付期望

doc-to-md 端需交付：

1. **v1.1 schema patch**：实现 §3 契约，写 `parsers/pdf_parser.py` + `backend/engine/renderer.py` 的修复 PR。
2. **`validate_against_ekrs_contract.py`**：每次 batch 输出后跑的校验脚本，统计 `single_table_monolith_like`（structured 占位符）数量，输出非 0 时 fail-fast。
3. **96 个 bundle 重解析产物**：用 v1.1 schema 输出新 `data.jsonl`，覆盖 `/mnt/disk/text/{doc_hash}/data.jsonl`（或写到 `/mnt/disk/text-v1.1/{doc_hash}/` 由 EKRS 切换 `SHARED_STORAGE_PATH`）。
4. **回复 §5 五个问题**：含修复时间估计（决定 §5.4 EKRS 是否动 chunker）。

EKRS 端承诺：

1. v1.1 输出到位后**不重置 Qdrant**，`pipeline.ingest(version=3)` 增量 ingest。
2. 跑 `scripts/c13_failed_bundles_verify.py`，按 §4.2 验收阈值出 report。
3. 若验收未达 93.7%，逐 doc 复盘。

---

**版本**：v1.1（2026-08-27，根因表述 + 验收分级 + manifest 交付清单）
**状态**：OPEN，等待 doc-to-md 评审 + 修复时间承诺

---

**版本**：v1（2026-08-27）
**状态**：OPEN，等待 doc-to-md 评审