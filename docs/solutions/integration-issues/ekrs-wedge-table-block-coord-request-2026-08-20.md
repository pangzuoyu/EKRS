---
title: "doc-to-md → EKRS: table block wedge (173k tokens, structured=None) — 修复请求"
date: 2026-08-20
category: docs/solutions/integration-issues
module: rag-integration
problem_type: ingestion_wedge
component: chunker._split_large_block + IR doc-to-md table writer
severity: P1 (silently blocks ingestion of 267 / 3033 new bundles; no data loss but pipeline wedges indefinitely)
target_audience: doc-to-md team
status: open — request
ekrs_actions_pending: none (workaround in scripts/ingest_new_bundles.py: pre-filter wedges)
related_constraint: doc-to-md/CONSTRAINTS.py O-6 (content.structured 类型正确)
---

# doc-to-md → EKRS: table block 缺 `content.structured` 导致 RAG wedge

> **TL;DR**: 3033 个新 bundle 中有 **267 个**含 `type="table"` 但 `content.structured=None` 的 block。EKRS chunker 走 fallback `_split_text_two_phase` 把单 block 切成 ~1340 个 ≤500-token chunks, bge-m3 encode + Qdrant upsert 阻塞单 worker → pipeline 永不写 TaskRepo → `/v1/ingestion/status` 永远 404 → ingest script 600s timeout 死循环. **doc-to-md 侧需保证 O-6 (CONSTRAINTS.py:9) 落地** — `type="table"` 必须有 `content.structured` 二维数组.

---

## 一、具体 wedge 例子

**Bundle**: `/home/pangzy/code_project/doc-to-md/output/text/97bc380d566b681b/`
**Source**: `DS016.doc` (216 KB, mtime 2005-02-21, Word 97-2003 .doc → .docx 转换)
**Block 1 个**:

```json
{
  "block_id": "fbaaee0e-86c2-40a3-875b-b694d300ff92",
  "type": "table",
  "content": {
    "raw": "| 1 | A: AIR COMPRESSOR | A: AIR COMPRESSOR | ...",
    "md_preview": "| 1 | A: AIR COMPRESSOR | ... (693,907 chars)",
    "structured": null,        ←←← 违反 O-6
    "formulas": [],
    "md_preview": "..."
  }
}
```

**Block 体量**: 693,907 字符 / ~173,476 估算 tokens / 268 行（最长行 9,796 字符）
**Block 性质**: 单 cell 重复（OCR 退化产物 — 列里全是 "A: AIR COMPRESSOR"）

---

## 二、wedge 在 EKRS 侧的发生路径

`rag/ekrs_rag/ingestion/chunker.py:797-811`（Boundary 1 → table 块）：

```python
block_tokens = estimate_tokens(text)        # 173,476
if block_tokens > max_tokens:               # 173,476 > 500 ✓
    logger.info("Large %s block ... (%d tokens), splitting", ...)  # 最后一行 log
    chunks.extend(_split_large_block(...))  # ← 入口
```

`_split_large_block` (`chunker.py:547`) 第一分支（line 575）：

```python
if block.content.structured and isinstance(block.content.structured, list):
    # rows = structured; 按行迭代 — 268 路径, 快
    ...
else:
    # ←←← structured=None 落到这里 (line 629-639)
    chunks.extend(_split_text_two_phase(text, ...))   # 1340 chunks
```

`_split_text_two_phase` 把 268 行 × ~5 fragments/行 → **~1340 个 ≤500-token chunks**，单 worker pipeline 后续 bge-m3 encode (intra_op=4) × 1340 = ~10 min/block + Qdrant upsert × 1340。**`TaskRepo` 在 pipeline 完成时才写 `status=success`**，期间 `/v1/ingestion/status` 持续返回 404。

`scripts/ingest_new_bundles.py` `poll_status(timeout=600s)` 600s 后判 timeout → 下一轮 notify 堆积 → 永久 wedge。

---

## 三、影响面

| 维度 | 数 |
|---|---|
| Corpus 总 bundles | 3809 |
| 已在 Qdrant | 1859 (Phase 12 Task D 745 + 后续增量) |
| **新 bundle 待入库** | 3033 |
| **wedge bundle（structured=None & block > 5000 tokens）** | **267 (8.8%)** |
| 安全 bundle（已过滤） | 2558 — 正在后台 ingest（bk2lxt2jg） |
| wedge top 10 体量 | 173k / 49k / 38k / 34k / 25k / 23k / 19k / 17k / 16k / 16k tokens |

---

## 四、请求 doc-to-md 修复的事项

### Q1. 落地 CONSTRAINTS O-6

> `CONSTRAINTS.py:9: O-6 content.structured 类型正确（table 为二维数组，kv 为字典）`

**预期行为**: `type="table"` 必须产出 `content.structured: list[list[CellValue]]`（行 × 列）。`type="kv"` 必须产出 `content.structured: dict[str, str]`。`structured=None` 只允许出现在非 table/kv block（paragraph、heading）。

**修复路径**:
1. 在 Word/.docx parser 里检测到 `type=table` 但 `structured is None` 时，要么 fallback 到 markdown-row parsing（`md_preview.split("\n")` 解析 `|` 分隔），要么 fail-loud 输出 ingest-warning（不入 EKRS）。
2. 加单元测试: 给 fixture `DS016.doc`，断言解析后的 `data.jsonl` 里 `fbaaee0e-86c2-40a3-875b-b694d300ff92.content.structured` 是 list of list。
3. 写 `scripts/audit_structured_none.py` 扫全 corpus 输出违反 O-6 的 bundle 清单（应该覆盖这 267 个 wedge + 任何历史 bundle）。

### Q2. 大 block 提前拒收 (可选，更稳)

> 单 block raw 长度 > 50,000 字符时, parser 应主动 fail 而非 wedge 下游。

EKRS chunker 的 max_tokens=500 对应 ~2000 chars，硬切后理论安全。但 `_split_text_two_phase` 走完整文本路径（per-line hard cut + greedy merge）后产生 chunks 数 ≈ `len(text) / 2000`。173k tokens → ~1340 chunks → bge-m3 wedge 阈值 ≈ 500+ chunks。建议:

- doc-to-md parser 加 sanity check: 单 block raw > 100k 字符 → raise ParseError("block too large; please refactor source PDF/DOC before ingest")
- 或加 chunk-count 上限: parser 自己按每 2000 字符 pre-split 成多个 block (chunker 不感知 multi-block boundary)

### Q3. 历史 267 个 wedge bundle 重新入库

修复 doc-to-md parser 后，对这 267 个 bundle 重跑 `output/text/` 生成：

```bash
# 仅重新生成 wedge bundle（避免重处理已入库的）
python /home/pangzy/code_project/doc-to-md/scripts/reprocess_bundles.py \
    --input-list /tmp/wedge_bundle_ids.txt \
    --output-dir /home/pangzy/code_project/doc-to-md/output/text
```

然后 EKRS 侧用新脚本（已写好 `scripts/ingest_new_bundles.py`）跑：

```bash
# 在新 bundle 修复完成后
python scripts/ingest_new_bundles.py \
    --include-list /tmp/new_wedge_reprocessed.json
```

---

## 五、EKRS 侧已做的 workaround

为不阻塞新 bundle 入库：

1. 写了 `scripts/ingest_new_bundles.py` (commit pending) — 复刻 `task_d_mvp_reingest.py` 但只处理 `--include-list` 指定的 bundle 子集。
2. 在 ingest 前过滤：检测 `content.structured is None AND any(content.raw // 4 > 5000)` → 跳过。2558 个安全 bundle 后台正在跑（task `bk2lxt2jg`）。
3. Buglog: `.wolf/buglog.json` 新增 `phase12-wedge-173k-table-block`，等 doc-to-md 修后 close。

---

## 六、验证 doc-to-md 修复完成的标准

- [ ] `scripts/audit_structured_none.py` 输出 0 违反 O-6 的新生成 bundle
- [ ] DS016.doc (97bc380d566b681b) 重新解析后 `content.structured` 是二维数组
- [ ] 267 个 wedge bundle 重新入库成功（success chunks 数 > 0, 无 timeout）
- [ ] EKRS wedge count = 0，新 ingest 速度不再被 1340-chunk block 拖慢

---

## 七、未决问题

- `DS016.doc` 是否本身就是 OCR 退化的（"A: AIR COMPRESSOR" 单 cell 重复 268 行）？如果是源文件问题，doc-to-md parser 即便填上 `structured` 也只是反映退化数据，EKRS 检索召回无意义。建议在 re-ingest 前先做 source-quality filter（268 行同 cell 表直接 drop 或合并到 single description chunk）。
- 是否需要 EKRS 侧补 chunker hard-cap：`if len(_split_text_two_phase output) > 500 chunks: log.error("block unprocessable, skip")`，与 doc-to-md parser 的 O-6 落地互为冗余。

---

**EKRS 侧 status**: workaround 已 ship, 等 doc-to-md 落地 O-6 后 close。
**回复请写**: `/home/pangzy/code_project/EKRS/docs/solutions/integration-issues/ekrs-wedge-table-block-coord-reply-2026-08-XX.md`