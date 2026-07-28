# EKRS 广谱检索移植设计报告

> **研究文档 — 设计研究，含伪代码，不含可运行实现。**
> 日期：2026-07-24
> 前序研究：
> - [`2026-07-24-mineru-explorer-feature-mapping.md`](2026-07-24-mineru-explorer-feature-mapping.md)（功能映射）
> - [`2026-07-24-mineru-deep-dive-extensions.md`](2026-07-24-mineru-deep-dive-extensions.md)（深度分析）
> - [`2026-07-24-ekrs-mineru-integration-feasibility.md`](2026-07-24-ekrs-mineru-integration-feasibility.md)（外部集成方案）
>
> 本报告定位：前序文档要么研究"把 QMD 设计模式照搬到 EKRS"，要么研究"把 mineru-explorer 作为外部服务接入"。**本报告研究第三条路径：把 mineru-explorer 的核心算法用 Python 重写到 EKRS 代码库内部，同时评估 zvec / turbovec 替换 Qdrant 的可行性**——让 EKRS 在确定性求解器之上增加自然语言广谱检索能力，且不引入任何外部常驻服务。

---

## 目录

1. [需求分析与目标定义](#1-需求分析与目标定义)
2. [四项目对比定位](#2-四项目对比定位)
3. [存储引擎评估](#3-存储引擎评估zvec-vs-turbovec-vs-qdrant)
4. [移植特性深度分析](#4-移植特性深度分析)
5. [广谱检索流水线设计](#5-广谱检索流水线设计)
6. [架构设计](#6-架构设计)
7. [风险评估](#7-风险评估)
8. [分阶段实施计划](#8-分阶段实施计划)
9. [结论与建议](#9-结论与建议)

---

## 1. 需求分析与目标定义

### 1.1 什么是"自然语言广谱检索"

EKRS 当前的检索能力是**单一向量语义检索**：

```
用户查询 → bge-m3 编码 → Qdrant 向量搜索 → scope 复合评分 → top-k chunks
```

这条路径对**语义相似性**处理优秀（"设计压力"能匹配到"design pressure"），但对**精确标识符匹配**薄弱。工程文档中充斥着：

| 标识符类型 | 示例 | 向量检索表现 |
|-----------|------|-------------|
| 压力等级 | `1.6MPa`、`Class600` | 差 — bge-m3 倾向语义切分 |
| 材料牌号 | `A312-TP316`、`20#`、`Q345R` | 差 — 子词切分破坏牌号完整性 |
| 标准编号 | `GB/T 12459`、`ASME B31.3` | 中 — 数字部分丢失 |
| 温度区间 | `-196°C`、`+540°C` | 差 — 负号和单位被语义化 |

**广谱检索的目标**：在现有向量语义检索之上，增加 BM25 关键词精确匹配路径，通过 RRF 融合两条检索结果，使 EKRS 同时具备：

- **精确匹配**能力（BM25）：`A312-TP316`、`GB/T 12459` 原样命中
- **语义匹配**能力（向量）：已有的 bge-m3
- **广谱检索**= 两者并行 + 融合，覆盖"从精确标识符到自然语言描述"的全频谱

### 1.2 广谱检索 vs 精确检索的边界

| 维度 | 约束求解（现有） | 广谱检索（新增） |
|------|-----------------|-----------------|
| 查询类型 | "设计压力 1.6MPa 容器的壁厚要求" | "这个项目用了哪些不锈钢材料" |
| 输出 | 结构化数值区间（per parameter） | 文档片段排序列表 + score |
| 确定性 | 确定性（R2 纯函数） | 确定性（BM25 + 向量 + RRF 均为确定性操作） |
| 流水线位置 | Gate 1→2→3 | Gate 1 增强层（可选重排在 Gate 1.5） |
| 返回给求解器 | RetrievalResult（含 hints） | 不变 — 只是召回更准 |

**关键边界**：广谱检索不改变求解器的输入/输出契约。它只改善 Gate 1（召回阶段）的召回质量。求解器仍然收到 `RetrievalResult`，仍然走 R2 纯函数路径。

### 1.3 与铁律 R1-R8 的兼容性分析

| 铁律 | 要求 | 广谱检索兼容性 | 理由 |
|------|------|---------------|------|
| R1 | hint 携带 source_span/block_id/context_window | ✅ 兼容 | BM25 结果仍映射到 EKRS 的 Chunk 模型，hint 提取不变 |
| R2 | 求解器是纯函数 | ✅ 兼容 | BM25/RRF 在检索层，不在求解器。求解器接口不变 |
| R3 | 三闸门流水线 | ✅ 兼容 | 广谱检索增强 Gate 1 召回，Gate 2/3 不变 |
| R4 | 上下文优先级 | ✅ 兼容 | scope_priority 复合评分保留，RRF 结果再过 scope 过滤 |
| R5 | 仅 scope_path 层级 | ✅ 兼容 | FTS5 是虚拟表，不是图数据库 |
| R6 | strict=true 禁止推断 | ✅ 兼容 | BM25/RRF 是确定性检索，不是推断 |
| R7 | hint 携带 scope_path | ✅ 兼容 | Chunk.scope_path 从 Qdrant payload 继承 |
| R8 | 索引层仅过滤非法状态 | ✅ 兼容 | FTS5 表的 WHERE 子句仅过滤 `status != 'illegal'` |

**结论**：广谱检索的确定性组件（BM25 + 向量 + RRF）与全部八条铁律兼容。非确定性组件（LLM 重排、查询扩展）必须隔离在 EKRS 流水线之外，仅作为 Agent 直接调用的可选工具。

---

## 2. 四项目对比定位

### 2.1 技术栈对比

| 维度 | EKRS | mineru-explorer (QMD) | zvec (Zvec) | turbovec |
|------|------|----------------------|-------------|----------|
| 语言 | Python 3.11+ | TypeScript (Node 22+) | C++ 核心 + Python/Node/Go/Rust 绑定 | Rust 核心 + Python 绑定 (maturin) |
| 存储引擎 | Qdrant (外部服务) + aiosqlite + Redis | SQLite + sqlite-vec + FTS5 (嵌入式) | 自研嵌入式引擎 (RocksDB + WAL) | 内存索引 (.tv/.tvim 文件持久化) |
| 嵌入模型 | bge-m3 ONNX (1024d dense+sparse, ~2.1GB) | embeddinggemma-300M GGUF (~300MB) | 任意（用户自带向量） | 任意（用户自带向量） |
| 全文检索 | ❌ 无 | ✅ FTS5 (porter + unicode61 分词) | ✅ 原生 FTS (UAX#29 分词 + 34 语言词干提取) | ❌ 无 |
| 向量检索 | Qdrant (HNSW, dense+sparse) | sqlite-vec (余弦相似度) | HNSW / HNSW-RaBitQ / DiskANN / Flat | TurboQuant (SIMD, 2-bit/4-bit 量化) |
| 混合搜索 | ❌ 无 | BM25 + 向量 + RRF + cross-encoder 重排 | 向量 + FTS + 标量过滤 (单次查询) | 向量 + allowlist 过滤 |
| 重排序 | ❌ 无 | qwen3-reranker-0.6b (cross-encoder logprob) | 内置 reranker (multi_vector_reranker.py) | ❌ 无 |
| 查询扩展 | ❌ 无 | qmd-query-expansion-1.7B (lex/vec/hyde 变体) | ❌ 无 | ❌ 无 |
| 压缩 | 无（float32 原始向量） | 无（float32 原始向量） | INT8/INT4 量化 + 随机旋转 | 2-bit/4-bit TurboQuant (16x 压缩) |
| Agent 集成 | HTTP API only | MCP Server (15 工具, stdio + HTTP) | 无 | LangChain/LlamaIndex/Haystack 适配器 |
| 持久化 | Qdrant 磁盘 + SQLite WAL | SQLite 文件 | WAL 预写日志 | 文件序列化 (.tv/.tvim) |
| 并发 | 多读者 (Qdrant) | 单进程 (better-sqlite3) | 多进程并发读，单进程写 | 进程内（无并发原语） |
| 部署 | docker-compose (4 服务) | npm 包，进程内 | pip install zvec，进程内 | pip install turbovec，进程内 |

### 2.2 检索能力矩阵

| 检索能力 | EKRS 现状 | QMD | zvec | turbovec |
|----------|----------|-----|------|----------|
| 稠密向量搜索 | ✅ Qdrant | ✅ sqlite-vec | ✅ HNSW/RaBitQ | ✅ TurboQuant SIMD |
| 稀疏向量搜索 | ✅ Qdrant sparse | ❌ | ✅ sparse index | ❌ |
| BM25 关键词搜索 | ❌ | ✅ FTS5 | ✅ 原生 FTS | ❌ |
| 混合检索 (BM25+向量) | ❌ | ✅ RRF k=60 | ✅ 单查询融合 | ❌ |
| RRF 融合 | ❌ | ✅ (k=60, 原始×2 权重) | ✅ (内置 RRF/Weighted reranker) | ❌ |
| Cross-encoder 重排 | ❌ | ✅ qwen3-reranker | ✅ multi_vector_reranker | ❌ |
| 查询扩展 | ❌ | ✅ (1.7B 模型) | ❌ | ❌ |
| 位置感知混合 | scope_priority 复合评分 | ✅ 75%/60%/40% 三档 | ❌ | ❌ |
| 边界感知分块 | scope-aware chunker | ✅ break-point scoring | N/A | N/A |
| Allowlist 过滤 | scope_path 前缀匹配 | collection 过滤 | 标量过滤 | ✅ SIMD 内核级 allowlist |

### 2.3 角色定位

```
┌─────────────────────────────────────────────────────────────────┐
│                        研究目标                                   │
│                                                                 │
│   EKRS (移植目标)  ◄──── 特性来源 ────  mineru-explorer (QMD)   │
│   Python / FastAPI              设计模式 + 算法                  │
│   确定性求解器 + RAG                                             │
│        ▲                                                        │
│        │                                                        │
│   存储引擎候选                                                   │
│   ┌────┴─────────────────────┐                                  │
│   │ zvec         turbovec    │                                  │
│   │ 嵌入式 + FTS  极致压缩    │                                  │
│   │ (可能替换 Qdrant)         │                                  │
│   └──────────────────────────┘                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 存储引擎评估（zvec vs turbovec vs Qdrant）

### 3.1 Qdrant 现状分析

EKRS 当前使用 Qdrant 1.11 作为向量存储（`rag/ekrs_rag/retrieval/qdrant_client.py`）：

**优势**：
- 生产级外部服务，支持分布式部署
- 支持 dense + sparse 向量（bge-m3 1024d）
- 成熟的 HNSW 索引
- 已与 EKRS 的 `EmbeddingService` 深度集成（Phase 6B 重写）
- Phase 7 的 tenacity 重试 + 审计失败发射机制成熟

**劣势**：
- **独立服务进程**：docker-compose 中需要额外维护一个 Qdrant 容器
- **无全文检索**：仅支持向量搜索，BM25 需要另建 SQLite FTS5
- **内存占用**：float32 原始向量，无量化压缩
- **运维复杂度**：是 4 个服务（qdrant + redis + rag + prometheus）之一

### 3.2 Zvec 评估

**Zvec 是阿里巴巴开源的嵌入式向量数据库**（`/home/pangzy/code_project/zvec-clone`），C++ 核心 + Python 绑定，`pip install zvec`。

**关键特性**（v0.6.0, 2026-07-20）：

1. **进程内运行**：无需独立服务，`import zvec` 即可用。消除 Qdrant 容器
2. **原生 FTS**：基于 Unicode UAX #29 标准分词器 + Snowball 词干提取（支持 34+ 种语言），Block-max 跳跃优化使 FTS 合取查询提速 22-38%
3. **混合搜索**：在单次 `collection.query()` 中融合向量相似度、全文检索和标量过滤
4. **WAL 持久化**：预写日志保证数据持久性，进程崩溃不丢数据
5. **量化压缩**：INT8/INT4 量化 + 随机旋转（将方差均匀分布到各维度，提升召回率）
6. **多种索引**：Flat / HNSW / HNSW-RaBitQ / DiskANN / sparse
7. **多进程并发读**：多个进程可同时读取同一个 Collection

**Python API 示例**（来自 `zvec-clone/python/tests/test_collection_fts_vector_hybrid.py`）：

```python
import zvec

# 创建 collection（含 FTS 字段 + 向量字段）
schema = zvec.CollectionSchema(
    name="ekrs_chunks",
    fields=[
        zvec.FieldSchema(name="text", dtype=zvec.DataType.STRING),
        zvec.FieldSchema(name="scope_path", dtype=zvec.DataType.STRING),
        zvec.FieldSchema(name="doc_hash", dtype=zvec.DataType.STRING),
    ],
    vectors=[
        zvec.VectorSchema("embedding", zvec.DataType.VECTOR_FP32, 1024),
    ],
)
collection = zvec.create_and_open(path="./ekrs_zvec", schema=schema)

# 混合搜索：FTS + 向量 + 标量过滤
results = collection.query(
    zvec.Query(field_name="text", query_text="1.6MPa 法兰"),  # FTS
    zvec.Query(field_name="embedding", vector=query_vec),      # 向量
    topk=40,
)
```

**内置 reranker**（`zvec-clone/python/zvec/multi_vector_reranker.py`）：
- 支持 RRF 融合和加权重排
- 可配置多个查询变体的权重

**与 EKRS 的兼容性**：
- ✅ 支持 1024d 向量（bge-m3 的维度）
- ✅ 原生 FTS 可替代独立 SQLite FTS5 表
- ✅ 进程内运行可消除 Qdrant 容器
- ✅ WAL 持久化可替代 Qdrant 的磁盘存储
- ⚠️ 不原生支持 sparse 向量（bge-m3 的 sparse 输出需适配）
- ⚠️ C++ 编译依赖（需要预编译 wheel 或源码构建）

### 3.3 Turbovec 评估

**Turbovec 是基于 Google Research 的 TurboQuant 算法的 Rust 向量索引**（`/home/pangzy/code_project/turbovec`），ICLR 2026 论文。

**关键特性**：

1. **极致压缩**：10M 文档 31GB(float32) → 4GB(4-bit)，16x 压缩
2. **SIMD 加速**：手写 NEON (ARM) 和 AVX-512BW (x86) 内核，比 FAISS FastScan 快 10-19% (ARM)
3. **在线摄入**：添加向量即索引，无训练步骤，无参数调优，无重建
4. **Allowlist 过滤**：`search(query, k, allowlist=ids)` 在 SIMD 内核内部过滤，无需 over-fetch
5. **Length-renormalized scoring**：编码时计算一个标量修正因子，使内积估计无偏

**Python API**：

```python
from turbovec import TurboQuantIndex, IdMapIndex
import numpy as np

index = IdMapIndex(dim=1024, bit_width=4)
index.add_with_ids(vectors, np.array([1, 2, 3], dtype=np.uint64))
scores, ids = index.search(query_vec, k=40, allowlist=allowed_ids)
```

**与 EKRS 的兼容性**：
- ✅ 支持任意维度（bge-m3 1024d 可用）
- ✅ 极低内存占用（4GB vs Qdrant 的 31GB 等价语料）
- ✅ Allowlist 过滤可与 EKRS 的 scope_path 前缀匹配结合
- ❌ **无全文检索**：纯向量索引，BM25 需另建 SQLite FTS5
- ❌ 不支持 sparse 向量
- ❌ 无服务端标量过滤（allowlist 在客户端传入）

### 3.4 综合评估矩阵

| 评估维度 | Qdrant (现状) | Zvec | Turbovec | Turbovec + SQLite FTS5 |
|----------|--------------|------|----------|----------------------|
| **FTS 内置** | ❌ | ✅ 原生 | ❌ | ✅ (SQLite 补充) |
| **混合搜索** | ❌ | ✅ 单查询 | ❌ | ⚠️ 需手动 RRF |
| **嵌入运行** | ❌ 外部服务 | ✅ 进程内 | ✅ 进程内 | ✅ 进程内 |
| **压缩比** | 1x (float32) | INT8/INT4 | 16x (4-bit) | 16x |
| **Sparse 向量** | ✅ | ⚠️ 需适配 | ❌ | ❌ |
| **持久化** | 磁盘 + WAL | WAL | 文件 | 文件 + SQLite WAL |
| **并发读** | ✅ 多读者 | ✅ 多进程 | 进程内 | 进程内 |
| **运维成本** | 高 (独立容器) | 低 (pip install) | 低 (pip install) | 低 |
| **成熟度** | 生产级 | 阿里巴巴生产验证 | ICLR 论文级 | 组合方案 |
| **CJK 支持** | N/A | ✅ UAX#29 + 中文优化 | N/A | 取决于 FTS5 配置 |
| **替换工作量** | — | 高 (重写 QdrantManager) | 高 (重写 + 补 FTS) | 高 |

### 3.5 存储引擎推荐

**推荐：Phase 9 保持 Qdrant + 新增 SQLite FTS5；Phase 10+ 评估 Zvec 替换。**

理由：

1. **Phase 9 的核心价值是检索增强，不是存储替换**。Qdrant 已经稳定运行（Phase 1-8），`QdrantManager` 与 `EmbeddingService` 深度集成。替换存储引擎是一个独立的大工程，不应与检索增强混在一起。

2. **FTS5 可以独立于 Qdrant 添加**。SQLite FTS5 虚拟表与 Qdrant 并行，通过 `AuditWriter` 同步。这是前序研究（feature-mapping §1）推荐的方案，工作量可控（3-5 任务）。

3. **Zvec 是 Phase 10+ 的有力候选**。如果 Phase 9 的 BM25+向量混合搜索验证有效，Phase 10 可以评估用 Zvec 替换 Qdrant+SQLite 组合：
   - Zvec 原生 FTS + 向量 + 混合搜索 = 消除两个独立存储
   - 嵌入式运行 = 消除 Qdrant 容器，docker-compose 从 4 服务减到 2 服务
   - 但需要评估 sparse 向量缺失对召回的影响（bge-m3 的 sparse 输出是 Phase 6B 的核心特性）

4. **Turbovec 不适合单独替换 Qdrant**（无 FTS），但其 TurboQuant 算法值得关注——如果 Zvec 的量化压缩不够，可以借鉴 Turbovec 的 length-renormalization 技术。

---

## 4. 移植特性深度分析

### 4.1 FTS5 BM25 混合检索（优先级 1 — 广谱检索基础）

#### a) QMD 中的实现

QMD 的 BM25 检索实现在 `src/search.ts:164-196`（`searchFTS` 函数）：

```typescript
// search.ts:164 — FTS5 查询
export function searchFTS(db: Database, query: string, limit: number = 20, collectionName?: string): SearchResult[] {
  const ftsQuery = buildFTS5Query(query);  // 构建 FTS5 MATCH 表达式
  if (!ftsQuery) return [];

  let sql = `
    SELECT ..., bm25(documents_fts, 2.0, 5.0, 1.0) as bm25_score
    FROM documents_fts f
    JOIN documents d ON d.id = f.rowid
    JOIN content ON content.hash = d.hash
    WHERE documents_fts MATCH ? AND d.active = 1
  `;
  // ...
}
```

FTS5 查询构建在 `search.ts:99-144`（`buildFTS5Query`）：
- 支持短语匹配 (`"phrase"`)
- 支持前缀匹配 (`"term"*`)
- 支持 AND 连接
- 支持 NOT 排除

BM25 分数归一化（`search.ts:187-188`）：
```typescript
const rawScore = Math.abs(row.bm25_score) / (1 + Math.abs(row.bm25_score));
const score = rawScore < 0.01 ? 0.01 : rawScore;
```

#### b) EKRS 当前对应能力

EKRS **没有** BM25 检索能力。当前检索路径（`retriever.py:41-82`）：

```python
class EKRSRetriever:
    def retrieve(self, query: str, top_k: int = 40, ...):
        hits = self._qdrant.search(query_text=query, top_k=top_k)  # 仅向量搜索
        # ...
        chunks, vector_scores, scope_scores, final_scores = self._rank_by_scope(chunks, vector_scores)
```

`_rank_by_scope` 的复合评分（`retriever.py:95`）：
```python
final_scores = [vec * (1 + scope) for vec, scope in zip(vector_scores, scope_scores)]
```

#### c) 移植方案设计

**新增模块**：`rag/ekrs_rag/retrieval/fts_manager.py`

```python
"""FTS5 BM25 全文检索管理器 — Phase 9 新增。

镜像 Qdrant 的 chunk payload 到 SQLite FTS5 虚拟表，
提供与向量检索并行的关键词检索路径。
"""
import sqlite3
import logging
from typing import List, Tuple, Optional
from pathlib import Path

from ekrs_shared.models import Chunk
from ekrs_constants import get_hermes_home

logger = logging.getLogger(__name__)


class FTSManager:
    """SQLite FTS5 全文索引管理器。

    表结构镜像 Qdrant payload：
    - block_id (UNINDEXED — 仅存储，不索引)
    - text (INDEXED — BM25 主字段)
    - scope_path (INDEXED — 用于 scope 感知评分)
    - status (UNINDEXED — R8: 仅过滤非法状态)
    - doc_hash (UNINDEXED — 文档溯源)
    """

    SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
        block_id UNINDEXED,
        text,
        scope_path,
        status UNINDEXED,
        doc_hash UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'  # [已裁决见 ADR] 不用 porter（对中文无效）
    );
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_hermes_home() / "ekrs_fts.db"
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(self.SCHEMA)
        self._conn.commit()

    def upsert(self, chunk: Chunk, block_id: str) -> None:
        """插入或更新 FTS 索引（与 Qdrant upsert 配对）。"""
        scope_str = " ".join(chunk.scope_path) if chunk.scope_path else ""
        status = "active"  # R8: 默认 active，非法状态由摄取层标记
        self._conn.execute(
            "INSERT INTO blocks_fts (block_id, text, scope_path, status, doc_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (block_id, chunk.text, scope_str, status, chunk.doc_hash),
        )
        self._conn.commit()

    def search(
        self,
        query: str,
        limit: int = 40,
        scope_filter: Optional[List[str]] = None,
    ) -> List[Tuple[str, float]]:
        """BM25 关键词搜索。返回 [(block_id, bm25_score), ...]。

        score 归一化到 [0, 1] 区间（与 QMD 一致）。
        """
        fts_query = self._build_fts5_query(query)
        if not fts_query:
            return []

        sql = (
            "SELECT block_id, "
            "  abs(bm25(blocks_fts)) / (1 + abs(bm25(blocks_fts))) as score "
            "FROM blocks_fts "
            f"WHERE blocks_fts MATCH ? AND status != 'illegal' "  # R8: 过滤非法
        )
        params = [fts_query]
        if scope_filter:
            scope_pattern = " ".join(scope_filter)
            sql += "AND scope_path MATCH ? "
            params.append(scope_pattern)
        sql += "ORDER BY bm25(blocks_fts) ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [(row[0], max(row[1], 0.01)) for row in rows]

    @staticmethod
    def _build_fts5_query(query: str) -> Optional[str]:
        """构建 FTS5 MATCH 表达式（移植自 QMD buildFTS5Query）。"""
        positive = []
        negative = []
        # 简化版：按空格分割，每个词加前缀通配符
        for term in query.strip().split():
            if term.startswith("-"):
                sanitized = term[1:].lower()
                if sanitized:
                    negative.append(f'"{sanitized}"')
            else:
                sanitized = term.lower()
                if sanitized:
                    positive.append(f'"{sanitized}"*')  # 前缀匹配
        if not positive:
            return None
        result = " AND ".join(positive)
        for neg in negative:
            result += f" NOT {neg}"
        return result

    def delete_by_doc(self, doc_hash: str) -> int:
        """删除文档的所有 FTS 索引（与 Qdrant delete_old_versions 配对）。"""
        cur = self._conn.execute(
            "DELETE FROM blocks_fts WHERE doc_hash = ?", (doc_hash,)
        )
        self._conn.commit()
        return cur.rowcount
```

**摄取层同步**（在 `pipeline.py` 的 `ingest()` 方法中，Qdrant upsert 之后）：

```python
# pipeline.py — 摄取流水线增加 FTS 同步
async def ingest(self, doc_hash: str):
    # ... 现有：读取 JSONL → 分块 → 编码 → Qdrant upsert ...

    # Phase 9 新增：FTS5 同步
    for chunk, block_id in zip(chunks, block_ids):
        try:
            self._fts_manager.upsert(chunk, block_id)
        except Exception as e:
            logger.warning("FTS sync failed for block %s: %s", block_id, e)
            # 审计事件（与 qdrant_write_failed 模式一致）
            audit_writer.write("fts_sync_failed", {"block_id": block_id, "error": str(e)})
```

#### d) 铁律兼容性检查

| 铁律 | 检查 | 结论 |
|------|------|------|
| R1 | FTS 结果映射到 Chunk 模型，hint 提取仍走 extract_hints() | ✅ |
| R2 | FTS 在检索层，求解器接口不变 | ✅ |
| R3 | FTS 增强 Gate 1 召回，Gate 2/3 不变 | ✅ |
| R4 | scope_priority 复合评分在 RRF 融合后应用 | ✅ |
| R5 | FTS5 是 SQLite 虚拟表，不是图数据库 | ✅ |
| R6 | BM25 是确定性检索，不是推断 | ✅ |
| R7 | Chunk.scope_path 从 Qdrant payload 继承 | ✅ |
| R8 | WHERE status != 'illegal' 仅过滤非法状态 | ✅ |

#### e) 工作量评估

| 任务 | 描述 | 工作量 |
|------|------|--------|
| T-FTS-1 | 实现 FTSManager 类（建表/CRUD/搜索） | 中 |
| T-FTS-2 | 摄取流水线集成 FTS 同步 | 小 |
| T-FTS-3 | 删除文档时同步清理 FTS 索引 | 小 |
| T-FTS-4 | BM25 分数归一化 + FTS5 查询构建器 | 小 |
| T-FTS-5 | 集成测试：golden set 回归 + 工程标识符用例 | 中 |

---

### 4.2 RRF 融合算法（优先级 2）

#### a) QMD 中的实现

QMD 的 RRF 实现在 `src/search.ts` 的 `reciprocalRankFusion` 函数。核心算法：

```typescript
// RRF 融合（k=60，原始查询列表 ×2 权重，top-1 加 0.05 bonus）
// 多个排序列表 → 单一排序列表
// score(doc) = Σ (weight_i / (k + rank_i))
```

位置感知混合在 `hybrid-search.ts:196-205`（`buildResult` 函数）：

```typescript
let rrfWeight = 1.0;
if (rerankScore > 0) {
    if (rrfRank <= 3) rrfWeight = 0.75;       // top 1-3: 信任 RRF
    else if (rrfRank <= 10) rrfWeight = 0.60;  // top 4-10: 平衡
    else rrfWeight = 0.40;                      // top 11+: 信任 reranker
    blendedScore = rrfWeight * rrfScore + (1 - rrfWeight) * rerankScore;
}
```

#### b) EKRS 当前对应能力

EKRS **没有** RRF 融合。当前评分是单一公式（`retriever.py:95`）：

```python
final_scores = [vec * (1 + scope) for vec, scope in zip(vector_scores, scope_scores)]
```

#### c) 移植方案设计

**新增模块**：`rag/ekrs_rag/retrieval/rrf_fusion.py`

```python
"""Reciprocal Rank Fusion — 移植自 QMD search.ts。

将多个排序结果列表融合为单一排序列表。
EKRS 用于融合 Qdrant 向量结果 + FTS5 BM25 结果。
"""
from typing import List, Tuple, Dict
from dataclasses import dataclass


@dataclass
class FusedResult:
    """RRF 融合后的单个结果。"""
    block_id: str
    rrf_score: float
    vector_rank: int    # 在向量列表中的排名（0 = 未出现）
    fts_rank: int       # 在 FTS 列表中的排名（0 = 未出现）
    contributions: list # 每个来源的贡献明细


def reciprocal_rank_fusion(
    ranked_lists: Dict[str, List[Tuple[str, float]]],
    k: int = 60,
    weights: Dict[str, float] = None,
) -> List[FusedResult]:
    """RRF 融合多个排序结果。

    Args:
        ranked_lists: {"vector": [(block_id, score), ...], "fts": [(block_id, score), ...]}
        k: RRF 常数（默认 60，与 QMD 一致）
            [已裁决见 ADR] Phase 9 硬编码 k=60（DEFAULT_K=60）。
            通过 config.yaml `retrieval.rrf_k: 60` 可选覆盖。
            不在 API 请求参数中暴露。调优留到 Phase 9b golden set 回归后评估。
            裁决依据：phase9-cross-doc-adjudication.md 不一致 1。
        weights: 各来源的权重（默认均为 1.0；原始查询可设 ×2）

    Returns:
        按 rrf_score 降序排列的 FusedResult 列表

    公式: score(doc) = Σ_i  weight_i / (k + rank_i(doc))
    其中 rank 从 1 开始计数，未出现的文档不贡献。
    """
    if weights is None:
        weights = {src: 1.0 for src in ranked_lists}

    # 为每个来源构建 rank 映射
    rank_maps: Dict[str, Dict[str, int]] = {}
    for source, ranked in ranked_lists.items():
        rank_maps[source] = {block_id: rank + 1 for rank, (block_id, _) in enumerate(ranked)}

    # 收集所有 block_id
    all_ids = set()
    for ranked in ranked_lists.values():
        all_ids.update(bid for bid, _ in ranked)

    # 计算 RRF 分数
    results: List[FusedResult] = []
    for block_id in all_ids:
        total_score = 0.0
        contributions = []
        vec_rank = 0
        fts_rank = 0

        for source, rank_map in rank_maps.items():
            rank = rank_map.get(block_id)
            if rank is not None:
                weight = weights.get(source, 1.0)
                contribution = weight / (k + rank)
                total_score += contribution
                contributions.append({
                    "source": source,
                    "rank": rank,
                    "weight": weight,
                    "contribution": contribution,
                })
                if source == "vector":
                    vec_rank = rank
                elif source == "fts":
                    fts_rank = rank

        results.append(FusedResult(
            block_id=block_id,
            rrf_score=total_score,
            vector_rank=vec_rank,
            fts_rank=fts_rank,
            contributions=contributions,
        ))

    results.sort(key=lambda r: r.rrf_score, reverse=True)
    return results
```

**在 retriever 中集成**：

```python
# retriever.py — Phase 9 增强
class EKRSRetriever:
    def __init__(self, qdrant: QdrantManager, fts: FTSManager = None):
        self._qdrant = qdrant
        self._fts = fts  # None = 纯向量模式（向后兼容）

    def retrieve(self, query: str, top_k: int = 40, ...):
        # 并行检索：向量 + BM25
        vector_hits = self._qdrant.search(query_text=query, top_k=top_k)
        vector_results = [(p.get("block_id", str(i)), s) for i, (p, s) in enumerate(vector_hits)]

        fts_results = []
        if self._fts:
            fts_results = self._fts.search(query, limit=top_k)

        # RRF 融合
        if fts_results:
            fused = reciprocal_rank_fusion({
                "vector": vector_results,
                "fts": fts_results,
            }, k=60, weights={"vector": 1.0, "fts": 1.0})

            # 用融合后的排序重建 chunks
            block_id_order = [f.block_id for f in fused[:top_k]]
            chunks = self._reorder_chunks_by_block_ids(vector_hits, block_id_order)
            rrf_scores = [f.rrf_score for f in fused[:top_k]]
        else:
            # 退化模式：纯向量
            chunks = self._build_chunks(vector_hits)
            rrf_scores = [s for _, s in vector_results]

        # scope 复合评分（R4: 保留 scope 优先级）
        final_scores = [rrf * (1 + scope) for rrf, scope in zip(rrf_scores, scope_scores)]
        # ...
```

#### d) 铁律兼容性检查

| 铁律 | 检查 | 结论 |
|------|------|------|
| R1 | RRF 结果映射到 Chunk，hint 提取不变 | ✅ |
| R2 | RRF 是纯数学运算，在检索层 | ✅ |
| R3 | RRF 融合 Gate 1 的两条召回路径 | ✅ |
| R4 | scope 复合评分在 RRF 之后应用 | ✅ |
| R5 | RRF 不涉及图查询 | ✅ |
| R6 | RRF 是确定性运算 | ✅ |
| R7 | Chunk.scope_path 保留 | ✅ |
| R8 | RRF 不改变索引层过滤 | ✅ |

#### e) 工作量评估

| 任务 | 描述 | 工作量 |
|------|------|--------|
| T-RRF-1 | 实现 reciprocal_rank_fusion 函数 | 小 |
| T-RRF-2 | 在 retriever 中集成并行检索 + RRF 融合 | 中 |
| T-RRF-3 | block_id 映射层（FTS 结果 → Qdrant payload） | 中 |
| T-RRF-4 | scope 复合评分与 RRF 的组合公式 | 小 |
| T-RRF-5 | 单元测试 + golden set 回归 | 中 |

---

### 4.3 Cross-encoder 重排序（优先级 3 — 可选，门控于延迟）

#### a) QMD 中的实现

QMD 使用 `qwen3-reranker-0.6b-q8_0` GGUF 模型（~640MB），通过 `node-llama-cpp` 执行 cross-encoder logprob 评分。

重排流程（`hybrid-search.ts`）：
1. RRF 融合后保留 top-40 候选
2. 对每个候选提取 best chunk（term overlap 最高的分块）
3. cross-encoder 对 (query, best_chunk) 对评分
4. 位置感知混合：top 1-3 = 75% RRF / 25% reranker；top 4-10 = 60/40；top 11+ = 40/60

#### b) EKRS 当前对应能力

EKRS **没有** 重排序器。当前的 `_rank_by_scope` 是结构化启发式，不是学习型相关性信号。

#### c) 移植方案设计

**新增模块**：`rag/ekrs_rag/retrieval/reranker.py`

```python
"""Cross-encoder 重排序器 — Phase 9c 可选组件。

使用 sentence-transformers 的 cross-encoder 模型对
(query, chunk) 对评分，提升 top-k 精度。

重要：此组件引入非确定性（模型推理），仅在非 strict 模式下使用，
且必须支持 skip（退化到纯 RRF 分数）。
"""
from typing import List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """Cross-encoder 重排序器。

    模型选择：
    - 默认：cross-encoder/ms-marco-MiniLM-L-6-v2（~80MB，英文）
    - CJK 优化：BAAI/bge-reranker-base（~280MB，中英文）
    - 本地 GGUF：通过 llama-cpp-python 加载 qwen3-reranker（与 QMD 一致）
    """

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self._model = None
        self._model_name = model_name

    def _ensure_loaded(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(self._model_name)
            except ImportError:
                logger.warning("sentence-transformers not installed, reranker disabled")
                raise

    def rerank(
        self,
        query: str,
        chunks: List[str],  # chunk text 列表
        top_k: int = 40,
    ) -> List[Tuple[int, float]]:
        """对 chunks 按与 query 的相关性重排。

        Returns: [(original_index, rerank_score), ...] 按 score 降序
        """
        self._ensure_loaded()
        pairs = [(query, chunk) for chunk in chunks]
        scores = self._model.predict(pairs)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    @staticmethod
    def position_aware_blend(
        rrf_rank: int,
        rrf_score: float,
        rerank_score: float,
    ) -> float:
        """位置感知混合（移植自 QMD hybrid-search.ts:200-204）。

        top 1-3: 信任 RRF（精确匹配优先）
        top 4-10: 平衡
        top 11+: 信任 reranker（语义相关性）
        """
        if rrf_rank <= 3:
            weight = 0.75
        elif rrf_rank <= 10:
            weight = 0.60
        else:
            weight = 0.40
        return weight * rrf_score + (1 - weight) * rerank_score
```

#### d) 铁律兼容性检查

| 铁律 | 检查 | 结论 |
|------|------|------|
| R1 | 重排不改变 chunk 的 source_span/block_id | ✅ |
| R2 | **⚠️ 重排引入非确定性。必须在 strict=true 时禁用（R6 联动）** | ⚠️ 需隔离 |
| R3 | 重排在 Gate 1.5（Gate 1 和 Gate 2 之间），不影响 Gate 2/3 | ✅ |
| R4 | 重排后的 scope 复合评分仍保留 | ✅ |
| R5 | 不涉及图查询 | ✅ |
| R6 | **strict=true 时必须跳过重排**，退化到纯 RRF | ⚠️ 需门控 |
| R7 | Chunk.scope_path 保留 | ✅ |
| R8 | 重排不改变索引层过滤 | ✅ |

**关键约束**：重排器必须在 `strict=True` 时自动禁用，回退到确定性 RRF 分数。这是与 R2/R6 的兼容性保证。

#### e) 工作量评估

| 任务 | 描述 | 工作量 |
|------|------|--------|
| T-RR-1 | 实现 CrossEncoderReranker 类 | 中 |
| T-RR-2 | 位置感知混合函数 | 小 |
| T-RR-3 | strict 模式门控（strict=true 时跳过） | 小 |
| T-RR-4 | 模型加载 + 缓存策略 | 中 |
| T-RR-5 | 延迟基准测试（p99 < 5s 预算） | 中 |
| T-RR-6 | golden set A/B 对比 | 中 |

---

### 4.4 强信号短路机制（优先级 4）

#### a) QMD 中的实现

QMD 的强信号短路在 `hybrid-search.ts:32-33`（常量定义）：

```typescript
export const STRONG_SIGNAL_MIN_SCORE = 0.85;
export const STRONG_SIGNAL_MIN_GAP = 0.15;
```

逻辑：BM25 探针先执行，如果 top score ≥ 0.85 且 top1-top2 gap ≥ 0.15，则跳过查询扩展（省 1-2 次 LLM 调用）。

#### b) EKRS 当前对应能力

EKRS 没有短路机制。所有查询都走完整的向量搜索路径。

#### c) 移植方案设计

```python
# rrf_fusion.py — 强信号检测

STRONG_SIGNAL_MIN_SCORE = 0.85
STRONG_SIGNAL_MIN_GAP = 0.15

def detect_strong_signal(fts_results: List[Tuple[str, float]]) -> bool:
    """检测 BM25 是否有明确赢家。

    如果 top score ≥ 0.85 且 gap ≥ 0.15，
    说明精确匹配已经找到，可以跳过后续增强（重排等）。
    """
    if len(fts_results) < 1:
        return False
    top_score = fts_results[0][1]
    if top_score < STRONG_SIGNAL_MIN_SCORE:
        return False
    if len(fts_results) >= 2:
        gap = top_score - fts_results[1][1]
        if gap < STRONG_SIGNAL_MIN_GAP:
            return False
    return True
```

**注意**：前序研究（deep-dive §1.3.1）明确指出——QMD 的短路会跳过查询扩展，但 EKRS **不允许短路三闸门流水线**。EKRS 的强信号短路仅用于跳过**可选的重排步骤**（省延迟），不跳过任何必需的 Gate。

#### d) 铁律兼容性

- R3 ✅：三闸门流水线完整执行，不短路
- R6 ✅：strict 模式下本来就不重排，短路逻辑不影响

#### e) 工作量：1 个任务，小。

---

### 4.5 边界感知分块优化（优先级 5）

#### a) QMD 中的实现

QMD 的分块在 `src/chunking.ts`，break-point 评分系统（`chunking.ts:53-66`）：

```typescript
export const BREAK_PATTERNS: [RegExp, number, string][] = [
  [/(?:^|\n)#{1}(?!#)/gm, 100, 'h1'],     // 标题权重最高
  [/(?:^|\n)#{2}(?!#)/gm, 90, 'h2'],
  [/(?:^|\n)#{3}(?!#)/gm, 80, 'h3'],
  [/(?:^|\n)#{4}(?!#)/gm, 70, 'h4'],
  [/(?:^|\n)```/gm, 80, 'codeblock'],     // 代码块边界
  [/\n(?:---|\*\*|___)\s*\n/g, 60, 'hr'], // 水平线
  [/\n\n+/g, 20, 'blank'],                 // 段落边界
  [/\n[-*]\s/g, 5, 'list'],               // 列表项
  [/\n/g, 1, 'newline'],
];
```

距离衰减窗口（200 token）：在窗口内，分数按距离衰减。

#### b) EKRS 当前对应能力

EKRS 的分块器在 `rag/ekrs_rag/ingestion/chunker.py`，基于 scope 变化 + token 溢出的三条件分块：
1. Scope 变化（heading_path 不同 → flush）
2. 表格/kv 类型（独立分块）
3. Token 溢出（len/4 估算，超出 max_tokens → flush）

没有 break-point 评分，没有距离衰减窗口。

#### c) 移植方案设计

在 EKRS 的 `chunker.py` 中**增加**边界评分（不替换现有 scope-aware 逻辑）：

```python
# chunker.py — Phase 9 增强

BREAK_PATTERNS = [
    # (正则, 分数, 类型) — 移植自 QMD chunking.ts:53-66
    (re.compile(r'(?:^|\n)#{1}(?!#)', re.MULTILINE), 100, 'h1'),
    (re.compile(r'(?:^|\n)#{2}(?!#)', re.MULTILINE), 90, 'h2'),
    (re.compile(r'(?:^|\n)#{3}(?!#)', re.MULTILINE), 80, 'h3'),
    (re.compile(r'(?:^|\n)```', re.MULTILINE), 80, 'codeblock'),
    (re.compile(r'\n(?:---|\*\*\*|___)\s*\n'), 60, 'hr'),
    (re.compile(r'\n\n+'), 20, 'blank'),
    (re.compile(r'\n[-*]\s'), 5, 'list'),
]

WINDOW_CHARS = 800  # ~200 tokens

def find_best_break_point(text: str, window_start: int, window_end: int) -> int:
    """在窗口内寻找最佳断点（移植自 QMD scanBreakPoints）。"""
    window = text[window_start:window_end]
    best_pos = window_end  # 默认：窗口末尾
    best_score = 0

    for pattern, base_score, _ in BREAK_PATTERNS:
        for match in pattern.finditer(window):
            pos = window_start + match.start()
            # 距离衰减：离窗口中心越远，分数越低
            distance = abs(pos - (window_start + window_end) // 2)
            decay = 1 - (distance / (WINDOW_CHARS // 2)) ** 2 * 0.7
            final_score = base_score * max(decay, 0.3)
            if final_score > best_score:
                best_score = final_score
                best_pos = pos

    return best_pos
```

**集成方式**：在 `chunk_blocks()` 的 token 溢出条件中，不直接在溢出点切分，而是在溢出点前后 200 token 的窗口内寻找最佳 break-point。

#### d) 铁律兼容性

- R1 ✅：break-point 优化不影响 source_span/block_id 的记录（它们跟随实际切分位置）
- 其他铁律不涉及分块逻辑

#### e) 工作量：2 个任务（实现 + 测试），小。

---

### 4.6 MCP 工具适配层（优先级 6）

#### a) QMD 中的实现

QMD 的 MCP Server 在 `src/mcp/server.ts`，提供 15 个工具，通过 Streamable HTTP + stdio 传输。

三组工具：
- **检索**：query, get, multi_get, status
- **文档精读**：doc_toc, doc_read, doc_grep, doc_query, doc_elements, doc_links
- **知识摄取**：wiki_ingest, doc_write, wiki_lint, wiki_log, wiki_index

#### b) EKRS 当前对应能力

EKRS 仅有 HTTP API（`/v1/constraints`, `/v1/ingestion/notify` 等），无 MCP 集成。

#### c) 移植方案设计

**新增模块**：`rag/ekrs_rag/mcp/server.py`

```python
"""EKRS MCP Server — 暴露 EKRS 能力给 AI Agent。

使用 Python mcp 包实现 stdio + Streamable HTTP 传输。
工具映射：
- ekrs_query → POST /v1/constraints（约束求解）
- ekrs_search → 新增：广谱检索（BM25+向量+RRF）
- ekrs_get_block → 新增：GET /v1/blocks/{block_id}（文档精读）
- ekrs_status → GET /healthz
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server("ekrs")

@server.list_tools()
async def list_tools():
    return [
        Tool(name="ekrs_query", description="查询工程约束...", ...),
        Tool(name="ekrs_search", description="广谱文档检索...", ...),
        Tool(name="ekrs_get_block", description="读取文档块内容...", ...),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "ekrs_query":
        # 调用内部 constraints API
        ...
    elif name == "ekrs_search":
        # 调用广谱检索
        ...
```

#### d) 铁律兼容性

MCP 适配层是 API 层的包装，不触及求解器或检索逻辑。所有铁律 ✅。

#### e) 工作量：3-4 个任务，中。

---

### 4.7 查询扩展（优先级 7 — 延迟评估）

#### 结论：暂不移植

前序研究（feature-mapping §6, deep-dive §1.3.3）已详细分析：
- 工程查询通常是精确的（"设计压力 1.6MPa 的容器壁厚"），不是探索性的
- bge-m3 已处理语义相似性
- 查询扩展需要 1.7B 参数的 LLM 模型（~1.1GB），引入非确定性和延迟
- 如果 FTS5 + RRF + 重排后 recall 仍不足，再评估

**决策**：Phase 9 不移植查询扩展。如果 golden set 显示 recall@10 不足，Phase 10 通过 MCP `structuredSearch` 路径让调用方（Agent）提供扩展变体——EKRS 不需要自己的扩展模型。

---

## 5. 广谱检索流水线设计

### 5.1 新流水线全图

```
用户查询 (自然语言)
    │
    ▼
┌───────────────────────────────────────────────────┐
│  Gate 1: 召回 (Recall)                             │
│                                                    │
│  ┌─────────────────┐    ┌──────────────────┐      │
│  │  Qdrant 向量搜索  │    │  FTS5 BM25 搜索   │      │
│  │  (bge-m3 1024d)  │    │  (porter+unicode) │      │
│  │  top_k=40        │    │  top_k=40         │      │
│  └────────┬────────┘    └────────┬─────────┘      │
│           │                       │                 │
│           └───────────┬───────────┘                 │
│                       ▼                             │
│              ┌────────────────┐                     │
│              │  RRF 融合 (k=60) │                     │
│              │  + scope 过滤    │                     │
│              └───────┬────────┘                     │
│                      │                              │
│     ┌────────────────┼────────────────┐             │
│     │ 强信号短路?      │                │             │
│     │ (BM25 top≥0.85  │                │             │
│     │  gap≥0.15)      │                │             │
│     ▼                ▼                │             │
│  [跳过重排]    ┌──────────────┐       │             │
│               │ Gate 1.5:     │       │             │
│               │ Cross-encoder │       │             │
│               │ 重排 (可选)    │       │             │
│               │ (strict时跳过) │       │             │
│               └──────┬───────┘       │             │
│                      │                │             │
│                      ▼                ▼             │
│              ┌──────────────────────────┐          │
│              │  top-k chunks             │          │
│              │  (RRF 或 blended score)   │          │
│              └──────────┬───────────────┘          │
└─────────────────────────┼──────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────┐
│  Gate 2: 提取 (Extraction)                         │
│  extract_hints(chunks) → List[NumericHint]         │
│  (R1: 每个 hint 携带 source_span/block_id)          │
└─────────────────────────┬──────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────┐
│  Gate 3: 求解 (Solve)                              │
│  solver.solve(hints, context) → ConstraintResult   │
│  (R2: 纯函数，确定性)                                │
└───────────────────────────────────────────────────┘
```

### 5.2 与三闸门流水线的集成

广谱检索**不改变**三闸门流水线的结构，只增强 Gate 1：

| 闸门 | Phase 8（现状） | Phase 9（增强后） |
|------|----------------|-------------------|
| Gate 1 召回 | Qdrant 向量搜索 | Qdrant 向量 + FTS5 BM25 并行 → RRF 融合 |
| Gate 1.5 重排 | 无 | Cross-encoder 重排（可选，strict 时跳过） |
| Gate 2 提取 | extract_hints | 不变 |
| Gate 3 求解 | solver.solve | 不变 |

### 5.3 确定性边界

| 操作 | 确定性？ | 理由 |
|------|---------|------|
| Qdrant 向量搜索 | ✅ 确定 | HNSW + 固定种子 |
| FTS5 BM25 搜索 | ✅ 确定 | BM25 是确定性算法 |
| RRF 融合 | ✅ 确定 | 纯数学运算 |
| scope 复合评分 | ✅ 确定 | 固定公式 |
| extract_hints | ✅ 确定 | 正则匹配 |
| solver.solve | ✅ 确定 | portion 区间运算（R2） |
| Cross-encoder 重排 | ❌ 非确定 | 模型推理有微小浮点差异 |
| 查询扩展 | ❌ 非确定 | LLM 生成（不移植） |

**确定性保证**：
- `strict=True`：跳过 Gate 1.5 重排，整个流水线 100% 确定
- `strict=False`：重排是可选的（`skip_rerank=True` 可禁用），且重排结果缓存在审计日志中支持回放

### 5.4 延迟预算分析

EKRS 的延迟预算为 5s（Phase 8 T8-5 chunker baseline p99=279µs/doc）。

| 步骤 | 预估延迟 | 预算占比 |
|------|---------|---------|
| Qdrant 向量搜索 | ~50-100ms | 2% |
| FTS5 BM25 搜索 | ~5-20ms | <1% |
| RRF 融合 | <1ms | ~0% |
| scope 过滤 | <1ms | ~0% |
| Gate 2 hint 提取 | ~5-10ms | <1% |
| Gate 3 求解器 | ~1-5ms | <1% |
| **小计（无重排）** | **~60-140ms** | **~3%** |
| Cross-encoder 重排 (40 对) | ~2-8s | 40-160% ⚠️ |
| **小计（含重排）** | **~2-8s** | **40-160%** |

**结论**：
- 不含重排的广谱检索完全在延迟预算内（< 200ms）
- Cross-encoder 重排可能超出预算。缓解措施：
  1. 仅对 top-10（而非 top-40）重排
  2. 强信号短路时跳过重排
  3. strict 模式禁用重排
  4. 异步重排（返回初步结果，后台更新）

---

## 6. 架构设计

### 6.1 移植后系统架构图

```
                              ┌──────────────────────────────────────────────────┐
                              │              EKRS RAG Service                     │
                              │            (FastAPI :8000)                        │
                              │                                                   │
   ┌──────────┐              │  ┌─────────────────────────────────────────────┐  │
   │ External │  POST        │  │             API Routes                       │  │
   │ Parser   │  /v1/        │  │  /v1/ingestion/notify  /v1/constraints      │  │
   │          │  ingestion/  │  │  /v1/blocks/{id} (新)  /healthz /metrics    │  │
   └────┬─────┘  notify      │  └───────────┬────────────────┬────────────────┘  │
        │                    │              │                │                   │
        │ JSONL              │  ┌───────────▼───────┐ ┌──────▼──────────────┐    │
        │                    │  │  Ingestion         │ │  Retriever (增强)    │    │
        │                    │  │  Pipeline          │ │                     │    │
        │                    │  │  ┌──────────────┐  │ │  ┌───────────────┐  │    │
        │                    │  │  │ Chunker      │  │ │  │ Qdrant 向量    │  │    │
        │                    │  │  │ (边界评分增强) │  │ │  │ 搜索          │  │    │
        │                    │  │  └──────┬───────┘  │ │  └───────┬───────┘  │    │
        │                    │  │         │          │ │          │          │    │
        │                    │  │  ┌──────▼───────┐  │ │  ┌───────▼───────┐  │    │
        │                    │  │  │ Embedding    │  │ │  │ FTS5 BM25     │  │    │
        │                    │  │  │ Service      │  │ │  │ 搜索 (新增)    │  │    │
        │                    │  │  │ (bge-m3)     │  │ │  └───────┬───────┘  │    │
        │                    │  │  └──────┬───────┘  │ │          │          │    │
        │                    │  │         │          │ │  ┌───────▼───────┐  │    │
        │                    │  │  ┌──────▼───────┐  │ │  │ RRF 融合      │  │    │
        │                    │  │  │ Qdrant       │  │ │  │ (新增)        │  │    │
        │                    │  │  │ upsert       │  │ │  └───────┬───────┘  │    │
        │                    │  │  └──────┬───────┘  │ │          │          │    │
        │                    │  │         │          │ │  ┌───────▼───────┐  │    │
        │                    │  │  ┌──────▼───────┐  │ │  │ Cross-encoder │  │    │
        │                    │  │  │ FTS Sync     │  │ │  │ 重排 (可选)    │  │    │
        │                    │  │  │ (新增)        │  │ │  └───────┬───────┘  │    │
        │                    │  │  └──────────────┘  │ └──────────┼──────────┘    │
        │                    │  └───────────────────┘            │               │
        │                    │                          ┌────────▼────────┐       │
        │                    │                          │ Constraint      │       │
        │                    │                          │ Engine (R2)     │       │
        │                    │                          │ solver (不变)   │       │
        │                    │                          └─────────────────┘       │
        │                    │                                                    │
        │                    │  ┌────────────┐  ┌──────────┐  ┌──────────────┐  │
        │                    │  │ AuditWriter│  │ Redis    │  │ SQLite       │  │
        │                    │  │ (16+事件)  │  │ Locks    │  │ task_repo    │  │
        │                    │  └────────────┘  └──────────┘  └──────────────┘  │
        │                    │                                                    │
        │                    │  ┌────────────────────────────────────────────┐   │
        │                    │  │  MCP Server (新增, Phase 9c)                │   │
        │                    │  │  ekrs_query / ekrs_search / ekrs_get_block │   │
        │                    │  └────────────────────────────────────────────┘   │
        └────────────────────┘────────────────────────────────────────────────────┘
                                          │
                              ┌───────────┴───────────────┐
                              │                           │
                       ┌──────▼──────┐           ┌───────▼───────┐
                       │ Qdrant      │           │ SQLite FTS5   │
                       │ :6333       │           │ ekrs_fts.db   │
                       │ (向量存储)   │           │ (全文索引, 新) │
                       └─────────────┘           └───────────────┘
```

### 6.2 数据流设计

#### 摄取流（增强后）

```
Parser 输出 JSONL
  ↓
POST /v1/ingestion/notify
  ↓ (X-Parser-Token 验证)
  ↓ idempotency check → replay short-circuit
  ↓ RedisLock on doc_hash
  ↓
读取 JSONL → 解析 DocumentBlockIR
  ↓
Chunker (边界评分增强)
  → scope 变化 → flush
  → 表格/kv → 独立分块
  → token 溢出 → 窗口内找最佳 break-point
  ↓
EmbeddingService.encode (bge-m3 dense+sparse)
  ↓
  ├─→ Qdrant upsert (向量写入)
  ├─→ FTS5 upsert (全文索引写入) ← Phase 9 新增
  └─→ AuditWriter.write("ingestion_completed", {...})
  ↓
callback /status
```

#### 查询流（增强后）

```
User POST /v1/constraints
  {query, context, strict, top_k}
  ↓
Gate 0: context merge
  ↓
Gate 1: Recall (并行)
  ├─→ Qdrant.search(query, top_k=40)     → 向量结果
  └─→ FTSManager.search(query, top_k=40) → BM25 结果
  ↓
RRF 融合 (k=60, 权重可配)
  ↓
scope 过滤 (active_scope 前缀匹配)
  ↓
强信号短路检测?
  ├─ 是 → 跳过 Gate 1.5
  └─ 否 → Gate 1.5 (可选)
         ↓
         strict == true?
         ├─ 是 → 跳过重排
         └─ 否 → CrossEncoder.rerank(query, top_chunks)
                 → position_aware_blend(rrf, rerank)
  ↓
Gate 2: extract_hints(top_chunks)
  ↓
Gate 3: solver.solve(hints, context)
  ↓
Multi-branch JSON response
```

### 6.3 新增模块一览

| 模块 | 路径 | 职责 | 依赖 |
|------|------|------|------|
| FTSManager | `rag/ekrs_rag/retrieval/fts_manager.py` | FTS5 全文索引管理 | sqlite3 (stdlib) |
| RRFFusion | `rag/ekrs_rag/retrieval/rrf_fusion.py` | RRF 融合 + 强信号检测 | 无 (纯函数) |
| CrossEncoderReranker | `rag/ekrs_rag/retrieval/reranker.py` | Cross-encoder 重排序 | sentence-transformers (可选) |
| MCP Server | `rag/ekrs_rag/mcp/server.py` | MCP 工具适配 | mcp (pip) |
| Block Reader | `rag/ekrs_rag/api/routes/blocks.py` | GET /v1/blocks/{id} | 无 |

**现有模块修改**：

| 模块 | 修改内容 |
|------|---------|
| `retriever.py` | 增加 FTS 并行检索 + RRF 融合 + 可选重排 |
| `chunker.py` | 增加 break-point 边界评分 |
| `pipeline.py` | 增加 FTS 同步写入 |
| `qdrant_client.py` | 增加 block_id 到 payload 的映射 |
| `constraints.py` | 无修改（retriever 接口不变） |

---

## 7. 风险评估

### 7.1 技术风险

| 风险 | 严重度 | 影响 | 缓解方案 |
|------|--------|------|---------|
| FTS5 索引与 Qdrant 不同步 | 高 | BM25 结果指向已删除的 chunk | 通过 AuditWriter 原子写入；定期一致性校验 |
| Cross-encoder 延迟超标 | 中 | 查询 p99 超出 5s 预算 | 仅 top-10 重排；强信号短路；strict 禁用 |
| 模型加载内存 | 中 | reranker ~280MB + bge-m3 ~2.1GB | reranker 懒加载；可选关闭 |
| FTS5 CJK 分词 | 中 | 工程文档含中文，porter 分词器对中文效果差 | 配置 `tokenize='unicode61 remove_diacritics 2'`；考虑 jieba 分词器 |
| block_id 映射断裂 | 中 | FTS 和 Qdrant 使用不同标识符 | 统一使用 block_id 作为主键，双写时保持一致 |

### 7.2 铁律违反风险（逐条审查）

| 铁律 | 风险 | 缓解 |
|------|------|------|
| R1 | FTS 结果可能缺少 source_span | FTSManager 返回 block_id，通过 block_id 回查 Qdrant payload 获取完整 Chunk |
| R2 | **Cross-encoder 引入非确定性** | strict=true 时禁用重排；非 strict 时审计日志记录完整响应支持回放 |
| R3 | 无风险 — 三闸门结构不变 | — |
| R4 | RRF 可能改变 scope 优先级排序 | scope 复合评分在 RRF 之后应用，保持 R4 优先级 |
| R5 | 无风险 — FTS5 不是图数据库 | — |
| R6 | **重排在 strict 模式下违反"禁止推断"** | strict=true 时 `skip_rerank=True` 强制跳过 |
| R7 | 无风险 — scope_path 从 Qdrant payload 继承 | — |
| R8 | FTS WHERE 子句可能过度过滤 | 仅过滤 `status = 'illegal'`，不过滤其他状态 |

### 7.3 运维复杂度变化

| 维度 | Phase 8 | Phase 9 (无重排) | Phase 9 (含重排) |
|------|---------|------------------|-----------------|
| 服务数 | 4 (qdrant, redis, rag, prometheus) | 4 (不变) | 4 (不变) |
| 进程内新增 | — | SQLite FTS5 (文件) | + reranker 模型 |
| 内存增量 | — | ~10MB (FTS5) | ~280MB (reranker) |
| 磁盘增量 | — | ~与 Qdrant payload 等量 | +280MB 模型 |
| 监控新增 | — | fts_search_latency | rerank_latency, rerank_skip_count |

### 7.4 回归风险

- 现有 346+ 测试必须全部通过
- golden set 50 case 不退化
- retriever 的退化模式（FTS=None）必须与 Phase 8 行为完全一致
- 新增至少 3 个 golden case：精确标识符查询（`A312-TP316`、`GB/T 12459`、`1.6MPa`）

---

## 8. 分阶段实施计划

### Phase 9a — FTS5 + RRF 核心（2-3 周）

**目标**：实现 BM25 + 向量并行检索 + RRF 融合，验证广谱检索价值。

| 任务 | 描述 | 验收标准 |
|------|------|---------|
| T9a-1 | 实现 FTSManager（建表/CRUD/搜索/BM25归一化） | 单元测试通过 |
| T9a-2 | 摄取流水线集成 FTS 同步 | 摄取后 FTS 行数 = Qdrant 点数 |
| T9a-3 | 实现 reciprocal_rank_fusion | 单元测试 + 边界用例 |
| T9a-4 | retriever 集成并行检索 + RRF | 退化模式与 Phase 8 一致 |
| T9a-5 | block_id 映射层 | FTS↔Qdrant 双向映射正确 |
| T9a-6 | golden set 回归 + 工程标识符用例 | 50 case 全通过 + ≥3 新 case |
| T9a-7 | 审计事件扩展（fts_synced/fts_searched） | 审计日志可回放 |

### Phase 9b — 边界感知分块 + 强信号短路（1-2 周）

**目标**：改善分块质量 + 优化查询延迟。

| 任务 | 描述 | 验收标准 |
|------|------|---------|
| T9b-1 | break-point 评分系统 | 单元测试 + 分块覆盖率 |
| T9b-2 | chunker 集成边界评分 | 代码块不被切分 |
| T9b-3 | 强信号短路检测 | 精确匹配查询跳过重排 |
| T9b-4 | golden set 回归 | 50 case 不退化 |

### Phase 9c — Cross-encoder 重排（2-3 周，可选）

**目标**：提升 top-k 精度，门控于 Phase 9a 的 recall 数据。

| 任务 | 描述 | 验收标准 |
|------|------|---------|
| T9c-1 | 实现 CrossEncoderReranker | 模型加载 + predict 正确 |
| T9c-2 | 位置感知混合函数 | 与 QMD 75/60/40 一致 |
| T9c-3 | strict 模式门控 | strict=true 跳过重排 |
| T9c-4 | 延迟基准测试 | p99 < 5s（不含重排）/ 接受重排延迟 |
| T9c-5 | golden set A/B 对比 | 重排后 recall@10 提升 |

### Phase 9d — MCP 适配层（2-3 周）

**目标**：让 AI Agent 通过 MCP 原生调用 EKRS。

| 任务 | 描述 | 验收标准 |
|------|------|---------|
| T9d-1 | 实现 GET /v1/blocks/{block_id} | 返回 block 完整内容 |
| T9d-2 | 实现 MCP Server (stdio + HTTP) | mcp inspector 验证 |
| T9d-3 | 注册 4 个工具（query/search/get_block/status） | 工具发现正常 |
| T9d-4 | Claude Code 集成验证 | Agent 能调用 EKRS |

### 依赖关系图

```
Phase 9a (FTS+RRF)
    │
    ├──▶ Phase 9b (分块+短路)  ← 可并行
    │
    ├──▶ Phase 9c (重排)       ← 依赖 9a 的 recall 数据
    │
    └──▶ Phase 9d (MCP)        ← 可并行（仅需 9a 的 search 能力）
```

**总预估**：
- Phase 9a + 9b = 3-5 周（核心广谱检索）
- + Phase 9c = +2-3 周（精度增强）
- + Phase 9d = +2-3 周（Agent 集成）
- 完整 Phase 9 = 7-11 周

---

## 9. 结论与建议

### 9.1 核心结论

1. **移植是可行的且高价值的**。mineru-explorer 的 BM25 + RRF + 边界分块等设计模式可以用纯 Python 重写到 EKRS 内部，无需引入外部常驻服务（与外部集成方案不同）。

2. **确定性是可保证的**。BM25/向量/RRF 都是确定性操作，与 R2 完全兼容。唯一引入非确定性的 cross-encoder 重排通过 strict 模式门控隔离。

3. **存储引擎暂不替换**。Phase 9 保持 Qdrant + 新增 SQLite FTS5。Zvec 是 Phase 10+ 的有力候选（原生 FTS + 嵌入式运行），但存储替换是独立工程，不应与检索增强混合。

4. **广谱检索不改变求解器**。所有增强都在 Gate 1 召回层，Gate 2/3 完全不变。retriever 的退化模式（FTS=None）与 Phase 8 行为完全一致。

### 9.2 三种路径的对比

| 维度 | 外部集成（前序报告） | 本报告（内部移植） | 混合路径 |
|------|---------------------|-------------------|---------|
| 外部依赖 | 需要 mineru-explorer daemon | 无 | 部分 |
| 工作量 | 8-11 周 | 7-11 周 | 8-12 周 |
| 确定性控制 | 需隔离 QMD 的 LLM | 完全可控 | 可控 |
| 演进独立性 | 依赖 mineru-explorer 版本 | 完全自主 | 部分 |
| 代码复用 | 直接用成熟代码 | 需重写算法 | 混合 |
| 存储 | 双索引（Qdrant + SQLite） | Qdrant + FTS5（同库） | 取决于方案 |

### 9.3 最终建议

**推荐启动 Phase 9a（FTS5 + RRF 核心）**，以最小成本验证广谱检索价值。

Phase 9a 只需 2-3 周，完成后即可测量：
- 精确标识符查询（`A312-TP316`）的 recall 是否显著提升
- 自然语言查询的召回宽度是否改善
- 延迟是否在可接受范围内

如果 Phase 9a 验证成功，按依赖图推进 9b（分块优化）、9c（重排）、9d（MCP）。

Phase 10 单独评估 Zvec 替换 Qdrant 的可行性——如果 Zvec 的原生 FTS + 混合搜索能消除独立 Qdrant 服务，运维成本可进一步降低。

---

## 附录 A：源码级交叉引用表

| QMD 源码 | 函数/常量 | EKRS 移植位置 | 移植类型 |
|----------|----------|-------------|---------|
| `search.ts:164-196` | `searchFTS()` | `fts_manager.py::FTSManager.search()` | 重写 (TS→Python) |
| `search.ts:99-144` | `buildFTS5Query()` | `fts_manager.py::_build_fts5_query()` | 重写 |
| `search.ts:187-188` | BM25 归一化 | `fts_manager.py::search()` 内联 | 直接移植 |
| `search.ts` | `reciprocalRankFusion()` | `rrf_fusion.py::reciprocal_rank_fusion()` | 重写 |
| `hybrid-search.ts:32-33` | `STRONG_SIGNAL_MIN_SCORE/GAP` | `rrf_fusion.py` 常量 | 直接移植 |
| `hybrid-search.ts:200-204` | 位置感知混合 | `reranker.py::position_aware_blend()` | 重写 |
| `chunking.ts:53-66` | `BREAK_PATTERNS` | `chunker.py` 常量 | 适配（Markdown→IR） |
| `chunking.ts:73+` | `scanBreakPoints()` | `chunker.py::find_best_break_point()` | 重写 |
| `mcp/server.ts` | MCP 工具注册 | `mcp/server.py` | 新建（Python mcp 包） |

---

## 附录 B：存储引擎评估详细数据

### B.1 Qdrant 现状

- 版本：Qdrant 1.11
- 集合：单个 collection，bge-m3 1024d dense + sparse
- 部署：docker-compose 独立容器 (:6333)
- 索引：HNSW
- 压缩：无（float32）
- 持久化：Qdrant 磁盘快照
- 重试：tenacity（3 次，指数退避）
- 审计：`_emit_qdrant_failure()` 速率限制（5s/key）

### B.2 Zvec 详细评估

- 版本：v0.6.0 (2026-07-20)
- 语言：C++ 核心，Python 绑定（`pip install zvec`）
- 存储：RocksDB + WAL
- FTS：UAX#29 分词器 + Snowball 词干提取（34 语言）
- 向量索引：Flat / HNSW / HNSW-RaBitQ / DiskANN / sparse
- 量化：INT8/INT4 + 随机旋转
- 混合搜索：单次 `collection.query()` 融合 FTS + 向量 + 标量
- 内置 reranker：`multi_vector_reranker.py`（RRF / Weighted）
- 并发：多进程并发读，单进程写
- Python API 位置：`zvec-clone/python/zvec/`
- 测试参考：`zvec-clone/python/tests/test_collection_fts_vector_hybrid.py`

### B.3 Turbovec 详细评估

- 算法：TurboQuant（ICLR 2026）
- 语言：Rust 核心，Python 绑定（maturin）
- 压缩：2-bit (16x) / 4-bit (8x)
- 搜索：SIMD（NEON / AVX-512BW / AVX2 fallback）
- 过滤：Allowlist 在 SIMD 内核内过滤
- 在线摄入：无训练步骤，添加即索引
- 持久化：`.tv` / `.tvim` 文件
- 框架集成：LangChain / LlamaIndex / Haystack / Agno
- **无 FTS** — 纯向量索引
- 论文：https://arxiv.org/abs/2504.19874

### B.4 替换可行性评分

| 维度 (1-5) | Qdrant (现状) | Zvec | Turbovec | Turbovec+FTS5 |
|-----------|--------------|------|----------|---------------|
| FTS 能力 | 1 | 5 | 0 | 4 |
| 向量性能 | 4 | 4 | 5 | 5 |
| 压缩比 | 1 | 3 | 5 | 5 |
| 嵌入式便利 | 1 | 5 | 5 | 4 |
| CJK 支持 | N/A | 5 | N/A | 3 |
| 持久化 | 5 | 5 | 3 | 4 |
| 成熟度 | 5 | 4 | 3 | 3 |
| 替换成本 | — | 2 | 2 | 1 |
| **总分** | **17** | **31** | **23** | **24** |

**评分说明**：Zvec 总分最高（31），主要得益于原生 FTS + 嵌入式运行 + CJK 支持。但替换成本高（需要重写 QdrantManager、适配 sparse 向量缺失、迁移现有数据）。Phase 9 不推荐替换，Phase 10+ 评估。
