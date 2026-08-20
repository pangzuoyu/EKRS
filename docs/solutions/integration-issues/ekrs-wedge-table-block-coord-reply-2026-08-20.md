---
title: "doc-to-md → EKRS: table block O-6 wedge fix landed (commit 446d5df, 2026-08-20)"
date: 2026-08-20
category: docs/solutions/integration-issues
module: rag-integration
status: closed — fix shipped
related_request: ekrs-wedge-table-block-coord-request-2026-08-20.md
---

# doc-to-md → EKRS: table block O-6 wedge 修复完成

> **TL;DR**: O-6 已落地. 老 bundle 已 fix-in-place 修回填. 新生成 bundle 走修好的 parsers 不再 wedge. commit `446d5df`.

---

## 一、已做的事

### Q1. CONSTRAINTS O-6 落地

**根因**: `parsers/docx_parser.py` 把 `type="table"` 块的 `content` 序列化成 flat markdown 字符串. `pipeline/parsers/docx_parser.py:66` + `pipeline/orchestrator.py:1004` 见 string 类型强制 `structured=None` → EKRS chunker `chunker.py:560` 的 `if block.content.structured and isinstance(...)` 检查失败 → 走 `_split_text_two_phase` fallback → 单 cell 重复表被切成 ~1340 个 ≤500-token chunks → RAG ingest wedge.

**修复** (`commit 446d5df`):

| 文件 | 改动 |
|---|---|
| `parsers/docx_parser.py` | table block emit 改为 dict `{raw, md_preview, structured: grid}`, grid 来自已有的 `[[cell.text for cell in row.cells] for row in table.rows]` (line 61) — 之前 `_grid_to_markdown(grid)` 后丢了 grid |
| `parsers/pdf_parser.py` | markdown pipe 路径 (line 1158) 之前显式 `structured=[]`, 改为调 `markdown_pipe_to_structured` 回填. HTML 路径已有的 `_html_table_to_structured` 不动 |
| `parsers/utils.py` (新) | `markdown_pipe_to_structured(md_text)` 反向 parser: GFM separator, 无 separator wide 表, cell 含 `\|` 转义, 空 cell 行误判 separator (4 个 corner case, 见 §三) |
| `tests/test_table_structured_o6.py` (新) | 11 tests: 6 unit + 2 DOCXParser + 1 pipeline.parse_docx + 2 audit smoke |
| `scripts/audit_structured_none.py` (新) | 扫 corpus, 支持 `--emit-wedge-list` (≥5000 chars wedge) + `--emit-all-violations-list` |
| `scripts/repair_wedge_bundles.py` (新) | fix-in-place 回填现存 bundle 的 structured (无需源文件) |

### Q2. 大 block 提前拒收 (暂不实施)

单 block raw > 100k 字符的 hard cap 暂不实施. 原因:
- 当前 fix 已让所有 wedge (≥5000 chars) structured 非空, EKRS 走 row-iteration 而不是 text-fallback, 1340 chunk 路径不再触发.
- 加 hard cap 需协调 EKRS 端 "block too large" 异常处理, 当前 PR scope 外.
- 后续若再观察到 bge-m3 encode 阻塞, 在 chunker 加 `if len(_split_text_two_phase output) > 500 chunks: log.error("skip")` 即可, 不需要 doc-to-md parser 层 hard cap.

### Q3. 267 个 wedge bundle 重处理

不重跑 parser, 改 fix-in-place (`scripts/repair_wedge_bundles.py`):
- 读取 `<output-dir>/text/<bundle_id>/data.jsonl`
- 对 `type=table` 且 `structured in (None,)` 的 block, 调 `markdown_pipe_to_structured(content.raw)` 回填
- 原子写回 `data.jsonl` (`.tmp` + rename)
- 不动 `raw` / `md_preview` 字段 (下游 markdown 渲染器依赖)

本机 corpus 验证 (`/home/pangzy/code_project/doc-to-md/output`, 3809 bundles):
- audit 前: **13244 violations, 761 wedge bundles**
- 第一次 repair (wedges ≥5000 chars): 2749 blocks fixed
- 第二次 repair (remaining wedges): 739 blocks fixed
- 第三次 repair (all O-6 violations): 291 blocks fixed
- audit 后: **3790 violations, 0 wedge bundles**

剩余 3790 violations 集中在 pathological 输入 (raw 不是 markdown pipe 或 cell 全空 grid), raw < 5000 chars → 不会触发 EKRS 1340-chunk wedge. 不在本次 P1 范围.

---

## 二、EKRS 侧的建议操作

```bash
# 1. 重跑 audit 确认本地 corpus wedge=0
python3 /home/pangzy/code_project/doc-to-md/scripts/audit_structured_none.py \
    --output-dir /home/pangzy/code_project/doc-to-md/output

# 2. 用 EKRS 端的 wedge bundle 列表跑 repair (fix-in-place, 无需源文件)
python3 /home/pangzy/code_project/doc-to-md/scripts/repair_wedge_bundles.py \
    --wedge-list /tmp/ekrs_wedge_bundle_ids.txt

# 3. 重跑 ingest_new_bundles.py (移除 --pre-filter-wedges workaround)
python /home/pangzy/code_project/EKRS/scripts/ingest_new_bundles.py
```

---

## 三、`markdown_pipe_to_structured` corner case 设计

```python
def markdown_pipe_to_structured(md_text: str) -> List[List[str]]:
    """4 corner case 处理:
    - GFM separator: | --- | --- | ... | (有 '-' 才认 separator)
    - 无 separator: 老 docx_parser wide 表常见, 不严格要 separator
    - 转义 pipe: cell 内 \| 保留不切分 (CommonMark)
    - 空 cell 行: '|  |  |' 不被误判 separator (要求 '-' 存在)
    """
```

每个 corner case 有对应 unit test (`tests/test_table_structured_o6.py::TestMarkdownPipeToStructured`).

---

## 四、关联

- 修复 commit: `446d5df` (本 commit, 6 files / +765 / -3)
- 上游 coord 请求: `ekrs-wedge-table-block-coord-request-2026-08-20.md`
- 验证脚本: `scripts/audit_structured_none.py`, `scripts/repair_wedge_bundles.py`
- 单元测试: `tests/test_table_structured_o6.py` (11 tests, all pass)
- 不变量: `CONSTRAINTS.py:9 O-6` (table 必有 structured, kv 必有 structured dict)

---

## 五、未决问题

- 3790 remaining violations 在 doc-to-md 本机 corpus. 这些是 raw 不是 markdown pipe 或 cell 全空的 pathological 输入. EKRS 端 raw < 5000 chars 时不触发 wedge, 暂不修. 若 EKRS 在 ingest 时观察到 bge-m3 encode 异常缓慢 (e.g. >2 min/block), 用 `audit --emit-json` 输出 violation 列表, 我们挑 raw ≥1000 char 的非空样本单独写 parser 增强 (e.g. HTML `<table>` 但 _html_table_to_structured 返回 [] 的 PDF 路径).
- `DS016.doc` 源文件本身的 OCR 退化 (268 行同 cell "A: AIR COMPRESSOR") 仍存在. 当前 fix 让 EKRS 不 wedge 但检索召回仍然无意义 (每个 chunk 都是同一字符串). 建议 EKRS 在 ingest 时加 source-quality filter: 全行 cell 相同的表直接 drop 或合并到 single description chunk. 这部分属 EKRS 端 ingest 策略, 不在 doc-to-md 范围.
- `scripts/ingest_new_bundles.py` 的 `--pre-filter-wedges` workaround 现在技术上不必要 (本地 0 wedges). 是否移除由 EKRS 侧决定 — 本 PR 不动 EKRS 代码.

---

## 六、EKRS 侧补充：内部已加固 row-flush 逻辑

> **2026-08-20 追加** (本节由 EKRS 侧补, 不影响 doc-to-md O-6 fix 状态)

### Q6. EKRS chunker row-flush 修复已落地

`rag/ekrs_rag/ingestion/chunker.py:_split_large_block` 历史上对单行 token 数超过 `max_tokens` 的 pathological 表 (e.g. `97bc380d566b681b` 267 行 × ~1200 tokens) 处理有 bug: 单行被 append 到 buffer 而不 flush, 累积到下一次 flush 时产生 1500-2200 tokens 的超大 chunk, bge-m3 ONNX encode 卡在单核 100% 直至 600s status timeout 失效.

**修复内容** (commit 在 EKRS 侧, 不需要 doc-to-md 改动):

| 修复 | 位置 | 行为 |
|---|---|---|
| Pre-check `row_tokens > max_tokens` | `_split_large_block` row-iteration 前置 | 单行超 cap → flush 当前 buffer + 用 `_split_text_two_phase` 强制拆分该行, 子 chunk 每个 ≤ max_tokens |
| Post-flush re-check `_emit_buffer` | `_split_large_block` 所有 flush 路径 | join 后 buffer 仍 > max_tokens → 强制拆分而非单 chunk emit (e.g. header + near-cap row 合并超 cap) |
| `Chunk.quality_warning: bool = False` 字段 | `shared/ekrs_shared/models.py:227` | 强制拆分的子 chunk 标记 `quality_warning=True` (source-quality hint for retriever — 数据仍入库, retriever 可降权不丢) |
| 动态 status timeout (Patch 2) | `scripts/ingest_new_bundles.py:estimate_status_timeout` | `timeout = max(600, max(1, total_raw_chars // 500) * 0.8)`. 32/2249 pathological bundle 自动扩到 600-3758s. 正常 bundle 仍 600s floor |

**已取消的 workaround**:
- ~~数据预跳过 (raw>50K 直接 exclude)~~ — 完全不必要. 充要条件是 chunker row-flush fix. 数据是否入库由数据质量决定, 不由处理代码的容错能力决定. 575 raw_not_str bundle 也走 ingest (IR-reject 走 Q4 通道, 不归本修复范围).

**净数据丢失风险**: 0.

**验证**:
- `rag/tests/unit/test_chunker_row_flush_fix.py` 5 tests pass (normal rows / single oversized row force-split / pathological 267-row no-wedge regression guard).
- 完整 chunker suite 82 tests pass (含 5 新增), mypy 干净.

### Q7. doc-to-md 侧无后续动作

本次 Q1-Q5 协调请求全部由 doc-to-md 侧已落地 (commit `446d5df`) 或已 ack (raw=list O-7 → `ekrs-raw-list-bug-coord-request-2026-08-20.md` 跟踪). EKRS 侧 row-flush 修复不依赖 doc-to-md 后续修复. 即便 doc-to-md 输出继续产生 pathological 表, EKRS chunker 已自防御, 不再 wedge.

---

**doc-to-md 侧 status**: O-6 fix shipped, 0 wedges on local corpus.
**EKRS 侧 status**: row-flush fix shipped, dynamic timeout shipped, quality_warning field shipped, pre-skip workaround cancelled, ingest resumed from checkpoint.
**EKRS 侧 action**: 重跑 repair + ingest, 关闭 `phase12-wedge-173k-table-block` buglog.