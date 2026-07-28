# Phase 10 T10a-1 — FTSManager 实施计划

## 父计划

[`2026-07-28-phase10-broad-spectrum-retrieval.md`](2026-07-28-phase10-broad-spectrum-retrieval.md) T10a-1 行 + §开放问题 1/5/6 已闭. 本计划不重复决议.

## 范围

新建 `rag/ekrs_rag/retrieval/fts_manager.py`. SQLite FTS5 镜像 Qdrant payload, 提供 BM25 关键词检索路径. T10a-2/3/4/5/6/7 后续任务; 本任务**只做 T10a-1** (建表 + CRUD + 归一化 + 测试), 不接 pipeline 不接 retriever.

## 设计

### 类

```python
class FTSManager:
    """SQLite FTS5 全文索引管理器 — Phase 10 T10a-1.

    镜像 Qdrant chunk payload 到 FTS5 虚拟表. 与 Qdrant 写入并行, 但不
    替代 (R1: FTS 不参与 hint 提取, R5: 不参与 KG).
    """

    SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
        chunk_id UNINDEXED,
        block_id UNINDEXED,
        text,
        scope_path,
        status UNINDEXED,
        doc_hash UNINDEXED,
        payload_json UNINDEXED,                        -- T10a-5: 双向映射; UNINDEXED 防 JSON tokenize
        tokenize = 'unicode61 remove_diacritics 2'    -- 不用 porter
    );
    """
```

### 字段命名 (T10a-5 命名空间共存)

| 列 | 来源 | 用途 |
|---|---|---|
| `chunk_id` | EKRS 生成, `generate_chunk_id(doc_hash, chunk_index)` → `{doc_hash[:8]}-{chunk_index:04d}` | FTS 主键, 后续 T10a-5 双向映射锚 |
| `block_id` | ir_parser UUID (不可改) | 现存 Qdrant payload, FTS 行保留便于回查 |
| `payload_json` | Qdrant payload 副本 (UNINDEXED) | T10a-5 双向映射存储, 不开 lookup 表 |

**注意**: `generate_chunk_id(doc_hash, index)` 函数必须在 T10a-1 内部实现 (即使是 stub). 父计划把 chunk_id 生成划入 T10a-5, 但 FTS schema 与 `upsert` 签名已经引用 — 推迟会留下不可执行的代码. T10a-5 接管 retriever 端生成时机, FTSManager 端生成器先到位.

### 同步 sqlite3 (非 aiosqlite)

参照 `storage/task_repo.py:8` 注释: "aiosqlite 0.20+ 移除同步入口, 本类只做同步 CRUD". FTS 也是同步. `check_same_thread=False` 共享 FastAPI worker 线程.

### API

```python
def __init__(self, db_path: Path) -> None: ...
def upsert(self, chunk_id: str, block_id: str, chunk: Chunk, payload: dict) -> None: ...
def search(self, query: str, *, limit: int = 40,
           scope_filter: list[str] | None = None) -> list[tuple[str, float]]: ...
def delete_by_doc(self, doc_hash: str) -> int: ...
def delete_by_chunk_id(self, chunk_id: str) -> int: ...   # H2: 单 chunk 回滚原语
def get_chunk_id(self, block_id: str) -> str | None: ...   # T10a-5 双向映射查询
def close(self) -> None: ...

@staticmethod
def generate_chunk_id(doc_hash: str, chunk_index: int) -> str: ...   # C1: 本任务自带
```

**scope_filter 语义** (H3): FTS5 列限定 MATCH 用 `scope_path : "term1" OR scope_path : "term2"`. 多 scope 元素 `OR` 联合 (用户查询的 scope_path 通常是多层级前缀, 任一层级命中即合理). 不允许 `AND`, 否则长 scope 路径查询永远为空

### BM25 归一化 (QMD §4.1 一致)

```
score = |bm25(blocks_fts)| / (1 + |bm25(blocks_fts)|)
score = max(score, 0.01)
```

### _build_fts5_query

正项: `'term'*` 前缀; 负项: `"term"`; `positive AND ... NOT negative`. 空查询返回 None. 与 `research/2026-07-24-broad-spectrum-retrieval-port-design.md §4.1 c) search()` 一致.

### 路径

- 默认 `FTS_DB_PATH = "/app/rag/fts.sqlite"` (容器内路径, 父计划 §开放问题 1 已闭)
- 加入 `core/config.py` Settings, 镜像 `TASK_DB_PATH` 模式 (line 61)
- 测试用 `tmp_path` fixture (父计划 §开放问题 5 已闭)
- bind-mount 是 10b+ 的 trivial change, 不在本任务范围

## Iron Rules 合规

| Rule | 影响 |
|---|---|
| R1 | FTS 不动 chunk 模型, hint 提取不变 |
| R2 | 求解器接口不变 |
| R3 | **本任务不动闸门** (Gate 1 增强推迟到 T10a-2/4) |
| R4 | scope_filter 等价于 scope_priority 过滤 (RRF 推迟到 T10a-4) |
| R5 | FTS5 是虚拟表不是图库 |
| R6 | BM25 确定性 |
| R7 | scope_path 索引列 |
| R8 | `WHERE status != 'illegal'` 只在 search(), upsert 默认 'active' |

## 4 个 TDD 任务 (Ta.1 / Ta.2 / Ta.4 / Ta.5, 跳过 Ta.3)

| # | 任务 | 工作量 | 验收 |
|---|---|---|---|
| **Ta.1** | RED: 写失败测试 | 0.5 天 | `tests/unit/test_fts_manager.py` ≥12 测试 fail (含 C2 五类用例) |
| **Ta.2** | GREEN: 最小实现 | 0.5 天 | 所有 Ta.1 测试 pass; 含 `generate_chunk_id` + `delete_by_chunk_id` |
| **Ta.4** | 集成测试 (tmp_path, 真实 Chunk IR) | 0.5 天 | `tests/integration/test_fts_manager_integration.py` ≥8 round-trip 测试 pass |
| **Ta.5** | 文档 + 标签 + 记忆 | 0.5 天 | CHANGELOG + handbook + memory; 无新 tag; FF push master |

**T10a-1 不做 T10a-2/3/4/5/6/7 的工作**. Pipeline 接入 = T10a-2. RRF = T10a-3. retriever 接入 = T10a-4. chunk_id 双向映射 (retriever 端生成时机) = T10a-5. golden 回归 = T10a-6. 审计事件 = T10a-7.

**注意**: Ta.3 IMPROVE 阶段在本任务中**不需要** — 父计划已经划 T10a-1 边界 = 建表 + CRUD + BM25 归一化. 本任务的 IMPROVE 阶段是 T10a-2. 所以本任务只有 Ta.1/2/4/5, 跳过 Ta.3.

### Ta.1 测试用例枚举 (C2)

`tests/unit/test_fts_manager.py` 至少 12 个测试:
1. `test_create_table_creates_blocks_fts` — schema 存在
2. `test_upsert_inserts_row_with_required_columns` — 7 列齐
3. `test_upsert_with_status_illegal` — 非法状态可插入
4. `test_search_returns_bm25_normalized_0_1` — 归一化区间
5. `test_search_filters_status_illegal` — R8
6. `test_search_filters_scope_path_or_logic` — R7 OR 语义
7. `test_search_filters_scope_path_multi_term` — H3 多元素
8. `test_search_builds_prefix_query_anded` — `_build_fts5_query` 正项
9. `test_search_negative_term_excluded` — 负项 NOT
10. `test_search_empty_query_returns_none` — 边界
11. `test_delete_by_doc_removes_rows` — 整 doc 清理
12. `test_delete_by_chunk_id_removes_single` — H2 单 chunk
13. `test_generate_chunk_id_format` — C1 生成器
14. `test_get_chunk_id_bidirectional_roundtrip` — T10a-5 双向映射
15. `test_engineering_identifier_A312_TP316_not_tokenized` — T10a-6 smoketest
16. `test_engineering_identifier_GB_T_12459_kept_intact` — T10a-6
17. `test_engineering_identifier_1_6MPa_not_split` — T10a-6
18. `test_cjk_tokenization_养护温度` — M1
19. `test_cjk_tokenization_预应力张拉` — M1
20. `test_unicode61_preserves_dashes` — 拆 dash 测试
21. `test_payload_json_UNINDEXED_no_tokenize` — H1 验证
22. `test_close_closes_connection` — 资源释放

= 22 个用例, 远超 ≥12 下限. 配合 tmp_path fixture.

## 标签策略

父计划 §"标签策略" 已规约: `phase10` reserved for T10a-7 closure (force-move later). `phase10.1` locked at commit `1c44eee` (T10b-1, do-not-move). **本任务不开新 tag**.

**Push 路径 (M2)**: FF push to master (参照 `phase7` / `phase8` / `phase9` precedent — 父计划 §"标签策略" 隐含 master 直接累积). 单 commit 任务 (`Ta.2` GREEN 闭合后) → 直接 FF. 多 commit 任务 (Ta.1 红 + Ta.2 绿 + Ta.4 集成 + Ta.5 文档) → 同样 FF push master, refspec-push 作为 fallback (参照 memory `phase7-closure.md` refspec-push pattern). 任务结束才 push, 不每个 Ta 都 push.

## 验证闸门 (本任务关闭条件)

- [ ] `tests/unit/test_fts_manager.py` ≥12 测试全 pass
- [ ] `tests/integration/test_fts_manager_integration.py` ≥8 round-trip 测试全 pass
- [ ] mypy 干净 (49/49 → 50/50 标准不变)
- [ ] `make test` 全套不退化 (Phase 9 346+ 测试 + Phase 10 T10b-1 60+8 测试)
- [ ] 3 个工程标识符 smoketest pass (`A312-TP316` / `GB/T 12459` / `1.6MPa` 在 BM25 检索中不被拆碎)
- [ ] CHANGELOG.md `[Unreleased] ## Added` 段写好
- [ ] ekrs-handbook.md §6 timeline 加 T10a-1 行
- [ ] memory `phase10-t10a-1-closed.md` 已写

## 风险

| 风险 | 缓解 |
|---|---|
| FTS5 写入与 Qdrant 写入非原子 | 接受短暂不一致, T10a-2 对账任务处理 |
| unicode61 tokenizer 拆碎牌号 (`stainless→stain`) | 已锁定 `remove_diacritics 2`, 不开 porter; Ta.1 加 3 个工程标识符测试防回归 (用例 15/16/17) |
| CJK 检索召回差 | 父计划 §Context 锁定: jieba 是 follow-up, 不在本任务. Ta.1 加 2 个 CJK tokenization 测试 (用例 18/19) 暴露但不强制高召回 |

## 开放问题 (实施前关闭)

1. ~~**FTS5 文件位置**~~ — **关闭**: 容器内 `/app/rag/fts.sqlite` (父计划 §开放问题 1).
2. ~~**aiosqlite vs sync sqlite3**~~ — **关闭**: 同步 sqlite3 (参照 `task_repo.py:8` 注释, FTS 也不跨事件循环).
3. ~~**chunk_id 命名 vs 复用 block_id**~~ — **关闭**: 新字段 `chunk_id` 与现 `block_id` 并存 (父计划 T10a-5).
4. ~~**双向映射存储形式**~~ — **关闭**: FTS 行 `payload_json` 列 (父计划 §开放问题 6).
5. ~~**测试形式**~~ — **关闭**: 单元 + 集成两档, `tmp_path` fixture (父计划 §开放问题 5).
6. ~~**retriever 接入是否在本任务**~~ — **关闭**: 否, 推迟到 T10a-4 (父计划切片图).
7. ~~**pipeline.ingest 接入是否在本任务**~~ — **关闭**: 否, 推迟到 T10a-2.

**已无未关闭问题. 可开始 Ta.1.**