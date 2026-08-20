---
title: "doc-to-md → EKRS: O-7 content.raw must be str fix landed (commit 4f4c3f5, 2026-08-20)"
date: 2026-08-20
category: docs/solutions/integration-issues
module: rag-integration
status: closed — fix shipped
related_request: ekrs-raw-list-bug-coord-request-2026-08-20.md
---

# doc-to-md → EKRS: O-7 `content.raw must be str` 修复完成

> **TL;DR**: O-7 已落地. 575 老 bundle 已 fix-in-place 修. 新生成 bundle 走修好的 parsers 不再 emit raw=list. commit `4f4c3f5`.

---

## 一、已做的事

### Q4. CONSTRAINTS O-7 落地

**根因**: 老 `parsers/pdf_parser.py` markdown pipe 路径内部用 list-of-lists 解析表格, 在 emit bundle 之前把 `content.raw` 直接赋成同一个 grid (而不是 GFM pipe string). Pydantic `DocumentBlockIR.raw: str` 校验失败 → `ir_parse_error` → 575 bundle 全部 ingest 失败, 每个 600s timeout.

**修复** (commit `4f4c3f5`):

| 文件 | 改动 |
|---|---|
| `parsers/utils.py` | 新 helper `serialize_structured_to_raw(structured, btype)`: table→GFM pipe, kv→`"k: v\nk: v"`, None/空→空串 |
| `scripts/audit_structured_none.py` | 新增 `--check-raw-type` + `--emit-raw-list`: 独立 axis 检测 `content.raw` 不是 str 的 block. exit code 反映 O-6 + O-7 总和违规 |
| `scripts/repair_wedge_bundles.py` | 重构: 接受 `--wedge-list` 或 `--raw-list`, 新增 `--fix-raw-list` flag 调 helper 反序列化 raw. 支持 O-6 + O-7 双 axis 同时修 |
| `tests/test_table_structured_o6.py` | +11 tests: `serialize_structured_to_raw` 7 unit (table/kv/None/空/未知 type/round-trip), audit `--check-raw-type` 2 smoke (违规 + 干净), repair `--fix-raw-list` 2 smoke (happy + structured 缺失跳过) |

### Q5. repair script fallback 落地

`scripts/repair_wedge_bundles.py` 加 `--fix-raw-list` flag. 检测到 raw 不是 str 时从 `structured` 反序列化重建 raw (用 `serialize_structured_to_raw` helper). `md_preview` 同步修复 (若不是 str 也覆盖).

边界处理:
- `raw=list + structured=list` → serialize structured → raw (正常路径, 12165 block)
- `raw=list + structured=None/缺失` → skip + warning (无法重建, 不写回)
- `raw=str` → 不动 (避免意外覆盖)
- `md_preview` 不是 str → 用 raw 副本覆盖 (Pydantic 同样约束, 避免双重 reject)

## 二、本机 corpus 验证 (`/home/pangzy/code_project/doc-to-md/output`, 3809 bundles)

| 阶段 | raw-type violations | bundles with raw≠str |
|---|---|---|
| audit 前 | 12165 | 585 |
| repair | 12165 fixed | 585 |
| audit 后 | **0** | **0** |

**O-7 P1 wedge COMPLETELY eliminated.**

Spot-check bundle `02e372d43b25a58b` (EKRS coord doc §一 引用的具体例子):

```python
# Before:
Line 13: type=table raw_type=list raw_len=19 struct_len=19  # same data twice
# After:
Line 13: type=table raw_type=str  raw_len=673 struct_len=19  # canonical GFM pipe
         structured unchanged
```

修复后 `content.raw` 是合规 GFM pipe string (`| Page | Location | Change |\n| --- | --- | --- |\n| iii-v | Contents | Updated |\n...`), `content.structured` 是同一份 grid list — 两份字段互补, 不再重复.

## 三、EKRS 侧的建议操作

```bash
# 1. 重跑 audit 确认本地 corpus O-7=0
python3 /home/pangzy/code_project/doc-to-md/scripts/audit_structured_none.py \
    --output-dir /home/pangzy/code_project/doc-to-md/output --check-raw-type

# 2. (本机已 ship) 如有其他 corpus 还有 raw=list, 用 audit 输出 + repair fix-in-place:
python3 /home/pangzy/code_project/doc-to-md/scripts/audit_structured_none.py \
    --output-dir /path/to/other/corpus --check-raw-type \
    --emit-raw-list /tmp/raw_not_str.txt
python3 /home/pangzy/code_project/doc-to-md/scripts/repair_wedge_bundles.py \
    --raw-list /tmp/raw_not_str.txt --fix-raw-list

# 3. 重跑 ingest 之前被跳过的 575 bundle:
python scripts/ingest_new_bundles.py --include-list /tmp/raw_not_str.json
```

## 四、`serialize_structured_to_raw` corner case 设计

```python
def serialize_structured_to_raw(structured: Any, btype: str) -> str:
    if btype == "table":
        if isinstance(structured, list) and structured:
            return matrix_to_markdown(structured)   # 复用现有 helper
        if isinstance(structured, list) and not structured:
            return ""
    if btype == "kv":
        if isinstance(structured, dict):
            return "\n".join(f"{k}: {v}" for k, v in structured.items())
        return ""
    return ""
```

复用 O-6 修复时已有的 `matrix_to_markdown` helper. Round-trip test (`test_roundtrip_md_to_structured_to_md`) 验证 `markdown_pipe_to_structured → serialize_structured_to_raw → markdown_pipe_to_structured` 三步回到同一份 grid.

## 五、关联

- 修复 commit: `4f4c3f5` (本 commit, 4 files / +491 / -66)
- 上游 O-6 fix commit: `446d5df` (related, ship 1 hour earlier 同日)
- 上游 coord 请求: `ekrs-raw-list-bug-coord-request-2026-08-20.md`
- 真理源: `CONSTRAINTS.py:9 O-6` + 新增 O-7 (content.raw must be str for ALL block types)
- 验证脚本: `scripts/audit_structured_none.py --check-raw-type`, `scripts/repair_wedge_bundles.py --fix-raw-list`
- 单元测试: `tests/test_table_structured_o6.py` (22 tests, all pass)

## 六、未决问题

- **3790 remaining O-6 violations** (本机 corpus, raw < 5000 chars 短表 `structured=None`). 跟 O-7 是独立 axis, 不触发 EKRS 1340-chunk wedge, 不在本 P1 scope. 已写入 [上一份 reply §五](ekrs-wedge-table-block-coord-reply-2026-08-20.md#五未决问题).
- **TaskRepo failed path bug**: 即便 RAG log `ingestion_failed`, `/v1/ingestion/status/{doc}` 仍返回 404. O-7 修完后 575 bundle 不会触发此路径, 但 bug 本身仍在 (其他 future failure mode 仍受影响). 建议 EKRS 单开 bug ticket.
- **`md_preview` 历史不合规**: 部分老 bundle `md_preview` 是 list 而 raw 是 str (罕见). 本次 fix-in-place 仅同步修复 `raw` 和 `md_preview` 都是 non-str 的 case; raw=str 但 md_preview≠str 的 case 暂不修 (Pydantic 仍 reject md_preview 但 case 稀少). 下次 audit 可加 `--check-md-preview-type` 单独跑.
- **91 small_no_struct bundles** (raw < 5000 chars, type=table, structured=None): 跟 575 raw=list 是不同 bug 模式. O-7 fix 不动它们. 如果 EKRS 决定也修, 可以用 O-6 修复时的 `repair_wedge_bundles.py --fix-structured` (默认 on) 走 markdown pipe parse 路径回填 structured.

---

**doc-to-md 侧 status**: O-7 fix shipped, 0 raw-type violations on local corpus.
**EKRS 侧 action**: 重跑 ingest 那 575 bundle (无需 workaround), 关闭 `phase12-raw-list-ir-reject` buglog.
