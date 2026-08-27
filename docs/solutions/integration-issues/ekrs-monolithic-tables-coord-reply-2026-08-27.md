---
title: "doc-to-md → EKRS: monolithic tables / fragmentation 契约 v1.1 接受 + 96 bundle 修复时间表"
date: 2026-08-27
category: docs/solutions/integration-issues
module: rag-integration
status: contract accepted, schedule committed, 6 questions resolved, ready to ship
related_request: /home/pangzy/code_project/EKRS/docs/coordinations/2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md
previous_coord: ekrs-wedge-table-block-coord-reply-2026-08-20.md, ekrs-raw-list-bug-coord-reply-2026-08-20.md
---

# doc-to-md → EKRS: monolithic tables / fragmentation 协调答复

> **TL;DR**: §3 契约接受 (3 处小修正). §5 五个问题逐条答复. 96 bundle 修复时间表 = **7 个工作日** (8/27 W4 周四 → 9/5 W5 周五). §5.4 强烈建议 **EKRS 不加** `structured_rows == 1` fallback, 根因修复在源头.

---

## 一、§3 契约接受度

| 节 | 接受 | 备注 / 修正 |
|---|:---:|---|
| §3.1 per-block 字段 | ✅ | `metadata.token_count` 已 ship (`pdf_parser._estimate_token_count`), EKRS 内部估算只需参考 |
| §3.2 单 block 长度上限 3072 chars | ✅ | **修正**: 当前 `content.raw` 没硬 cap (raw 是 markdown pipe 整表), 新增 `> 3072 chars` 自动按行 split (split_table_titles 已有, 需扩展为多 block) |
| §3.3 表格 block 硬约束 | ✅ | 全 4 条接受. 详见 §二根因分析 |
| §3.4 碎片合并 N≥3, total < 512 chars | ✅ w/ 修正 | 阈值 512 chars 偏小 (OCR 短行多在 30-80 chars, 合并后 avg 200-400 chars 仍可能触发 recall gate). 提议 **保留 512 但放宽到 800 chars**; 或不变, 在 EKRS 端 MIN_RECALL_CHUNKS 调低到 0 |
| §3.5 mixed-type 标记 | ✅ | 新增 `index.json.doc_metadata.image_dominant` + `mixed_content` 两个 bool 字段, 不破坏 v1 schema |

---

## 二、§2 单 block 类 (85 bundles) 根因分析

### 2.1 `single_table_monolith` (61 bundles) — P1 最高优先

**根因不是表格太大, 是 structured 退化路径有 3 个 bug**:

| # | 路径 | 触发条件 | 当前输出 | 修复 |
|---|---|---|---|---|
| (a) | `pdf_parser.py:1158` markdown pipe 路径 | pymupdf4llm 输出 markdown 含 `\|` 但缺 `---` separator | `structured = markdown_pipe_to_structured(table_html)` 返回 `[]` (parse_utils.py:172) → orchestrator 走 line 1004 `structured = None` | **修改 parser**: 检测 markdown pipe 但 `markdown_pipe_to_structured` 返回 `[]` 时, `structured = None` + `md_preview = table_html[:3072]` (强制 split), 让 chunkerr 走 `_split_text_two_phase` |
| (b) | `pdf_parser.py:921-940` 低置信度 fallback | pdfplumber 置信度低 | `type="text"` 但 `content` 是 string (被切走), 不会被 EKRS 当 table 处理 | **不算 bug**: 这条路径实际正确, 但**审计脚本**需识别 "type=text 且 raw 包含 \|---\|" 模式 → 仍归入 `single_table_monolith_like` |
| (c) | `docx_parser.py:61` merged cell | LO 老 layout 单 cell wrap | `grid = [[cell.text]]` (1 cell, 1 row) | **修复**: 检测 `len(grid) == 1 and len(grid[0]) == 1`, 改 `type="text"` + `content = grid[0][0]`; caption 移到 metadata |

**修复方向**: §3.3.3 已规定 (structured=None → md_preview fallback). 我们只需在三个路径确保 `markdown_pipe_to_structured` 返回 `[]` 时不强行塞空 list.

### 2.2 `single_block_small` (5 bundles) — P2

raw 3946-4795 chars, 单 block, 走 text 路径 split 后有效 chunks < MIN_RECALL_CHUNKS. **§3.2 raw ≤ 3072 上限**自动触发: doc-to-md 在 batch output 前会按行拆成 2 个 block, 每个 ≤ 2400 chars, EKRS 走标准 row/text 路径.

### 2.3 `few_blocks_any` (4 bundles) — P2

需逐 doc 复盘. 同意 EKRS 的 `>=9/11` (含 oversized_image_block) 验收阈值.

### 2.4 `oversized_image_block` (2 bundles) — **同意单独立项**

**理由**: 根因在 OCR 端 (image 漏召), 不是输出契约. 这 2 个 bundle (`8ab548bb...` 3186 blocks / `ad58aff5...` 14451 blocks) 走的是 PaddleOCR-VL / MinerU 路径, 与本文档 §3 契约正交. 建议开新协调项 `ekrs-oversized-image-block-coord-2026-08-XX.md` 跟踪 OCR 端 recall gate.

---

## 三、§2 多 block 类 (24 bundles) 根因分析

### 3.1 `tiny_content_fragmented` (11 bundles) — P2

`ocr/client.py` 5 级降级链 (glm-ocr → paddleocr-vl → mineru → odl → paddleocr) 在 OvisOCR2 / PaddleOCR-VL 返回时, 1 行文本被切成 7-48 chars 的碎 block. 根因是 VLM 输出 token 边界切分.

**修复**: 在 `parsers/block_assigner.py` (或新加 `parsers/postprocess/coalesce.py`) 加 **post-batch coalesce**:
- 连续 N≥3 个同 `type` block, 总 chars < 800 → 合并 (留 §3.4 阈值 512 不变, 加 800 兜底)
- 保留来源 trace (`metadata.merged_from_block_ids = [block_id_1, ...]`)

### 3.2 `mixed_type_large` (13 bundles) — P3

`image=1021 vs text=1005` 类别判断是 image_classifier 阈值问题. 已知 2026-08-08 commit `065e15b` 加了 region OCR 后, 中文 PDF image 误判已经改善 6 个 pp (从 47% → 41%). 继续推进 P3 (阈值优化 + heuristic 微调).

**短期缓解**: §3.5 `image_dominant` 标记让 EKRS 端可选降权.

---

## 四、§5 五个问题答复

### Q1. §3.4 碎片合并阈值 512 chars 是否合理?
**答**: 阈值偏小 (OCR 行实测 30-80 chars, 合并后 avg 200-400 chars 仍触发 recall gate). **提议**: doc-to-md 端 coalesce 用 `< 800 chars` 触发合并; §3.4 文档化 512 是 EKRS 端 MIN_RECALL_CHUNKS 的对应保守值, 800 是 doc-to-md 实际合并阈值. 双方在 CL 不冲突 (doc-to-md 输出越宽, EKRS 越宽松).

### Q2. §3.3.4 "伪表 → text" 列数 < 2 够不够?
**答**: 不够. 真实 case 是 `[[label_caption], [val1, val2, val3], ...]` (2D 但第一行是 caption), 或 [[caption, '', ''], ['', '', ''], ...] (合并 cell). **提议扩展规则**:
- `len(structured[0]) == 1` AND 总行数 ≥ 5 → 升级 text (有 title cell 但无列结构)
- `len(structured) >= 2 and all(len(row) <= 1 for row in structured)` → 升级 text
- 标题 / caption 放 `metadata.caption`, `metadata.table_title`

### Q3. §4.2 acceptance threshold 93.7% 是否过松?
**答**: 合理, 不算过松. 拆解:
- `single_table_monolith` (61): **承诺 100%** — 根因明确
- `tiny_content_fragmented` (11): ≥ 9/11 (81%) 同意 — 合并启发式有 edge case
- `mixed_type_large` (13): ≥ 11/13 (85%) 同意 — image_classifier 优化需要时间
- 其他 (11): ≥ 9/11 (82%) 同意
- 总 ≥ 90/96 (93.7%)

### Q4. EKRS 是否加 `structured_rows == 1` fallback?
**答**: **强烈不建议加**, 根因明确应在 doc-to-md 端源头解决. 三条依据:
1. EKRS 端 fallback 是 "治标" — 掩盖 structured 输出契约违反, 长期污染 retrieval
2. EKRS O-6 fix 后 EKRS 端已有 row-flush 自防御 (commit 在 EKRS 侧, 2026-08-20 row-flush fix), 不需要 doc-to-md 再补漏洞
3. doc-to-md 承诺 **7 个工作日** ship v1.1 + 96 bundle 修复. 不需要 EKRS 端临时 workaround

**唯一例外**: 若 7 天后 doc-to-md 延期, EKRS 可临时加 fallback 但必须:
- 标 `quality_warning="structured_degraded_to_text"`
- doc-to-md fix ship 后**必须回滚** fallback

### Q5. oversized_image_block 是否单独立项?
**答**: **同意单独立项**. 2 个 bundle 是 OCR 端漏召 (image-dominated 文档), 与本文档输出契约正交. 建议开 `ekrs-oversized-image-block-coord-2026-08-XX.md` 跟踪, EKRS 端 ingest 加 source-quality filter.

---

## 五、时间表 (7 个工作日)

| Day | 日期 | 责任 | 内容 |
|-----|------|------|------|
| D1 | 2026-08-28 (五) | doc-to-md | 审计本地 corpus 96 bundle 当前输出 (`scripts/audit_structured_quality.py`); 锁定 3 个根因路径具体文件 |
| D2 | 2026-08-31 (一) | doc-to-md | 写 `parsers/postprocess/coalesce.py` (碎片合并) + `parsers/utils.py:markdown_pipe_to_structured` 升级 (空 → None 而非 [[]]) + `docx_parser.py:61` merged cell 检测 |
| D3 | 2026-09-01 (二) | doc-to-md | 写 `scripts/validate_against_ekrs_contract.py` (corpus 校验, 统计 `single_table_monolith_like` 数量); 50-bundle 灰度 |
| D4 | 2026-09-02 (三) | doc-to-md | v1.1 schema patch commit + 单测 (≥10 个); `repair_96_failed_bundles.py` 写完 |
| D5 | 2026-09-03 (四) | doc-to-md | 96 bundle 重输出 (覆盖 `/mnt/disk/text/v1.1/{doc_hash}/`) |
| D6 | 2026-09-04 (五) | 联调 | EKRS `pipeline.ingest(version=3)` 灰度 50-bundle; 验证 ≥ 90% pass |
| D7 | 2026-09-05 (六) | 联调 | 全 96 bundle ingest + `scripts/c13_failed_bundles_verify.py` 出 report |

**关键节点**:
- **D3 end-of-day**: doc-to-md 给 EKRS 一份 v1.1 schema spec (15 行 markdown), EKRS 在 D4-D5 review
- **D4 end-of-day**: doc-to-md 给 96 bundle 的 `/mnt/disk/text/v1.1/` 路径列表, EKRS 配置 SHARED_STORAGE_PATH
- **D6 mid-day**: 灰度报告. 若 ≥ 50%, 进 D7 全量; 若 < 50%, doc-to-md D6 下午补 fix, D7 早上重跑

---

## 六、doc-to-md 端交付清单

1. **`v1.1 schema spec`** — 15 行 markdown, D3 EOD
2. **`parsers/pdf_parser.py` patch** — markdown pipe 路径空 structured 降级 (D2)
3. **`parsers/docx_parser.py` patch** — merged cell → text 升级 (D2)
4. **`parsers/postprocess/coalesce.py` (新)** — 碎片合并 (D2)
5. **`parsers/utils.py:markdown_pipe_to_structured`** — 空 list 改 None 返回 (D2)
6. **`scripts/validate_against_ekrs_contract.py` (新)** — corpus 校验, fail-fast on `single_table_monolith_like > 0` (D3)
7. **`scripts/repair_96_failed_bundles.py` (新)** — 96 bundle 重输出工具 (D4)
8. **测试** ≥ 10 单测, 覆盖 3 个根因路径 (D4)
9. **96 bundle 重产物** in `/mnt/disk/text/v1.1/` (D5)

---

## 七、EKRS 端承诺 (从协调文档 §4.2 摘录 + 补充)

1. **不动 Qdrant**, `pipeline.ingest(version=3)` 增量 ingest (D6)
2. **写 `scripts/c13_failed_bundles_verify.py`**, 按 §4.2 阈值出 report (D7)
3. 若 96 bundle 验收未达 93.7%, 逐 doc 复盘
4. **不接受** doc-to-md 在源文件不可用时用 stale 旧 bundle 凑数 — 我们承诺所有 96 bundle 都重新解析

---

## 八、未决问题 — EKRS 侧建议（2026-08-27 已答复）

> **状态**: 全部 6 项已 EKRS 侧明确, doc-to-md 按此实施. 本文档可 close.

| # | 问题 | EKRS 答复 | doc-to-md 实施要点 |
|---|------|----------|------------------|
| 1 | §3.2 raw ≤ 3072 chars split 策略 | **按 GFM row split** (识别 `\n\| ` 边界), 失败再 hard cap + `[... truncated ...]` marker. 保持表格结构完整性优先 | `parsers/utils.py` 加 `_split_markdown_pipe_by_row(text, max_chars=3072)`, 优先 `\n\| ` 切分, 切不开再 3072 硬切 |
| 2 | §3.5 `mixed_content` 字段类型 | **dict**: `mixed_content: {has_text: bool, has_image: bool, has_table: bool}` | `index.json.doc_metadata.mixed_content` 三 bool 字段, EKRS 端 filter 任意组合 |
| 3 | §3.4 `merged_from_block_ids` 类型 | **list[str]** (UUID), 与 EKRS point_id 派生一致, 便于追踪 | `parsers/postprocess/coalesce.py` 输出 `metadata.merged_from_block_ids: list[str]` |
| 4 | 96 bundle 重输出路径 | **`/mnt/disk/text/v1.1/{doc_hash}/data.jsonl`** 新路径, 原路径保留 fallback, EKRS 通过 `SHARED_STORAGE_PATH` 环境变量切换 | D5 输出至 v1.1/ 不动 in-place |
| 5 | Q5 oversized_image_block 立项 | **D7 后单独立项**, 本文档 close, 新协调项 `ekrs-oversized-image-block-coord-2026-08-XX.md` 跟踪 | 不阻塞本文档, doc-to-md D7 后开新回复 |
| 6 | D3 schema spec 内容 | 15 行 markdown, 覆盖: **structured 契约 (§3.3)** + **`mixed_content` 字典格式** + **`merged_from_block_ids` list[UUID] 格式** | D3 EOD 给 EKRS review |

---

## 九、元数据

- 协调请求: `/home/pangzy/code_project/EKRS/docs/coordinations/2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md`
- 上游协调: O-6 wedge fix (2026-08-20 ship), O-7 raw=list fix (2026-08-20 ship), Phase 12 全 ship (2026-07-30)
- 关联代码: parsers/pdf_parser.py, parsers/docx_parser.py, parsers/utils.py, parsers/block_assigner.py, pipeline/orchestrator.py, backend/engine/renderer.py
- doc-to-md 侧 owner: 表格解析 (docx_parser + pdf_parser markdown path), 碎片合并 (postprocess/coalesce)
- EKRS 侧 owner: ingest verification, 灰度控制
- 状态: **contract accepted, 6 questions resolved, ready to ship** (D1 启动后 ship 流程按 §五 时间表推进)