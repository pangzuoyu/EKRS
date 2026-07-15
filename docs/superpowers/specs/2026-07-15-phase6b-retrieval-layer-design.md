# Phase 6B — Retrieval Layer bge-m3 Migration Design

> 修复 6A final review 留下的 3 个生产级检索 bug,以 bge-m3 (1024d + sparse) 替换现有 bge-small-en (384d dense-only) 嵌入路径。

**Date:** 2026-07-15
**Phase tag target:** `phase6b-retrieval-layer`
**Iron Rules:** R1-R8 维持不变(6A 已固化)
**Audit event count:** 16 事件不变(6A 已固化)

---

## §1 目标 / 非目标

### 目标(3 production bug + embedder 升级)
修复 6A triage list 中识别的 3 个检索层 bug,并将嵌入路径从 bge-small-en (384d dense-only) 升级到 bge-m3 (1024d + sparse):

| Bug | 文件:行 | 描述 |
|-----|---------|------|
| B1 | `qdrant_client.py:185` | `self._client.search()` 在 qdrant-client 1.17.1 已删除,真 Qdrant 上 `/v1/constraints` 必然 500 |
| B2 | `qdrant_client.py:41` | `existing.vectors_config["dense"].size` → 1.17.1 是 `existing.config.params.vectors["dense"].size` |
| B3 | `qdrant_client.py:97` | `upsert_chunks` 使用 `[0.0] * vector_size` 零向量(Phase 1 dummy),需接真 embedder |

### 不含(明确延后到 6C+ 或永不)
- DocumentRepo Q-2/Q-5/Q-6/Q-8/Q-10/Q-11/Q-12(Task 2 review minor,paper-tracked)
- Task 4 cosmetic:M3 死引用 + M4 `audit.py` 缺尾换行
- dev_ui Streamlit / k8s manifests / 负载基线 / SLO
- handbook §11 等显式排除项

### 强约束(不可破)
- Iron Rules R1-R8 不动(6A Task 11 reviewer 已 spot-check)
- 16 audit event name/schema 不变(B1/B2/B3 失败时复用 `qdrant_write_failed`,不新增 event)
- 单 commit ≤500 LOC(CQ2 仅适用静态 JSON 数据,模型二进制不属 JSON,需拆分多 commit)
- 测试:531 passed + 1 skipped + 0 failed 基线必须保持
- 覆盖率:≥86.63% 基线 + 新增模块按 100% 目标
- Phase 6A 已固化的所有架构决策(D1-D9)、16 audit 事件集不动
- **新增外部依赖**:FlagEmbedding 是 6A "no new external deps" 之后的第一个新增,需明确批准(本 spec §7 决议)

---

## §2 3 bug + 升级项归位矩阵

| # | 项目 | 归位 | 理由 |
|---|------|------|------|
| 1 | B1: `.search()` → `query_points()` | **6B T3** | 修复即修 bug,Qdrant 1.17.1 唯一可用 API |
| 2 | B2: `vectors_config` → `config.params.vectors` | **6B T3** | 同一 Qdrant 版本相关,需同行修复 |
| 3 | B3: 零向量 → bge-m3 dense+sparse 真实嵌入 | **6B T2-T3** | 接 embedder 是 B1/B2 修复的前置(bge-m3 用法与 1.17.1 query_points 配套) |
| 4 | EmbeddingService facade | **6B T2** | T3 修复 B3 需要新接口,facade 是架构选择(取代 BGESmallEmbedder) |
| 5 | retriever 简化(移除 embedder 注入) | **6B T4** | EmbeddingService 集中管理,retriever 只用 query 接口 |
| 6 | 模型 vendor 进仓(bge-m3 + tokenizer) | **6B T1** | 决策 D3,离线可构建 |
| 7 | lifespan 自动重建 Qdrant 集合(dim 变更) | **6B T3** | 决策 D4,1 次性数据迁移 |
| 8 | 测试分层:mock + `@pytest.mark.heavy` | **6B T5** | 决策 D6,CI 速度 + 真模型验证 |
| 9 | handbook §7 同步(bge-m3 从"声明"改为"已实现") | **6B T6** | 文档对齐,消除 6A §7 的"声明 vs 实际"漂移 |

**Accept / Defer**:9 项全部入 6B,无接受为废弃/重设计。

---

## §3 关键架构决策

### D1: EmbeddingService 接口(取代 BGESmallEmbedder)
**采用 facade 模式**:新增 `rag/ekrs_rag/retrieval/embedding_service.py`,提供单一 `encode(texts) -> list[EncodedVector]` 接口,内部封装 FlagEmbedding `BGEM3FlagModel`,返回 dense + sparse 联合输出。

```python
@dataclass
class EncodedVector:
    dense: list[float]              # 1024 维,L2 归一化
    sparse: dict[int, float]        # {term_id: weight}

class EmbeddingService:
    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR): ...
    def encode(self, texts: list[str]) -> list[EncodedVector]: ...
    def to_qdrant_sparse(self, sparse: dict[int, float]) -> dict: ...  # D8
    @property
    def dense_size(self) -> int: return 1024
    @property
    def is_dummy(self) -> bool: ...   # 模型加载失败时为 True
```

**加载流程**:
1. 校验 `model_dir/model_optimized.onnx` 与 `bge-m3.sha256` 一致(SHA256 校验),**校验失败直接 raise RuntimeError,不允许进入 dummy 模式**(用户反馈点 4)
2. 加载 ONNX session;FlagEmbedding 框架初始化
3. 失败 → 标记 `is_dummy=True`,日志 WARN

**Dummy 模式防御**(用户反馈点 2):
- `is_dummy=True` 时 `encode()` 仍返回零向量 + 空 sparse(保留 CI 通过能力)
- 但 `upsert_chunks` 必须先检查 `embedding_service.is_dummy`;True 则 raise `EmbeddingUnavailableError`,**禁止写入无效数据**
- Service 可以启动(允许只读 + 测试),但写入路径完全禁用
- Production 部署应通过 health check / 外部 probe 检测 dummy 状态,触发告警

**回退策略**:模型加载失败 → 标记 `is_dummy=True`,encode 返回零向量 + 空 sparse,日志 WARN。这是 6A "测试覆盖" 思路的延续,允许 CI 在模型未就绪时通过。

### D2: BGESmallEmbedder 处置
**采用方案 B**:删除 `rag/ekrs_rag/retrieval/embedder.py` 整个文件 + `test_embedder.py`。
- 理由 1:bge-small-en 与 bge-m3 在接口/能力上完全不同(dense-only vs dense+sparse+multivec),保留只会增加 YAGNI 负担
- 理由 2:D6 测试策略下 BGESmallEmbedder 的 mock 也无意义(无生产调用方)
- 理由 3:删除减少 coverage denominator,与 6A Task 8 同样的 YAGNI 决策

**例外**:如未来需要 dense-only 快速嵌入(例:debug/dry-run),按需新增,本 spec 不预设。

### D3: 模型供给(已决)
**预下载 vendor 进仓**,路径 `rag/models/bge-m3/`:
- `model_optimized.onnx`(bge-m3 ONNX,~2GB)
- `sentencepiece.bpe.model`(tokenizer,1MB)
- `config.json`(模型配置,~2KB)
- `bge-m3.sha256`(校验文件)
- 不走 Git LFS(决策 B),直接 commit(用户已批准)

`.gitignore` 例外:`!rag/models/bge-m3/` 与 `!rag/models/bge-m3/**` 允许进仓。

### D4: 数据迁移(Qdrant 集合 dim 变化)
**lifespan 启动时自动重建**(FastAPI 标准模式,用户反馈点 3):
- lifespan 是 FastAPI 标准 startup hook,在 server 接受请求前完成所有初始化
- `QdrantManager.ensure_collection(vector_size=1024)` 在 lifespan 内部调用,保证重建完成后服务才 listen
- 异步包装:`ensure_collection` 是 sync(网络阻塞 I/O),用 `asyncio.to_thread(ensure_collection)` 避免阻塞 event loop
- 检测到 dim 不匹配(384d vs 1024d)→ 删旧集合 → 重建(dense=1024d + sparse 已配置)
- 旧 384d 零向量被丢弃(零向量本无检索价值)
- 操作记入 logger INFO(迁移事件非业务事件,不入 audit)

**运维提示**:README + `.env.example` 注释说明首次部署后需触发 ingestion 重新推送所有文档(parser 侧)。

**AUTO_REINDEX 环境变量**(用户反馈点 8):
- 默认 `AUTO_REINDEX=true` → 启动时检测到 dim 不匹配自动重建
- `AUTO_REINDEX=false` → 检测到 dim 不匹配时 raise,要求 operator 手动处理(适合 prod 防误删)
- 该变量仅控制 Qdrant 集合 dim 重建,不控制 ingestion 数据(由 parser 侧处理)

### D5: retriever 简化
**采用方案 A**:`EKRSRetriever` 不再持有 `embedder`,只持有 `qdrant_manager`。`retrieve(query)` 改为:
```python
def retrieve(self, query, top_k, active_scope):
    hits = self._qdrant.search(query_text=query, top_k=top_k)  # 内部 encode
    ...
```

理由:Query-time 嵌入只服务 search,合并到 QdrantManager.search(query_text=...) 让 EmbeddingService 单一职责,retriever 关注 ranking + scope filter。

### D6: 测试分层(已决)
- **单元测试**:mock `FlagEmbedding.FlagModel` 返回固定 dense + sparse → 验证 QdrantManager.upsert/search 逻辑
- **集成测试 `@pytest.mark.heavy`**:1-2 个真实 bge-m3 调用,验证模型能加载 + 编码格式正确
- **CI 默认**:`pytest -m "not heavy"`(避开 2GB 模型加载)
- **nightly job**:`pytest -m heavy`(独立 schedule,允许慢)

`.github/workflows/test.yml` 增加 `heavy` job,触发条件 `schedule: cron: '0 3 * * *'`(每日 03:00 UTC)+ `workflow_dispatch`。

### D7: 16 audit 事件不变(回归保护)
- B1 修复后 search 失败 → 复用 `qdrant_write_failed`(用户反馈点 6:语义放宽,覆盖 read/write 全部 Qdrant 操作失败,handbook §16 同步更新语义说明)
- B3 零向量回退(模型加载失败)→ logger WARN,**不入 audit**(运维事件非业务事件)
- 集合重建 → logger INFO,**不入 audit**(迁移事件非业务事件)
- 新事件种类不增加(16 事件集冻结)

**qdrant_write_failed 语义放宽决议**(用户反馈点 6 决议):
- 原义:仅 Qdrant 写入失败
- 新义:Qdrant 任何操作失败(read/write/delete/upsert/scroll),event name 保留
- 理由:加新 event `qdrant_read_failed` 破坏 16 事件冻结;重命名 `qdrant_qdrant_operation_failed` 破坏已有 audit log 可读性;event payload 中 `operation: str` 字段可区分 read/write
- handbook §16 同步更新说明

**16 事件集冻结**(同 6A final review)。

### D8: Sparse 格式转换(用户反馈点 1)
Qdrant NamedVectors sparse 字段要求 `{"indices": [...], "values": [...]}` 格式,不是 `dict[int, float]`。**转换在 EmbeddingService 完成**,QdrantManager 不关心向量内部结构。

```python
# embedding_service.py
def to_qdrant_sparse(self, sparse: dict[int, float]) -> dict:
    """Convert {term_id: weight} dict to Qdrant sparse format.
    Returns: {"indices": sorted(term_ids), "values": [matching_weights]}
    """
    if not sparse:
        return {"indices": [], "values": []}
    indices = sorted(sparse.keys())
    values = [sparse[i] for i in indices]
    return {"indices": indices, "values": values}
```

**QdrantManager 职责边界**:
- EmbeddingService:encode + 内部格式转换(to_qdrant_sparse)
- QdrantManager:仅存储 + 检索(upsert/query_points),输入是 NamedVectors dict
- 这样 QdrantManager 不依赖任何 sparse 内部表示,未来换模型仅需替换 EmbeddingService

---

## §4 组件 & 数据流

### 新增文件

| 路径 | 用途 |
|------|------|
| `rag/ekrs_rag/retrieval/embedding_service.py` | `EmbeddingService` facade + `EncodedVector` dataclass |
| `rag/models/bge-m3/model_optimized.onnx` | bge-m3 ONNX 模型(~2GB,vendor) |
| `rag/models/bge-m3/sentencepiece.bpe.model` | bge-m3 tokenizer(1MB,vendor) |
| `rag/models/bge-m3/config.json` | 模型配置(2KB,vendor) |
| `rag/models/bge-m3/bge-m3.sha256` | 校验文件 |
| `rag/tests/unit/test_embedding_service.py` | EmbeddingService 单元测试(mock FlagEmbedding,6 例) |
| `rag/tests/integration/test_embedding_heavy.py` | 真实 bge-m3 调用(2 例,@pytest.mark.heavy) |
| `.github/workflows/heavy-tests.yml` | nightly heavy job |

### 修改文件

| 路径 | 改动 |
|------|------|
| `rag/ekrs_rag/retrieval/qdrant_client.py` | 3 bug 修复:`.search → query_points`、`vectors_config → config.params.vectors`、`upsert_chunks` 接 EmbeddingService;`search` 签名加 `query_text` 参数 |
| `rag/ekrs_rag/retrieval/retriever.py` | 移除 `embedder` 参数,改调 `qdrant.search(query_text=...)` |
| `rag/ekrs_rag/main.py` | lifespan:`QdrantManager(embedding_service=EmbeddingService())` + `get_embedding_service` Depends |
| `rag/ekrs_rag/api/dependencies.py` | 加 `get_embedding_service` Depends |
| `rag/ekrs_rag/retrieval/embedder.py` | **删除**(D2) |
| `rag/tests/unit/test_embedder.py` | **删除**(D2 连带) |
| `rag/tests/unit/test_qdrant_client.py` | **重写**(原地,8 例,适配新 QdrantManager 签名 + EmbeddingService 注入) |
| `pyproject.toml` (rag/) | 加 `FlagEmbedding` 依赖(需批准) |
| `.gitignore` | 例外 `!rag/models/bge-m3/**`(允许 vendor 进仓) |
| `ekrs-handbook.md` §7 | bge-m3 实现从"声明"改"已实现";新增 §7.4 首次部署流程(用户反馈点 8);§16 qdrant_write_failed 语义更新;§14 依赖清单加 FlagEmbedding/onnxruntime/numpy(版本锁定) |
| `.env.example` | 加 `AUTO_REINDEX=true` 注释说明(用户反馈点 8) |

### 数据流(检索 query)

```
6A:                                            6B:
client                                          client
  ↓ POST /v1/constraints                          ↓ POST /v1/constraints
retriever.retrieve(query)                       retriever.retrieve(query)
  ↓                                               ↓
embedder.encode([query])                        qdrant_manager.search(query_text=query)
  ↓ 384d                                          ↓ (内部)
qdrant.search(dense=...)  ←BUG: .search 删了       embedding_service.encode([query])
  ↓                                               ↓ 1024d dense + sparse
[元数据]                                          qdrant.query_points(NamedVectors{"dense": ..., "sparse": ...})
                                                 ↓
                                                 [元数据]
```

### 数据流(ingestion)

```
6A:                                            6B:
parser.notify → ingestion                       parser.notify → ingestion
  ↓                                               ↓
QdrantManager.upsert_chunks(chunks)             QdrantManager.upsert_chunks(chunks)
  ↓ 零向量 (BUG)                                  ↓
[Phase 1 dummy]                                 embedding_service.encode([c.text for c in chunks])
                                                 ↓ 1024d dense + sparse
                                                 qdrant.upsert(NamedVectors{"dense": ..., "sparse": ...})
```

### 错误处理

| 场景 | 行为 |
|------|------|
| 模型文件缺失 | startup WARN,EmbeddingService.is_dummy=True;encode 返回零向量 |
| ONNX 加载失败 | startup WARN + logger.exception,同上回退 |
| FlagEmbedding 未安装 | ImportError → 启动失败(direct dep,启动时检测) |
| Qdrant 集合 dim 不匹配 | lifespan 自动重建 + INFO 日志 |
| 真实检索时 Qdrant 不可达 | 既有 503,不动 |
| FlagEmbedding.encode 抛错 | 上抛到 retriever → 既有 500/503 路径 |

---

## §5 测试策略

### 单元测试(mock FlagEmbedding)

`test_embedding_service.py`(9 例,新增 3 例):
- `test_encode_returns_dense_and_sparse`
- `test_encode_handles_empty_list`
- `test_encode_normalizes_dense`
- `test_is_dummy_when_model_missing`
- `test_is_dummy_when_onnx_load_fails`
- `test_dense_size_returns_1024`
- `test_sha256_mismatch_raises_runtime_error`(用户反馈点 4 验证)
- `test_to_qdrant_sparse_converts_dict_format`(用户反馈点 1 + D8 验证)
- `test_to_qdrant_sparse_handles_empty_dict`

`test_qdrant_client.py`(原地重写,11 例,合并 6A 旧测试内容 + 用户反馈补充):
- `test_ensure_collection_creates_dense_and_sparse`
- `test_ensure_collection_recreates_on_dim_mismatch`(B2 验证)
- `test_ensure_collection_no_recreate_when_dim_matches`
- `test_upsert_chunks_encodes_via_embedding_service`(B3 验证)
- `test_upsert_chunks_uses_named_vectors`(dense + sparse 都进 PointStruct)
- `test_search_calls_query_points`(B1 验证)
- `test_search_encodes_query_text_via_service`
- `test_search_passes_named_vectors_to_query_points`
- `test_upsert_chunks_raises_when_embedding_service_dummy`(用户反馈点 2 验证)
- `test_ensure_collection_handles_qdrant_unreachable`(用户反馈点 3 验证,模拟连接异常)

`retriever.py` 修改后测试更新:
- `test_retrieve_no_longer_takes_embedder`
- `test_retrieve_calls_qdrant_search_with_text`

### 集成测试(`@pytest.mark.heavy`)

`test_embedding_heavy.py`(2 例):
- `test_real_bge_m3_encodes_english_text`:加载真模型,编码 `"hello world"`,断言 dense shape=(1024,),L2-norm=1.0,sparse 含常见英文 token id
- `test_real_bge_m3_handles_chinese`:加载真模型,编码中文 `"高温合金"`,断言 dense shape + sparse 含中文 token

### CI / nightly 配置

`.github/workflows/test.yml`(已有)增加:
```yaml
- name: Run tests (default, no heavy)
  run: |
    cd rag
    pytest tests/ -m "not heavy" --cov=ekrs_rag --cov-fail-under=85 -v
```

`.github/workflows/heavy-tests.yml`(新增):
```yaml
name: heavy-tests
on:
  schedule:
    - cron: '0 3 * * *'  # nightly 03:00 UTC
  workflow_dispatch:
jobs:
  heavy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: false  # 不用 LFS,但模型文件 2GB 已 in repo
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: python -m pip install -e shared/ -e 'rag[dev]'
      - name: Heavy tests (real bge-m3)
        run: cd rag && pytest tests/ -m heavy -v
```

### 回归保护

- Iron Rules 测试不需改(7 文件不动)
- 16 audit 事件 schema 兼容性:`tests/observability/test_audit_event_registry.py` 已固化,无需重测
- 现有 Qdrant unit tests(`test_qdrant_client.py`):重写适配新签名,但通过覆盖率保持 ≥85%

### 覆盖率目标

- 6A 基线:86.63%(≥85% gate 满足)
- 6B 新增模块:EmbeddingService(9 例) + QdrantManager 重写(11 例) + heavy 集成(2 例),目标 100%
- 删除 BGESmallEmbedder 后 denominator 减少(~30 LOC test_embedder.py),~ +1-2% 净覆盖率
- 末态预估:87-88%

---

## §6 迁移 & 部署

### 模型 vendor 步骤(T1)

1. 在 HF 下载 `BAAI/bge-m3` 的 ONNX 导出:
   - `model_optimized.onnx`(2GB)
   - `sentencepiece.bpe.model`
   - `config.json`
2. 计算 sha256,写入 `bge-m3.sha256`
3. 验证 ONNX 可用:`python -c "import onnxruntime as ort; ort.InferenceSession('rag/models/bge-m3/model_optimized.onnx', providers=['CPUExecutionProvider'])"` 加载成功
4. 复制到 `rag/models/bge-m3/`
5. 更新 `.gitignore`(移除例外排除,确认 `rag/models/bge-m3/` 不被 ignore)
6. **单独 commit**(模型二进制与代码分离审查,便于后续 reviewer 评估是否需要 LFS 迁移)

### Qdrant 集合 dim 迁移(T3)

- `QdrantManager.ensure_collection(vector_size=1024)` 检测到 dim 变化 → `delete_collection` + 重建
- 重建时机:lifespan 启动
- 旧 384d 集合数据丢失(parser 需重新推送所有文档)
- 日志:INFO "Rebuilding Qdrant collection: dim mismatch (was 384, now 1024)"

### 配置变更

`.env.example` 无新增(模型路径 hardcode 到 `rag/models/bge-m3/`)。

`pyproject.toml` (rag/) 新增(用户反馈点 5,版本锁定):
```toml
[dependencies]
FlagEmbedding = "==1.2.13"     # 锁定 1.2.13,实测与 bge-m3 ONNX 兼容
onnxruntime = ">=1.15.0,<1.18.0"  # 锁定 1.15-1.18,FlagEmbedding 兼容范围
numpy = ">=1.24.0,<2.0.0"        # 锁定 <2.0,FlagEmbedding 内部使用 1.x API
```

**此为 6A "no new external deps" 例外**,已由 user 在 6B spec review 中显式批准。

**新依赖在 handbook §14 同步登记**(用户反馈点 5)。

### 后向兼容

- API 契约:检索结果 schema 不变(`Chunk` + `score`)
- 重部署:**首次部署后所有文档需重新 ingestion**(旧 384d 集合被丢弃)
- audit log:无 schema 变化,旧 trace 仍可读

### 部署顺序

5 个 commit + 1 个 tag(对应 6 个任务):

1. **T1**:vendor bge-m3 模型(单 commit,~2GB 二进制,文档 review 重点)
2. **T2**:EmbeddingService 骨架 + FlagEmbedding 集成 + 单元测试
3. **T3**:QdrantManager 重写(B1/B2/B3 修复)+ EmbeddingService 注入 + 集成测试
4. **T4**:retriever 简化 + main.py Depends 迁移 + 删除 BGESmallEmbedder
5. **T5**:heavy 集成测试 + CI / nightly 配置
6. **T6**:handbook §7/§14 同步 + progress.md 更新
7. **打 tag**:`phase6b-retrieval-layer`

每步 commit + subagent review 闸门。

### 风险评估

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| FlagEmbedding pip 安装慢/失败 | 中 | 中 | Dockerfile 层缓存 `pip install FlagEmbedding` 单独 step |
| onnxruntime 版本不兼容 | 中 | 高 | 版本锁定 `>=1.15,<1.18`,handbook §14 记录(用户反馈点 5) |
| bge-m3 ONNX 加载内存 ~2GB | 高 | 中 | lifespan startup 显式 warning;测试在 dummy 模式 |
| 模型 vendor 增加仓库 2GB | 中 | 低 | 用户已批准,接受 clone 慢;CI 用 `--depth=1` |
| Qdrant 集合重建丢失旧数据 | 高 | 中 | 部署前 backup;`AUTO_REINDEX=false` 可禁止;README + handbook §7.4 提示需要 parser 重推 |
| Dummy 模式 upsert 写无效数据 | 中 | 中 | D1 强化:`is_dummy=True` 时 `upsert_chunks` raise `EmbeddingUnavailableError`(用户反馈点 2) |
| FlagEmbedding 新增依赖 | 中 | 中 | user 显式批准(本 spec §7 决议) |
| BGESmallEmbedder 删除破坏外部调用方 | 低 | 低 | 全仓 grep 验证无外部引用 |
| 模型文件损坏 | 低 | 中 | D1 强化:SHA256 校验失败直接 raise,不允许 dummy 回退(用户反馈点 4) |
| Qdrant 集合重建时收到请求 | 中 | 低 | D4 强化:lifespan 内 `asyncio.to_thread(ensure_collection)` 阻塞,服务 listen 前完成(用户反馈点 3) |

### 回滚

- 每 commit 独立可 git revert
- 模型 vendor 步骤 revert → 仓库重新瘦身
- Qdrant 集合重建无 destructive(旧集合已被删,但 384d 数据本就无价值)

---

## §7 未解决问题

### 已决(本 spec 中固化)
- ✅ 6B 范围 = 3 bug + bge-m3 升级 — §1 锁定
- ✅ EmbeddingService facade 架构 — D1 锁定
- ✅ BGESmallEmbedder 删除 — D2 锁定
- ✅ 模型 vendor 进仓(D3 选项 B)— D3 锁定
- ✅ 不走 Git LFS(D3 选项 B 子项)— D3 锁定
- ✅ Qdrant 集合 dim 不匹配自动重建 — D4 锁定
- ✅ retriever 移除 embedder 注入 — D5 锁定
- ✅ 测试分层 mock + heavy — D6 锁定
- ✅ 16 audit 事件不变,D1-D7 失败不入 audit — D7 锁定

### 待解(不阻塞 spec,实施时定或 user 确认)
- ✅ **R1: FlagEmbedding 新增依赖** — user 在 6B spec review 中批准(含版本锁定)
- ❓ **R2: bge-m3 ONNX 文件确切清单** — HF 上 BAAI/bge-m3 的 ONNX 导出含哪些文件?需在 T1 实施时下载验证
- ✅ **R3: FlagEmbedding 版本约束** — 锁定 `FlagEmbedding==1.2.13` + `onnxruntime>=1.15,<1.18` + `numpy<2.0`(用户反馈点 5)
- ❓ **R4: 集合重建触发 parser 重新推送的运维脚本** — 是否在 6B 范围内?倾向 6C+ 运维脚本,6B 仅补 README
- ❓ **R5: EmbeddingService 在 dummy 模式下 upsert 零向量是否会被新 query_points 拒?** — 已由 D1 强化(禁止写入),无需此问题
- ❓ **R6: 是否保留 bge-small-en 作为轻量快速嵌入备选** — D2 已决定删除,本 spec 不保留
- ✅ **R7: handbook §7 同步更新** — T6 必须新增"模型路径 / vendor 决策 / dummy 回退 / AUTO_REINDEX"小节(用户反馈点 8)

---

## §8 实施顺序(垂直切片,每项独立闭环)

按 6A 同样的"每项独立 TDD 小循环 + commit + review"模式:

1. **T1 — 模型 vendor**:下载 bge-m3 ONNX + tokenizer + config → vendor 到 `rag/models/bge-m3/` → sha256 校验 → 单 commit。**重点 review**:模型版本 + 文件清单 + .gitignore 例外。
2. **T2 — EmbeddingService**:新建 `embedding_service.py` + `test_embedding_service.py`(6 例 mock)。FlagEmbedding 集成测试。**重点 review**:facade 接口设计 + dummy 回退。
3. **T3 — QdrantManager 重写**:B1/B2/B3 修复 + upsert_chunks 注入 EmbeddingService + search 改 query_points + ensure_collection 改 config.params.vectors。重写 `test_qdrant_bge_m3.py`(8 例 mock)。**重点 review**:3 bug 修复正确性 + named vectors 结构。
4. **T4 — retriever + main.py**:retriever 移除 embedder 参数 + 调用改 `qdrant.search(query_text=...)`。main.py lifespan 注入 EmbeddingService + Depends 迁移。删除 `embedder.py` + `test_embedder.py`。**重点 review**:retriever 行为不变 + Depends 接线。
5. **T5 — 测试分层**:新增 `test_embedding_heavy.py`(2 例,@pytest.mark.heavy)。`.github/workflows/heavy-tests.yml` 新增 nightly job。**重点 review**:mark 注册 + CI 配置。
6. **T6 — 文档同步**:handbook §7 "bge-m3 实现" + §14 依赖清单加 FlagEmbedding/onnxruntime/numpy + 新增"§7.4 首次部署流程"段(用户反馈点 8);handbook §16 qdrant_write_failed 语义更新(用户反馈点 6);.env.example 加 `AUTO_REINDEX` 注释;progress.md 更新 6B 状态。**重点 review**:文档与代码一致。
7. **打 tag**:`phase6b-retrieval-layer`(在 master 上,本地)

每步 commit + subagent task reviewer 闸门。T1 因 2GB 模型文件需特殊 review(代码变更 0,但文件大小 + 内容审查)。

### 任务依赖图

```
T1 (vendor) ─→ T2 (EmbeddingService) ─→ T3 (QdrantManager 重写) ─→ T4 (retriever/main) ─→ T5 (tests) ─→ T6 (docs) ─→ tag
                              │                                │
                              └→ T5 (heavy 测试需 T2 真实接口) ┘
```

T1 是 T2 的前置(模型加载需要文件);T2 是 T3 的前置(QdrantManager 依赖 EmbeddingService);T3 是 T4 的前置(retriever 用新签名);T5 依赖 T2-T4 全部;T6 全部完成后。

并行机会:T6 文档同步可与 T5 测试并行(T5 完成后开始 T6 同步即可)。