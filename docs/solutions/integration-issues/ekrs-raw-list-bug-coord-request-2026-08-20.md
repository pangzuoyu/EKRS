---
title: "doc-to-md → EKRS: table block content.raw is list not string — 575 bundles IR-rejected"
date: 2026-08-20
category: docs/solutions/integration-issues
module: rag-integration
problem_type: schema_validation_failure
component: EKRS DocumentBlockIR.content.raw (Pydantic string constraint)
severity: P1 (silently blocks 575/2825 new bundles; ir_parse_error → TaskRepo no record → 600s timeout per bundle)
target_audience: doc-to-md team
status: open — request
ekrs_actions_pending: restart ingest with /tmp/new_safe_v2.json (2249 bundles, excludes the 575 broken)
related_constraint: doc-to-md/CONSTRAINTS.py O-6 + new O-7 (content.raw must be string for ALL blocks)
supersedes: ekrs-wedge-table-block-coord-request-2026-08-20.md (closed in 446d5df reply, but a different violation pattern remains)
---

# doc-to-md → EKRS: `content.raw` 是 list 不是 string — 575 bundle IR-rejected

> **TL;DR**: O-6 fix (`446d5df`) 解决了 `structured=None` 的 wedge, 但留下 **575 个 bundle** 有另一种违规: `type=table` block 的 `content.raw` 是 `list` 而不是 `str`. Pydantic DocumentBlockIR 强制 `raw: str`, ingest 失败 (`ir_parse_error`). doc-to-md parser 需要新增 O-7 约束: **任何 block 的 `content.raw` 都必须是 string**. 当前 EKRS workaround: 跳过这 575 bundle, 跑剩下 2249.

---

## 一、具体 IR-reject 例子

**Bundle**: `/home/pangzy/code_project/doc-to-md/output/text/02e372d43b25a58b/`

| Line | type | raw_type | raw_len | struct_len |
|---|---|---|---|---|
| 13 | `table` | **list** | 19 | 19 |
| 16 | `table` | **list** | 10 | 10 |
| 其他 | text/image | str | <5000 | N/A |

`Line 13` 的 raw 值:
```python
content.raw = [['1', 'N/A', 'N/A', 'N/A', 'N/A', ...]]  # type=list, len=19
content.structured = [['1', 'N/A', 'N/A', 'N/A', 'N/A', ...]]  # type=list, len=19  ← 重复!
```

注意 `raw` 和 `structured` 是**同一份 list 数据**. parser 把 grid 序列化时漏了 string 转换.

## 二、IR-reject 在 EKRS 侧的发生路径

`rag/ekrs_rag/ingestion/ir_parser.py:57` → `extract_text(content)` → `DocumentBlockIR` Pydantic 校验:

```python
class ContentIR(BaseModel):
    raw: str                              # ← 强制 string
    md_preview: str | None = None
    structured: list | dict | None = None
```

`raw: list` 触发 Pydantic `string_type` 校验失败:

```
JSONL parse error for a0f796a58ad78f93: 
  Line 2: Schema validation failed: 2 validation errors for DocumentBlockIR
  content.raw:    Input should be a valid string [type=string_type, 
                  input_value=[['N/A', 'N/A', ...]]]
  content.md_preview: 同上
```

`ingestion_pipeline.ingest()` 捕获后 emit `ingestion_failed error_code=ir_parse_error` 到 audit log + **写 TaskRepo** 但用 failed 状态. 但 `/v1/ingestion/status/{doc_hash}` endpoint 实际**不返回 failed 状态**, 持续返回 404. 这是已知 TaskRepo vs status endpoint 的不一致 (parent §204 audit isolation 没覆盖 failure path).

`scripts/ingest_new_bundles.py` `poll_status(timeout=600s)` 600s 后判 timeout → 标 failed → 进入下一 bundle. **每个 IR-reject bundle 浪费 600s wall clock**.

## 三、影响面

| 维度 | 数 |
|---|---|
| Corpus 总 bundles | 3809 |
| 已在 Qdrant | 1859 (Phase 12 Task D 745 + 后续增量) |
| **新 bundle 待入库** | 2825 (new_valid.json) |
| **wedge bundle（structured=None & block > 5000 tokens）** | 0 (O-6 fix 后) |
| **IR-reject bundle（raw 是 list）** | **575 (20.4%)** |
| 真正可入库（OK + small_no_struct） | **2249** |
| jsonl_broken | 1 |

`raw=[]` 集合 (`/tmp/raw_not_str.json`) 全部是 `type=table` block — 没有 text/image/kv block 触发此 bug.

## 四、请求 doc-to-md 修复的事项

### Q4. 落地 CONSTRAINTS O-7（新增约束）

> `CONSTRAINTS.py O-7 content.raw must be string for ALL block types`

**预期行为**: 任何 `content.raw` 必须是 `str` 类型. 若内部数据是 `list` 或 `dict`, parser emit 前序列化: `type=table` 用 markdown pipe string (`| ... | ... |`), `type=kv` 用 `key: value\nkey: value` string, `type=text` 用 raw 文本.

**修复路径**:
1. **docx_parser.py** emit `type=table` block: 不要把 `[[cell.text for cell in row.cells] for row in table.rows]` 直接当 `raw` (line 61 现有 grid 用法). 用 `"\n".join("| " + " | ".join(row) + " |" for row in grid)` 序列化.
2. **pdf_parser.py** markdown pipe 路径 (`line 1158`): emit 前检查 `raw` 是 `str`, 如果 parser 内部变量是 list, 立即 serialize.
3. **`parsers/utils.py`** 加 helper `serialize_structured_to_raw(structured, btype) -> str`, 与 `markdown_pipe_to_structured` 反向. 复用现有 `_grid_to_markdown(grid)`.
4. 单元测试 (`tests/test_table_structured_o6.py` 扩展): 给 fixture `02e372d43b25a58b` 断言所有 block 的 `content.raw` 是 `str`.

### Q5. 给修复脚本加 fallback (可选, 跟 Q4 互补)

> `scripts/repair_wedge_bundles.py` 增加 `--fix-raw-list` flag

当检测到 `type=table` 且 `raw` 是 `list` 时, 从 `structured` 重建 `raw` (用 Q4 描述的 serialize helper).

**EKRS 侧 workaround**:
```bash
# 当前 575 bundle 跳过, 用 /tmp/new_safe_v2.json (2249)
python scripts/ingest_new_bundles.py --include-list /tmp/new_safe_v2.json
```

Q4 落地后, 跑 fix-in-place:
```bash
python /home/pangzy/code_project/doc-to-md/scripts/repair_wedge_bundles.py \
    --raw-list /tmp/raw_not_str.json --fix-raw-list
```

然后重 ingest:
```bash
python scripts/ingest_new_bundles.py --include-list /tmp/raw_not_str.json
```

## 五、验证 doc-to-md 修复完成的标准

- [ ] `scripts/audit_structured_none.py --check-raw-type` 输出 0 违反 O-7 的 bundle
- [ ] bundle `02e372d43b25a58b` + `a0f796a58ad78f93` + `9df5a745c7c3cc7d` 重新解析后所有 block 的 `content.raw` 是 `str`
- [ ] 575 个 IR-reject bundle 用 fix-in-place 修后, 跑 ingest 全部 success (无 ir_parse_error)
- [ ] EKRS ingest 速度恢复正常, 无 600s/bundle timeout

## 六、未决问题

- **TaskRepo failed path bug**: 即便 RAG log `ingestion_failed`, `/v1/ingestion/status/{doc}` 仍返回 404. 这是 EKRS 侧 status endpoint 不读 failed record 的 bug, 不在 doc-to-md 范围. 建议 EKRS 单独开个 bug ticket 修 (跟 O-7 修复同步, 修完后 ingest script 不用 timeout 浪费 600s).
- **`md_preview` 同样违规**: Pydantic error 显示 `md_preview` 跟 `raw` 同步违规. 同 fix 一起修即可.
- **`01d2...` 91 small_no_struct 跟 575 raw_not_str 是同一类么?**: 不一样. 91 个是 `raw` 是 `str` (合规) 但 `type=table` 且 `structured=None` 且 raw_len < 5000 → 不会 wedge, 但 `O-6` 也不合规. doc-to-md audit 3790 violations 包含这些. 跟 Q4 是独立 axis, 可在 O-7 PR 一起 sweep.

---

**EKRS 侧 status**: workaround 已 ship (new_safe_v2.json 2249 跑中), 等 doc-to-md 落地 O-7 后 close.
**回复请写**: `/home/pangzy/code_project/EKRS/docs/solutions/integration-issues/ekrs-raw-list-bug-coord-reply-2026-08-XX.md`