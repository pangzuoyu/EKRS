# Phase 10 T10a-5 — chunk_id EKRS-side generation + 双向映射 (FTS↔Qdrant)

## 父计划

[`2026-07-28-phase10-broad-spectrum-retrieval.md`](2026-07-28-phase10-broad-spectrum-retrieval.md) §T10a-5 行 (line 30).

## 范围

把 `chunk_id` 从 T10a-1 的 generator-only 提升到 payload+row 字段, 双向 round-trip:

1. **`QdrantManager.upsert_chunks` payload 加 `chunk_id: str` 字段**. **注意命名空间共存** (父计划 §[M2]): Qdrant payload 已有 `block_id` (来自 `ir_parser` UUID, 不可重写). 新字段命名 `chunk_id`, **并存**于 payload, 不替换 `block_id`. `source_block_ids` (list) 保留.
2. **生成时机**: 分块完成后、Qdrant 写入前由 EKRS 生成. `QdrantManager.upsert_chunks` 接收 `chunks: list[Chunk]`, 在构造 payload 时按 `(chunk_index = chunks.index(chunk))` 调 `FTSManager.generate_chunk_id(doc_hash, chunk_index)` 生成. 不依赖 Qdrant 返回的 point ID.
3. **FTS `replace_doc` payload_json 已包含 `chunk_id`** (T10a-2 既有). 验证 round-trip: 同一 chunk 的 Qdrant payload `chunk_id` == FTS row `chunk_id`.
4. **`FTSManager.get_chunk_id(block_id)`** 已存在 (T10a-1 双向原语). 改文档为 "block_id→chunk_id 入口" — 实现在 T10a-5 进一步加入 "chunk_id→block_id" 互补方向 (`get_block_id_by_chunk_id`).
5. **retriever 切换 key_fn**: T10a-4 用 `f"{doc_hash}:{source_block_ids[0]}"` 作 fallback. T10a-5 后改用 `chunk.chunk_id`. **回滚兼容**: 老 doc (无 chunk_id 字段) → key_fn 走 fallback (现有 T10a-4 行为); 新 doc → chunk_id.
6. **新 doc 路径端到端验证**: ingestion → Qdrant+FTS 同步写 → retriever → RRF → chunk_id 命中.

**T10a-5 边界**: chunk_id 生成 + 双向 + retriever 切换. **不做**:
- FTS schema 改造 (T10a-1 schema 已包含 chunk_id 列 UNINDEXED, no schema change needed).
- golden set 50→55 (T10a-6).
- 审计 fts_synced/fts_searched emit (T10a-7).

## 设计

### chunk_id 生成

```python
# FTSManager (T10a-1 既有, 不变)
@staticmethod
def generate_chunk_id(doc_hash: str, chunk_index: int) -> str:
    """Generate chunk_id = `{doc_hash[:8]}-{chunk_index:04d}`."""
    return f"{doc_hash[:8]}-{chunk_index:04d}"
```

调用点: `QdrantManager.upsert_chunks` (line 199 起) 在构造 payload 时:

```python
for idx, (chunk, vec) in enumerate(zip(chunks, encoded)):
    point_id = str(uuid.uuid5(...))
    sparse_qdrant = self._embedding_service.to_qdrant_sparse(vec.sparse)
    chunk_id = FTSManager.generate_chunk_id(chunk.doc_hash, idx)  # T10a-5
    payload = {
        "text": chunk.text,
        "scope_path": chunk.scope_path,
        "source_block_ids": chunk.source_block_ids,
        "token_count": chunk.token_count,
        "doc_hash": chunk.doc_hash,
        "version": chunk.version,
        "page_numbers": chunk.page_numbers,
        "chunk_id": chunk_id,  # T10a-5 NEW
    }
    points.append(models.PointStruct(...))
```

### Qdrant payload schema 改动

| 字段 | 类型 | 索引 | 备注 |
|---|---|---|---|
| `block_id` | str (UUID from ir_parser) | 已存在 | **不可重写** (parent §[M2]) |
| `source_block_ids` | list[str] | 已存在 | 多 block 合并时存 UUIDs |
| `chunk_id` | str (`{doc_hash[:8]}-{idx:04d}`) | **T10a-5 新增** | 与 `block_id` 并存 |

### 双向映射 (`FTSManager`)

T10a-1 既有 `get_chunk_id(block_id)` 查 chunk_id (FTS 行存 block_id). T10a-5 加反向:

```python
def get_block_id_by_chunk_id(self, chunk_id: str) -> Optional[str]:
    """Inverse lookup: chunk_id → block_id (T10a-5). FTS row 唯一."""
    row = self._conn.execute(
        "SELECT block_id FROM blocks_fts WHERE chunk_id = ? LIMIT 1",
        (chunk_id,),
    ).fetchone()
    return row[0] if row else None
```

### retriever key_fn 切换

```python
# T10a-4 (现在)
key_fn = lambda c: f"{c.doc_hash}:{c.source_block_ids[0]}"

# T10a-5 (after this task)
key_fn = lambda c: c.chunk_id or f"{c.doc_hash}:{c.source_block_ids[0]}"
```

**回滚兼容**: `chunk.chunk_id` is None for 老 doc (ingestion 之前未带 chunk_id). `c.chunk_id or fallback` 双轨: 新 doc 走 chunk_id, 老 doc 走 fallback. **无迁移脚本** (chunk_id 是 deterministic from doc_hash + index, 老 doc 可重新 ingest 生成, 但本任务不强制 re-ingest).

### Iron Rules 合规

| Rule | 影响 |
|---|---|
| R1 | chunk_id 是 Qdrant payload 字段, 不参与 hint 提取 — 既有 retriever/solver 路径不动 |
| R2 | retriever 仍纯函数-ish (异步 I/O 隔离 via to_thread); RRF key_fn 改动不影响 R2 |
| R4 | scope_priority 仍 AFTER RRF (T10a-4 已锁定); key_fn 切换不影响 |
| R7 | scope_filter 不变 |
| R8 | `status != 'illegal'` 仍 apply |

## 4 个 TDD 任务 (Te.1 / Te.2 / Te.3 / Te.4)

| # | 任务 | 工作量 | 验收 |
|---|---|---|---|
| **Te.1** | RED: chunk_id payload + round-trip + retriever key_fn 切换 + 老 doc 回滚兼容 | 0.5 天 | ≥8 个 fail test |
| **Te.2** | GREEN: QdrantManager 加 chunk_id; FTSManager.get_block_id_by_chunk_id; retriever key_fn switch | 0.5 天 | 8 测试 pass; Phase 6B / T10a-4 既有 retriever 测试不退化 |
| **Te.3** | IMPROVE: 老 doc payload 无 chunk_id 路径 + 边界 (chunk_index > 9999, doc_hash < 8 chars) | 0.25 天 | 3 边界测试 pass |
| **Te.4** | 文档 + 标签 + 记忆 + FF push | 0.25 天 | CHANGELOG + handbook + memory + FF push master; 无新 tag |

### Te.1 测试枚举

`tests/unit/test_qdrant_chunk_id.py` ≥4 测试:

1. `test_upsert_chunks_payload_includes_chunk_id_field` — QdrantManager.upsert_chunks 写入的 payload 包含 `chunk_id` 字段 (mock Qdrant client; 验证 payload dict)
2. `test_upsert_chunks_chunk_id_format_matches_generator` — `chunk_id == FTSManager.generate_chunk_id(doc_hash, chunk_index)`
3. `test_upsert_chunks_chunk_id_unique_within_doc` — 同一 doc 不同 chunk 索引 → 不同 chunk_id
4. `test_upsert_chunks_chunk_id_stable_across_calls` — 同一 `(doc_hash, chunk_index)` 多次调用 → 相同 chunk_id (deterministic)

`tests/unit/test_fts_bidirectional.py` ≥2 测试 (T10a-1 已有 `get_chunk_id`, 本任务加反向):

5. `test_get_block_id_by_chunk_id_returns_block_id` — FTS row 写入后, `get_block_id_by_chunk_id(chunk_id)` 返回 `block_id`
6. `test_get_block_id_by_chunk_id_returns_none_for_missing` — 不存在 chunk_id → None

`tests/unit/test_retriever_t10a5.py` ≥2 测试:

7. `test_retrieve_key_fn_uses_chunk_id_when_present` — chunks 有 chunk_id → RRF key 是 chunk_id (mock reciprocal_rank_fusion to verify key)
8. `test_retrieve_key_fn_falls_back_to_doc_hash_for_legacy_chunks` — chunks 无 chunk_id → key 是 `f"{doc_hash}:{block_id}"` (回滚兼容)

### Te.3 IMPROVE 边界测试

- `test_generate_chunk_id_handles_short_doc_hash` — doc_hash 长度 < 8 → 用原 doc_hash (no truncation error)
- `test_generate_chunk_id_handles_large_chunk_index` — chunk_index > 9999 → 仍生成合法字符串 (no overflow)
- `test_upsert_chunks_legacy_payload_without_chunk_id_still_works` — ingestion 端 chunk.chunk_id = None 时, QdrantManager payload 不写 chunk_id 字段 (老 ingestion 路径兼容)

### Te.4 文档

- CHANGELOG.md `[Unreleased] ## Added` 加 T10a-5 段
- ekrs-handbook.md §6 timeline 加 T10a-5 行
- CLAUDE.md Current State 加 T10a-5 行
- memory `phase10-t10a-5-closed.md`
- MEMORY.md 加 pointer
- git commit `feat(retrieval): ...`
- FF push master

## 标签策略

父计划 §"标签策略": `phase10.1` 锁 1c44eee; `phase10` 留给 T10a-7 closure. **本任务不开新 tag**.

**Push 路径 (M2)**: FF push master. refspec-push fallback per `phase7-closure.md`.

## 验证闸门

- [ ] `tests/unit/test_qdrant_chunk_id.py` ≥4 测试全 pass
- [ ] `tests/unit/test_fts_bidirectional.py` ≥2 测试 pass (含 T10a-1 既有 + T10a-5 新增)
- [ ] `tests/unit/test_retriever_t10a5.py` ≥2 测试 pass
- [ ] Phase 6B + T10a-4 既有 retriever 测试不退化 (4 + 13 = 17)
- [ ] mypy 干净 (新增 2 文件, 标准 50/50 → 52/52)
- [ ] `make test` 全套不退化 (现有 800+ 测试)
- [ ] CHANGELOG.md `[Unreleased] ## Added` 写好
- [ ] ekrs-handbook.md §6 timeline 加 T10a-5 行
- [ ] memory `phase10-t10a-5-closed.md` 已写
- [ ] FF push master 成功
- [ ] 无新 tag

## 风险

| 风险 | 缓解 |
|---|---|
| 老 doc (无 chunk_id) ingestion 后 retriever 报错 | `key_fn = c.chunk_id or f"{doc_hash}:{block_id}"` 双轨; Te.1 测试 #8 显式覆盖 |
| chunk_id 与已有 `block_id` 字段冲突 (parent §[M2]) | 新字段并存, **不替换** `block_id`; tests 验证 `block_id` 路径不退化 |
| chunk_index enumeration 与 Qdrant 写入顺序不一致 | 单次 `enumerate(zip(chunks, encoded))` 保证顺序; doc_hash[:8] + chunk_index 决定 |
| round-trip 不一致 (Qdrant payload chunk_id != FTS row chunk_id) | Te.1 测试 #2 验证生成函数确定; round-trip test (upsert → search_with_payload → 比对) 显式覆盖 |
| `QdrantManager.upsert_chunks` 已有 14 测试可能因新字段而失败 | payload dict 是新加 key, 既有测试只断言 sub-keys, 不比较整体; 跑全套确认 |

## 开放问题 (实施前关闭)

1. ~~**retriever key_fn 切换时机**~~ — **关闭**: T10a-5 commit 时改; `c.chunk_id or fallback` 双轨处理老 doc.
2. ~~**chunk_id 格式**~~ — **关闭**: `{doc_hash[:8]}-{chunk_index:04d}` (T10a-1 已锁, `FTSManager.generate_chunk_id` 实现). doc_hash < 8 chars → 用原字符串.
3. ~~**老 doc re-ingest 强制 vs 不强制**~~ — **关闭**: 不强制. 新 doc 自动有 chunk_id; 老 doc 用 fallback key_fn. 无迁移脚本 (deterministic 重新 ingest 会自动生成).
4. ~~**retriever payload deserialization 加 chunk_id 字段**~~ — **关闭**: `_payload_to_chunk` 加 `chunk_id=payload.get("chunk_id")`. Pydantic Chunk model 加可选 `chunk_id: Optional[str] = None`. 老 ingestion 不传 → None.
5. ~~**FTSManager.get_block_id_by_chunk_id vs get_block_ids_by_chunk_id**~~ — **关闭**: 单数 (1 row → 1 block_id). FTS row 设计是 1 chunk → 1 block_id (T10a-1 schema). 多 block 合并的 chunk 走 `source_block_ids` list, 但 FTS row 只存首个 block_id (T10a-2 `replace_doc` 既有).
6. ~~**新增 chunk_id 字段影响 Qdrant schema migration**~~ — **关闭**: payload field 加新 key 不需要 schema migration (Qdrant 自动 allow). 既有 collection 写入新 payload field → 自动新增 index. Te.1 测试 #1 验证 payload 包含 chunk_id 即可, 不需要真 Qdrant 重启.

**已无未关闭问题. 可开始 Te.1 RED.**

## GSTACK REVIEW REPORT

**Run:** 1 (self-review pre-implementation) · **Status:** clean (1 patch recommended, applied below)
**Date:** 2026-07-29
**Reviewer:** claude (eng-review pass)

### Findings — 5 项 (1 MEDIUM + 4 INFO)

| # | Severity | Conf | Finding | Resolution |
|---|---|---|---|---|
| [M1] | MEDIUM | 7/10 | `Chunk` Pydantic model 加 `chunk_id` 字段需 Pydantic 字段声明 + default None; 不加则 `_payload_to_chunk` 抛 ValidationError | ✅ Te.1 RED 验证 `c.chunk_id` 行为; 加 `Optional[str] = None` 到 Chunk model |
| [I1] | INFO | 6/10 | `get_block_id_by_chunk_id` 单数 vs 多 block_id 风险 (FTS row 只存首个 block_id) | ✅ docstring 注明 "first block_id; multi-block 合并走 source_block_ids"; 计划 §5 关闭问题 |
| [I2] | INFO | 5/10 | Qdrant payload 新 key 无 schema migration — 既有点写入可能索引缺失 | ✅ Qdrant payload field 是 dynamic schema; 加新 field 自动索引; Te.1 测试 #1 mock 验证 payload dict, 不需真 Qdrant |
| [I3] | INFO | 4/10 | 老 doc (无 chunk_id) retriever 走 fallback key_fn → RRF 与 T10a-4 fallback 路径一致, 但 key 不唯一 across docs | ✅ docstring 注明 fallback 是 "短-时间过渡兼容", 长期 re-ingest 自动生成 chunk_id |
| [I4] | INFO | 3/10 | `chunk_index` enumeration vs `chunks.index()` 性能 — enumeration 是 O(1), index() 是 O(n) | ✅ enumeration 优先 (T10a-5 计划 §Te.2 GREEN code review) |

### Run 1 Verdict

**QUALITY: 7.5/10**. 1 MEDIUM (M1 Chunk model schema) 已规划在 GREEN 阶段一并 patch; 4 INFO mitigation 文档化. **Ready to implement** — proceed Te.1 RED.