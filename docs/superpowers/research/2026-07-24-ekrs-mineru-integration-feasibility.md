# MinerU-Document-Explorer × EKRS 集成可行性研究报告

> **研究文档 — 仅研究，不含实现代码。**
> 日期：2026-07-24
> 前序研究：
> - [`2026-07-24-mineru-explorer-feature-mapping.md`](2026-07-24-mineru-explorer-feature-mapping.md)（功能映射）
> - [`2026-07-24-mineru-deep-dive-extensions.md`](2026-07-24-mineru-deep-dive-extensions.md)（深度分析）
> 本报告定位：前两份文档聚焦于**将 QMD 的设计模式移植到 EKRS 内部**（如自建 FTS5、自建 reranker）；
> 本报告则回答一个不同的问题：**能否直接集成 mineru-explorer 项目本身（作为外部服务/工具），而非照搬其代码？**

---

## 目录

1. [背景与现状分析](#1-背景与现状分析)
2. [技术可行性分析](#2-技术可行性分析)
3. [架构集成方案建议](#3-架构集成方案建议)
4. [风险评估与挑战](#4-风险评估与挑战)
5. [分阶段实施路线图](#5-分阶段实施路线图)
6. [结论与建议](#6-结论与建议)

---

## 1. 背景与现状分析

### 1.1 EKRS 项目现状

**定位**：工程知识恢复系统（Engineering Knowledge Recovery System），从非结构化文档（PDF/Word/DWG）中提取工程约束条件（温度、压力、材料限值），通过确定性求解器计算参数可行区间，并提供 scope 级冲突检测。

**当前状态**：Phase 1–8 全部完成（`phase8` 标签已于 2026-07-24 force-move 至 HEAD）。346+ 测试通过。

**技术栈**：

| 维度 | EKRS |
|------|------|
| 语言 | Python 3.11+ |
| Web 框架 | FastAPI 0.115 |
| 向量数据库 | Qdrant 1.11（bge-m3 1024d dense + sparse） |
| 关系存储 | aiosqlite（task_repo, documents） |
| 缓存/锁 | Redis（分布式锁 + replay 去重） |
| 嵌入模型 | bge-m3 ONNX（已 vendored 至 Docker 镜像，~2.1 GB） |
| 区间运算 | portion 库（确定性求解器，R2 纯函数） |
| 部署 | docker-compose（qdrant + redis + rag + prometheus） |

**核心架构**（两层流程）：

```
流程 1 — 摄取：Parser(外部) → POST /v1/ingestion/notify → 读取 JSONL → 分块(scope-aware) → 编码 → Qdrant 写入 → 回调
流程 2 — 查询：User POST /v1/constraints → Qdrant 向量检索 → scope 复合评分 → hint 提取 → evidence 构建 → 区间求解器(纯函数) → 多分支结构化输出
```

**铁律 R1–R8（不可违反的设计约束）**：

| 规则 | 核心要求 |
|------|----------|
| R1 | 每个 numeric_hint 必须携带 source_span、block_id、context_window |
| R2 | 求解器是纯函数 — 无 I/O、无状态、无副作用（确定性） |
| R3 | 三闸门流水线：召回 → 提取 → 求解；任一失败阻断结果 |
| R4 | 上下文优先级：User > Explicit_Doc > Inferred_Doc > Default |
| R5 | 仅 scope_path 层级查询 — 禁止图数据库 |
| R6 | strict=true 禁止推断；缺少上下文返回 400 |
| R7 | 每个 hint 携带 scope_path；查询可按 scope 过滤 |
| R8 | 索引层仅过滤非法状态；绝不裁剪权威性 |

**当前检索能力**：单一检索路径 — Qdrant 向量搜索（bge-m3 1024d dense + sparse），复合评分公式为 `vector_score * (1 + scope_priority)`。无关键词索引、无重排器、无查询扩展。

**API 接口**（当前暴露的 HTTP 端点）：
- `POST /v1/ingestion/notify` — 摄取通知（X-Parser-Token 认证）
- `POST /v1/constraints` — 约束查询（三闸门流水线）
- `GET /v1/constraints/trace` — 查询回放
- `GET /v1/ingestion/status/{doc_hash}` — 摄取状态
- `GET /v1/admin/*` — 管理接口（X-Admin-Key）
- `GET /healthz` / `GET /metrics` — 健康/指标

### 1.2 MinerU Document Explorer（QMD）现状

**定位**：Agent 原生的知识引擎 — 为 AI Agent 提供三组工具（检索、文档精读、知识摄取），支持 Markdown、PDF、DOCX、PPTX 四种格式，实现索引 → 检索 → 精读的完整闭环。由 OpenDataLab/MinerU 团队开发，基于 QMD（Tobi Lütke）和 Karpathy LLM Wiki 模式。

**技术栈**：

| 维度 | MinerU Document Explorer |
|------|--------------------------|
| 语言 | TypeScript（Node.js >= 22 或 Bun） |
| 数据库 | SQLite（better-sqlite3 / bun:sqlite）+ sqlite-vec + FTS5 |
| 向量搜索 | sqlite-vec（余弦相似度） |
| 全文搜索 | SQLite FTS5（BM25，porter + unicode61 分词） |
| 嵌入模型 | embeddinggemma-300M（GGUF Q8_0，~300 MB） |
| 重排模型 | qwen3-reranker-0.6b（GGUF Q8_0，~640 MB） |
| 查询扩展 | qmd-query-expansion-1.7B（GGUF Q4_K_M，~1.1 GB，微调模型） |
| LLM 推理 | node-llama-cpp（本地 GGUF，GPU 加速可选） |
| 文档解析 | Python 子进程（pymupdf / python-docx / python-pptx），可选 MinerU Cloud |
| Agent 集成 | MCP Server（stdio + Streamable HTTP，15 个工具） |
| 包分发 | npm（`mineru-document-explorer`），CLI 为 `qmd` |

**核心架构**（三组能力）：

| 能力组 | 工具 | 核心模块 |
|--------|------|----------|
| **检索 (Retrieve)** | query, get, multi_get, status | `hybrid-search.ts`, `search.ts`, `store.ts` |
| **文档精读 (Deep Read)** | doc_toc, doc_read, doc_grep, doc_query, doc_elements, doc_links | `backends/{pdf,docx,pptx,markdown}.ts`, `backends/types.ts` |
| **知识摄取 (Ingest)** | wiki_ingest, doc_write, wiki_lint, wiki_log, wiki_index | `wiki/{lint,log,index-gen}.ts`, `links.ts` |

**混合搜索流水线**（QMD 的核心竞争力）：

```
用户查询
  ↓
初始 FTS 扫描（BM25 探针）
  ↓
强信号检测：topScore ≥ 0.85 && gap ≥ 0.15 ? → 跳过扩展（省 1-2 次 LLM 调用）
  ↓
查询扩展（qmd-query-expansion-1.7B 生成 lex/vec/hyde 变体，去重）
  ↓
并行检索：原始查询(×2 权重) + 每个扩展变体 → BM25(FTS5) + Vector(sqlite-vec)
  ↓
RRF 融合（k=60，原始列表 ×2 权重，top-1 加 0.05 bonus）→ 保留 top 40
  ↓
LLM 重排（qwen3-reranker cross-encoder logprob 评分）
  ↓
位置感知混合：top 1-3 = 75% RRF / 25% reranker；top 4-10 = 60/40；top 11+ = 40/60
```

**关键 API 接口**：

- **MCP HTTP**：`POST /mcp`（JSON-RPC，15 个工具），`POST /query` 或 `POST /search`（REST 搜索 API），`GET /health`
- **SDK**：`createStore()` → `QMDStore` 接口（TypeScript 库模式，支持 `search()`, `get()`, `getBackend()`, `update()`, `embed()` 等方法）
- **CLI**：`qmd query`, `qmd search`, `qmd vsearch`, `qmd doc-toc`, `qmd doc-read` 等

**存储模型**：SQLite 单库（`~/.cache/qmd/index.sqlite`），12 张表（content 内容寻址存储、documents 虚拟路径、documents_fts 全文索引、content_vectors 嵌入分块、vectors_vec 向量索引、llm_cache LLM 缓存、links 链接图、wiki_log/wiki_sources 活动日志、pages_cache/toc_cache/section_map/slide_cache 格式缓存）。

### 1.3 两者对比总览

| 对比维度 | EKRS | MinerU Document Explorer |
|----------|------|--------------------------|
| **领域** | 工程约束提取（温度/压力/材料） | 通用文档探索 / RAG |
| **输出** | 结构化数值区间（per parameter） | 文档片段 + 排序分数 + wiki 页面 |
| **确定性** | 确定性（R2 纯函数求解器） | 非确定性（LLM 在关键路径中） |
| **存储** | Qdrant + aiosqlite + Redis | SQLite + sqlite-vec + FTS5 |
| **嵌入模型** | bge-m3 ONNX（1024d dense + sparse） | embeddinggemma-300M GGUF |
| **检索** | 向量 + scope 复合评分 | BM25 + 向量 + RRF + cross-encoder 重排 |
| **文档解析** | 外部 Parser（JSONL 输入） | 内置（Python 子进程）+ MinerU Cloud |
| **Agent 集成** | 仅 HTTP API | MCP Server（15 工具）+ HTTP API + CLI |
| **认证模型** | X-Parser-Token / X-Admin-Key | MCP 客户端（本地信任） |
| **运行时** | Python（FastAPI） | Node.js/Bun（TypeScript） |

### 1.4 前序研究的结论回顾

前两份研究文档（feature-mapping 和 deep-dive）已经完成了 QMD 功能到 EKRS 的映射分析，核心结论：

1. **推荐移植的模式**（Phase 9 候选）：FTS5 BM25 并行检索、边界评分分块、MCP 工具适配器、版本化迁移、Deep Read API（block/toc/grep）、Wiki Lint/Index
2. **不适用 EKRS 的功能**：多格式后端框架（EKRS 接收已解析 JSONL）、Web 搜索、LLM 生成 wiki 内容、多模态 PDF 图元提取
3. **根本设计分歧**：Karpathy 的 LLM Wiki 模式将 LLM 放在关键路径中（自动维护 wiki），而 EKRS 的 R2 规则禁止 LLM 进入求解路径（必须确定性）

**本报告的新视角**：前序研究关注"把 QMD 的代码/模式移植进 EKRS"；本报告关注"把 mineru-explorer 作为独立服务/工具直接集成到 EKRS 生态中"。两者的工作量、风险、收益截然不同。

---

## 2. 技术可行性分析

### 2.1 技术栈兼容性

#### 2.1.1 语言层面：Python × TypeScript

EKRS 是纯 Python 项目（FastAPI + Pydantic + portion），mineru-explorer 是纯 TypeScript 项目（Node.js + better-sqlite3 + node-llama-cpp）。

**跨语言集成的三条路径**：

| 路径 | 机制 | 延迟 | 复杂度 | 可行性 |
|------|------|------|--------|--------|
| **A. HTTP REST** | mineru-explorer 的 `POST /query` 或 `POST /search` REST 端点 | ~50-200ms（含 LLM 重排） | 低 — 标准 HTTP 调用 | ✅ 高 |
| **B. MCP 协议** | mineru-explorer 的 MCP Streamable HTTP 端点（`POST /mcp`） | 同上 | 中 — 需 Python MCP 客户端 | ✅ 中高 |
| **C. CLI 子进程** | EKRS 调用 `qmd query` CLI | ~5-15s（每次重新加载模型） | 低 | ⚠️ 低（延迟过高） |

**结论**：路径 A（HTTP REST）是最直接、延迟最低的跨语言集成方案。mineru-explorer 的 MCP HTTP Server 已经内置了 `POST /query` 和 `POST /search` REST 端点（见 `src/mcp/server.ts:194-239`），返回标准 JSON。EKRS 通过 Python `httpx` 库即可调用，无需任何额外依赖。

**注意**：mineru-explorer 的 MCP HTTP Server 需要 `qmd mcp --http --daemon` 模式运行，模型常驻内存（VRAM），避免每次调用的 5-15s 模型加载开销。这要求运维层面管理一个额外的常驻进程。

#### 2.1.2 存储层面：Qdrant vs SQLite + sqlite-vec

两个系统使用完全不同的存储引擎：

| 维度 | EKRS | mineru-explorer |
|------|------|-----------------|
| 向量库 | Qdrant（独立服务，gRPC + REST） | sqlite-vec（嵌入式 SQLite 扩展） |
| 全文索引 | 无 | FTS5（SQLite 虚拟表） |
| 数据格式 | DocumentBlockIR JSONL → Chunk | 文件系统扫描 → content hash → 文档分块 |

**关键约束**：两个系统各自独立索引文档，**无法共享向量索引**。EKRS 使用 bge-m3（1024d），mineru-explorer 使用 embeddinggemma-300M（维度不同），向量空间不兼容。这意味着如果要让 mineru-explorer 检索 EKRS 的文档，需要在 mineru-explorer 中**重新索引**一份。

**结论**：存储层不可共享。集成方案必须在存储层之上设计数据流。

#### 2.1.3 运行时依赖

| 依赖 | EKRS 需要 | mineru-explorer 需要 |
|------|-----------|----------------------|
| Python 3.10+ | ✅（核心运行时） | ✅（PDF/DOCX/PPTX 解析子进程） |
| Node.js 22+ / Bun | ❌ | ✅（核心运行时） |
| Qdrant | ✅ | ❌ |
| Redis | ✅ | ❌ |
| SQLite | ✅（aiosqlite） | ✅（better-sqlite3） |
| pymupdf/python-docx/python-pptx | ❌（由外部 Parser 负责） | ✅（内置文档解析） |
| GGUF 模型（~2 GB） | ❌ | ✅（首次使用自动下载） |

**关键发现**：mineru-explorer 已经依赖 Python（用于 PDF/DOCX/PPTX 解析），所以两个系统的运行时环境有 Python 这个交集。但 mineru-explorer 的核心搜索逻辑（TypeScript）不能被 Python 直接调用。

### 2.2 数据流衔接点分析

两个系统的数据流存在天然衔接点，但有关键约束。

#### 2.2.1 衔接点 A：文档解析 → 摄取（Parser → QMD → EKRS）

**当前 EKRS 数据流**：
```
原始文档(PDF/Word/DWG) → 外部 Parser → JSONL(DocumentBlockIR) → EKRS 摄取 → Qdrant
```

**潜在集成数据流**：
```
原始文档(PDF/Word) → mineru-explorer 索引(qmd update) → QMD 检索 → 传回 EKRS
```

**问题**：mineru-explorer 和 EKRS 的外部 Parser 是两个不同的文档处理系统。如果 EKRS 已有 Parser（输出 JSONL），则 mineru-explorer 的文档解析功能是**冗余的**。前序研究已明确排除"多格式后端框架"（feature-mapping §10）。

**但有一个有价值的衔接场景**：如果 mineru-explorer 作为**文档预处理层**——对原始 PDF 做 TOC 提取、段落识别、全文索引——然后将结构化内容输出为 JSONL 供 EKRS 摄取。这需要自定义 mineru-explorer 的输出格式，工作量中等。

#### 2.2.2 衔接点 B：检索增强（QMD 混合搜索 → EKRS 求解器）

**这是最高价值的衔接点。**

**当前 EKRS 检索**：
```
User query → Qdrant 向量搜索(bge-m3) → scope 复合评分 → top-k chunks → hint 提取 → 求解
```

**增强后的检索**：
```
User query → Qdrant 向量搜索(bge-m3) + mineru-explorer 混合搜索(BM25+向量+RRF+重排)
           → 结果融合 → top-k chunks → hint 提取 → 求解
```

**具体实现思路**：
1. EKRS 在 `/v1/constraints` 的 Gate 1（召回阶段）并行调用 mineru-explorer 的 `POST /query`
2. 将 QMD 返回的文档片段与 Qdrant 结果做 RRF 融合
3. 融合后的 top-k 进入 hint 提取阶段

**关键约束**：
- QMD 返回的是文档路径 + 片段（snippet），而 EKRS 需要 block_id + source_span。需要映射层。
- QMD 的文档库必须与 EKRS 的文档库**指向同一批文档**（否则检索结果无法映射回 EKRS 的 block_id）。
- R2 规则要求确定性：QMD 的 LLM 重排引入非确定性（每次查询的 RRF 融合结果可能略有不同）。需要缓存或固定种子。

#### 2.2.3 衔接点 C：文档精读（Deep Read → Agent 工具链）

**EKRS 当前缺失 Deep Read 能力**（前序研究 deep-dive §2.2 已确认）。mineru-explorer 的 6 个 Deep Read 工具（doc_toc, doc_read, doc_grep, doc_query, doc_elements, doc_links）可以直接为 AI Agent 提供文档级导航。

**衔接价值**：当 Agent 通过 EKRS 的 `/v1/constraints` 获得约束结果后，需要**验证证据**——读取原文档的特定章节。这个能力 mineru-explorer 已经开箱即用。

**前提条件**：mineru-explorer 需要索引 EKRS 的原始文档（PDF/Word），这样 Agent 才能通过 QMD 的 doc_read/doc_grep 直接定位到约束来源。

### 2.3 架构集成模式评估

基于以上分析，评估四种集成架构模式：

#### 模式 1：API 对接（Loosely Coupled Services）

```
┌─────────────┐         ┌──────────────────────────┐
│   AI Agent  │────────▶│     EKRS (FastAPI)       │
│ (Claude/    │         │  :8000                   │
│  Cursor)    │         │  POST /v1/constraints    │
│             │         │  GET /v1/blocks/{id}     │
│             │         └──────────┬───────────────┘
│             │                    │ httpx (内部调用)
│             │         ┌──────────▼───────────────┐
│             │────────▶│ mineru-explorer (MCP)    │
│             │         │  :8181                   │
│             │         │  POST /query (hybrid)    │
│             │         │  POST /mcp (doc_read...) │
└─────────────┘         └──────────────────────────┘
```

**优点**：
- 最小侵入性：EKRS 和 QMD 各自独立部署、独立演进
- EKRS 可以在 Gate 1 召回阶段调用 QMD 的 `POST /query` 增强检索
- Agent 可以同时使用两个系统的 MCP 工具
- 两个系统的认证模型互不干扰

**缺点**：
- 文档需要双索引（Qdrant + SQLite），存储成本翻倍
- 两套嵌入模型（bge-m3 vs embeddinggemma），向量空间不兼容
- 需要维护两个常驻服务进程

**工作量**：低-中。EKRS 侧新增一个 QMD client 类（~150 LOC），在 retriever 中增加并行调用 + RRF 融合逻辑。

#### 模式 2：共享存储层（Shared Storage）

```
┌─────────────────────────────────────┐
│         共享文件系统 (JSONL)          │
│  Parser 输出 ←→ EKRS 读取            │
│              ←→ mineru-explorer 索引 │
└──────────┬──────────────┬───────────┘
           │              │
    ┌──────▼──────┐ ┌────▼──────────────┐
    │ EKRS        │ │ mineru-explorer   │
    │ (Qdrant)    │ │ (SQLite + vec)    │
    └─────────────┘ └───────────────────┘
```

**优点**：
- 原始文档只存一份
- 两个系统各自构建适合自己的索引

**缺点**：
- mineru-explorer 需要能读取 EKRS 的 JSONL 格式（DocumentBlockIR），目前不支持
- 需要写一个格式转换层（DocumentBlockIR → mineru-explorer 的文件系统扫描输入）
- 索引一致性维护复杂（一方重新索引时需要通知另一方）

**工作量**：中。需要自定义 mineru-explorer 的 collection 配置 + 格式适配器。

#### 模式 3：QMD 作为 EKRS 的 MCP 工具适配层（Proxy Pattern）

```
┌─────────┐         ┌──────────────────────┐
│  Agent  │────────▶│  EKRS MCP Adapter    │
│         │         │  (新增 Python MCP)    │
│         │         │                      │
│         │         │  工具路由：           │
│         │         │  query → EKRS + QMD  │
│         │         │  doc_read → QMD      │
│         │         │  constraints → EKRS  │
│         │         │  wiki_lint → EKRS    │
└─────────┘         └──┬───────────────┬───┘
                       │               │
              ┌────────▼────┐ ┌────────▼────────┐
              │ EKRS :8000  │ │ QMD :8181       │
              └─────────────┘ └─────────────────┘
```

**优点**：
- Agent 只对接一个 MCP 入口，工具发现更简洁
- 可以实现智能路由：文档精读 → QMD；约束求解 → EKRS
- 统一认证层

**缺点**：
- 新增一个 Python MCP Server（~500-800 LOC），维护成本
- 增加一跳网络延迟（Agent → MCP Proxy → EKRS/QMD）
- 工具命名冲突需要协调

**工作量**：中-高。这是前序研究 deep-dive §4.1 推荐的 Phase 9 方向（MCP tool adapter），但前序研究是将 QMD 的工具模式照搬到 EKRS，本模式则是直接代理 QMD 的现有工具。

#### 模式 4：统一容器编排（Compose-level Integration）

```yaml
# docker-compose.yml (扩展)
services:
  qdrant:     # EKRS 向量库
  redis:      # EKRS 锁/缓存
  rag:        # EKRS FastAPI
  prometheus: # EKRS 监控
  qmd:        # 新增：mineru-explorer MCP daemon
    image: node:22
    command: qmd mcp --http --daemon --port 8181
    volumes:
      - shared_docs:/docs        # 共享文档
      - qmd_cache:/root/.cache/qmd  # 模型缓存
```

**优点**：
- 一键部署（`make dev` 启动所有服务）
- 网络隔离（容器间内网通信）
- 健康检查统一管理

**缺点**：
- mineru-explorer 的 Docker 镜像需要自行构建（项目未提供官方镜像）
- 模型下载（~2 GB GGUF）需要在构建时预下载或运行时拉取
- 容器资源占用增加（额外 Node.js 进程 + VRAM）

**工作量**：低（如果模式 1/2/3 已确定）。主要是编写 Dockerfile 和 compose 配置。

### 2.4 功能互补性分析

| EKRS 能力 | QMD 是否覆盖 | 互补价值 |
|-----------|-------------|----------|
| 工程约束提取（T/P/material） | ❌ 无 | EKRS 独有，QMD 完全不具备 |
| 确定性区间求解（portion） | ❌ 无 | EKRS 独有 |
| Scope 级冲突检测 | ❌ 无 | EKRS 独有 |
| Qdrant 向量检索（bge-m3） | ⚠️ 有但不同模型 | 低互补（都是向量检索） |
| **BM25 关键词检索** | ✅ 有（FTS5） | **高互补**（EKRS 缺失，工程标识符如 `1.6MPa`、`A312-TP316` 需要精确匹配） |
| **Cross-encoder 重排** | ✅ 有（qwen3-reranker） | **高互补**（EKRS 无重排器） |
| **查询扩展** | ✅ 有（qmd-query-expansion-1.7B） | 中互补（工程查询通常精确，价值待测量） |
| **文档精读（TOC/read/grep）** | ✅ 有（6 工具） | **高互补**（EKRS 完全缺失 Deep Read） |
| 文档解析（PDF/DOCX/PPTX） | ✅ 有 | 低互补（EKRS 由外部 Parser 负责） |
| Wiki 知识编译 | ✅ 有（5 工具） | 低互补（与 R2 冲突，前序研究已排除） |
| MCP Agent 集成 | ❌ 无 | **高互补**（EKRS 仅有 HTTP API） |
| 审计日志 / 回放 | ✅ 部分（wiki_log） | 低互补（EKRS 已有 16 事件 schema） |
| Prometheus 监控 | ❌ 无 | EKRS 独有 |

**高互补领域汇总**（集成优先级排序）：
1. **BM25 关键词检索** — 工程文档中的精确标识符（标准号、材料牌号、压力等级）的召回率提升
2. **文档精读工具链** — Agent 验证约束证据时必需的文档内导航
3. **Cross-encoder 重排** — 提升 top-k 精度，减少 hint 提取的假阳性
4. **MCP Agent 集成** — 让 Claude/Cursor 原生调用 EKRS 约束求解

---

## 3. 架构集成方案建议

### 3.1 推荐方案：模式 1 + 模式 4 组合（API 对接 + 容器编排）

**核心理念**：保持两个系统独立部署、独立演进，通过 HTTP API 对接，通过 docker-compose 统一编排。

```
                              ┌────────────────────────────────────────────┐
                              │            docker-compose 网络               │
                              │                                            │
   ┌──────────┐              │  ┌────────────┐     ┌──────────────────┐   │
   │ AI Agent │──────────────│─▶│  EKRS      │────▶│  mineru-explorer │   │
   │ (Claude/ │   HTTP/MCP   │  │  :8000     │ http│  :8181           │   │
   │  Cursor) │              │  │            │ x   │  POST /query     │   │
   │          │──────────────│─▶│            │     │  POST /mcp       │   │
   └──────────┘   MCP(HTTP)  │  └─────┬──────┘     └────────┬─────────┘   │
                              │        │                      │            │
                              │  ┌─────▼──────┐     ┌────────▼─────────┐  │
                              │  │ Qdrant     │     │ SQLite + sqlite  │  │
                              │  │ :6333      │     │ -vec + FTS5      │  │
                              │  └────────────┘     └──────────────────┘  │
                              │  ┌────────────┐                          │
                              │  │ Redis      │     ┌──────────────────┐  │
                              │  │ :6379      │     │ GGUF Models      │  │
                              │  └────────────┘     │ (~2 GB, 持久卷)   │  │
                              │                     └──────────────────┘  │
                              │  ┌────────────────────────────────────┐   │
                              │  │       共享文档卷 (shared_docs)      │   │
                              │  │  原始 PDF/Word ← Parser JSONL →     │   │
                              │  └────────────────────────────────────┘   │
                              └────────────────────────────────────────────┘
```

### 3.2 集成接口设计

#### 3.2.1 EKRS → QMD 检索增强（Gate 1 并行召回）

在 EKRS 的 `EKRSRetriever.retrieve()` 方法中增加 QMD 并行调用：

```python
# 新增：rag/ekrs_rag/retrieval/qmd_client.py
class QMDClient:
    """HTTP client for mineru-explorer hybrid search."""

    def __init__(self, base_url: str = "http://qmd:8181", timeout: float = 3.0):
        self._base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def hybrid_search(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.0,
        rerank: bool = True,
    ) -> list[QMDSearchResult]:
        """Call mineru-explorer POST /query endpoint."""
        payload = {
            "searches": [{"type": "lex", "query": query},
                         {"type": "vec", "query": query}],
            "limit": limit,
            "minScore": min_score,
        }
        if collections:
            payload["collections"] = collections
        resp = await self._client.post("/query", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return [QMDSearchResult(**r) for r in data.get("results", [])]
```

```python
# EKRS retriever 增强（伪代码）
class EKRSRetriever:
    def __init__(self, qdrant: QdrantManager, qmd_client: QMDClient | None = None):
        self._qdrant = qdrant
        self._qmd = qmd_client  # 可选，None 则退化为纯 Qdrant 检索

    async def retrieve(self, query: str, top_k: int = 40, ...):
        # 并行：Qdrant 向量 + QMD 混合搜索
        import asyncio
        tasks = [self._qdrant_search(query, top_k)]
        if self._qmd:
            tasks.append(self._qmd.hybrid_search(query, limit=top_k))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # RRF 融合两个来源的结果
        fused = self._rrf_fuse(results[0], results[1] if len(results) > 1 else [])
        ...
```

**关键设计决策**：
- QMD 调用设置 3s 超时，失败时优雅降级（仅用 Qdrant 结果）
- RRF 融合使用 k=60 参数（与 QMD 内部一致）
- QMD 的结果需要映射回 EKRS 的 block_id（通过文件路径匹配）

#### 3.2.2 Agent → QMD Deep Read（直接 MCP 对接）

Agent 直接配置 mineru-explorer 的 MCP Server，无需通过 EKRS 代理：

```json
// Claude Code 的 ~/.claude/settings.json
{
  "mcpServers": {
    "ekrs": {
      "url": "http://localhost:8000/mcp"  // 假设 Phase 9 实现 EKRS MCP
    },
    "qmd": {
      "url": "http://localhost:8181/mcp"  // mineru-explorer MCP
    }
  }
}
```

Agent 的工作流：
1. 调用 `ekrs.query`（`POST /v1/constraints`）获得约束结果
2. 调用 `qmd.doc_read` 或 `qmd.doc_grep` 验证证据来源
3. 调用 `qmd.doc_toc` 浏览相关章节

#### 3.2.3 QMD REST API 调用契约

mineru-explorer 的 `POST /query` 端点（`src/mcp/server.ts:194-239`）接受：

```json
{
  "searches": [
    {"type": "lex", "query": "1.6MPa flange rating"},
    {"type": "vec", "query": "pressure rating for flange connections"}
  ],
  "collections": ["engineering_docs"],
  "limit": 10,
  "minScore": 0.0,
  "intent": "engineering specification lookup"
}
```

返回：

```json
{
  "results": [
    {
      "docid": "#abc123",
      "file": "specs/GB-150-2011.pdf",
      "title": "压力容器 第1部分：通用要求",
      "score": 0.85,
      "context": "国标压力容器规范",
      "snippet": "  45: 设计压力 1.6MPa 的法兰连接..."
    }
  ]
}
```

EKRS 需要的映射：`file` → `doc_hash`，`snippet` 中的行号 → `source_span`。

### 3.3 数据一致性策略

由于两个系统独立索引，需要确保文档一致性：

| 策略 | 实现 | 适用场景 |
|------|------|----------|
| **共享文档卷** | docker-compose 挂载同一个 `shared_docs` 卷 | 推荐 — 确保两个系统扫描同一批文件 |
| **索引触发联动** | EKRS 摄取完成后调用 `qmd update` | 可选 — 保证 QMD 索引及时更新 |
| **定期同步** | cron 定时执行 `qmd update` | 简单方案 — 容忍短暂不一致 |

### 3.4 认证与安全

| 系统接口 | 认证方式 | 集成建议 |
|----------|----------|----------|
| EKRS `/v1/*` | X-Parser-Token / X-Admin-Key | 不变 |
| QMD `/query` | 无认证（本地信任） | 容器网络隔离 + 不暴露外部端口 |
| QMD `/mcp` | 无认证 | 同上 |

**建议**：mineru-explorer 仅在 docker-compose 内网暴露（`localhost:8181`），不映射到宿主机外部端口。外部 Agent 通过 EKRS 的 MCP 代理访问（如果采用模式 3）。

---

## 4. 风险评估与挑战

### 4.1 高风险项

#### 风险 R1：R2 确定性违反（严重）

**问题**：mineru-explorer 的混合搜索包含 LLM 重排（qwen3-reranker）和查询扩展（qmd-query-expansion-1.7B），这两个组件引入非确定性。同一个查询在不同时间可能返回略有不同的排序结果。如果 EKRS 在 Gate 1 召回阶段使用 QMD 的结果，那么 `/v1/constraints` 的输出将不再是完全确定性的，违反 R2。

**影响**：回放测试（`/v1/constraints/trace`）可能无法精确复现结果。

**缓解方案**：
1. **缓存策略**：对同一查询的 QMD 结果做 LRU 缓存（key = query hash），回放时使用缓存而非重新调用 QMD
2. **确定性降级**：在 QMD 调用中禁用查询扩展和重排（`rerank: false`），仅使用 BM25 + 向量检索（这两者是确定性的）
3. **审计记录**：将 QMD 的完整响应记录在 audit.log 中，回放时从审计日志恢复而非重新调用

**推荐**：方案 2（确定性降级）+ 方案 3（审计记录）组合。BM25 + 向量检索（不含 LLM）是确定性的，可以安全用于 R2 上下文。LLM 重排仅用于 Agent 直接调用（非 EKRS 流水线内）。

#### 风险 R2：运维复杂度翻倍（中等）

**问题**：当前 EKRS 部署栈包含 4 个服务（qdrant, redis, rag, prometheus）。集成 mineru-explorer 后变为 5 个服务，且 QMD daemon 需要管理模型加载/卸载、VRAM 占用、PID 文件生命周期。

**影响**：部署文档、故障排查、性能调优的工作量增加。

**缓解方案**：
- 统一在 docker-compose 中管理，添加健康检查
- QMD daemon 使用 `--daemon` 模式（PID 文件管理），编排层处理重启
- 监控 QMD 的 `/health` 端点，纳入 Prometheus scrape

#### 风险 R3：向量模型不兼容（中等）

**问题**：EKRS 使用 bge-m3（1024d dense + sparse），mineru-explorer 使用 embeddinggemma-300M。两个模型对同一文档产生的向量完全不同。文档需要被两套模型分别索引，存储和计算成本翻倍。

**影响**：摄入新文档时需要触发两次索引（EKRS Qdrant + QMD SQLite）。

**缓解方案**：
- 接受双索引成本（工程文档数量通常在千级别，存储开销可接受）
- 如果统一嵌入模型是硬需求：mineru-explorer 支持自定义嵌入模型（`QMD_EMBED_MODEL` 环境变量），可配置为 bge-m3 的 GGUF 版本（但需要验证 CJK 性能）

### 4.2 中等风险项

#### 风险 R4：跨语言调用的延迟叠加

**问题**：EKRS `/v1/constraints` 的当前延迟预算为 5s（T8-5 chunker baseline p99 = 279µs/doc）。增加 QMD HTTP 调用会叠加 50-200ms（BM25+向量）或 2-8s（含 LLM 重排）。

**影响**：含 LLM 重排的 QMD 调用会显著增加约束查询延迟。

**缓解方案**：
- QMD 调用设置严格超时（3s），超时则优雅降级
- 仅在非 strict 模式下使用 QMD 增强（strict 模式走纯 Qdrant，保证确定性）
- QMD daemon 模式确保模型常驻 VRAM（避免冷启动 5-15s）

#### 风险 R5：文档路径映射脆弱性

**问题**：EKRS 使用 `doc_hash`（SHA256）标识文档，mineru-explorer 使用文件路径 + content hash 标识文档。两个系统的文档标识不同，需要映射层。

**影响**：如果文档路径变更或文件移动，映射会断裂。

**缓解方案**：
- 共享文档卷中使用稳定的目录结构
- 在 EKRS 摄取时记录原始文件路径（已有 `output_path` 字段），QMD 索引时使用相同路径
- 定期校验映射一致性（前序研究的 wiki_lint 模式可借鉴）

#### 风险 R6：mineru-explorer 的 Python 依赖冲突

**问题**：mineru-explorer 的 Python 子进程依赖 `pymupdf`、`python-docx`、`python-pptx`，这些可能与 EKRS 的 Python 环境产生版本冲突（虽然两个系统的 Python 在不同容器中运行，但如果尝试共享 Python 环境则有风险）。

**缓解方案**：**不要共享 Python 环境**。mineru-explorer 在自己的容器中运行独立的 Python（仅用于文档解析），EKRS 在自己的容器中运行独立的 Python。

### 4.3 低风险项

#### 风险 R7：模型下载依赖外网

mineru-explorer 首次使用时从 HuggingFace 下载 ~2 GB GGUF 模型。在受限网络环境（EKRS 的部署场景通常是内网）中，无法下载。

**缓解方案**：在 Docker 构建阶段预下载模型（类似 EKRS Phase 8 T8-3a 的 bge-m3 vendoring 模式），将模型 bake 到镜像中。

#### 风险 R8：license 合规

mineru-explorer 使用 MIT 许可证，EKRS 的 license 需确认是否兼容（通常 MIT 与企业内部使用兼容）。三个 GGUF 模型各自的 license 需单独检查（embeddinggemma 和 qwen3 系列通常为 Apache 2.0 或类似）。

---

## 5. 分阶段实施路线图

### 5.1 Phase 9a — 基础集成（MVP，2-3 周）

**目标**：验证两个系统能通信，Agent 能同时使用两边工具。

| 任务 | 描述 | 工作量 |
|------|------|--------|
| T9a-1 | 编写 mineru-explorer 的 Dockerfile（含 GGUF 模型预下载） | 中 |
| T9a-2 | 在 EKRS docker-compose.yml 中添加 QMD 服务 | 小 |
| T9a-3 | 配置共享文档卷，确保 QMD 能索引 EKRS 的原始文档 | 小 |
| T9a-4 | 编写 QMD collection 配置（指向工程文档目录） | 小 |
| T9a-5 | 端到端验证：`qmd query` 能搜索到工程文档 | 小 |
| T9a-6 | 配置 Agent 的 MCP 客户端，同时连接 EKRS + QMD | 小 |

**验收标准**：
- `docker compose up` 启动 5 个服务（qdrant, redis, rag, prometheus, qmd），全部健康
- `curl http://qmd:8181/health` 返回 `{"status":"ok"}`
- `qmd query "设计压力 1.6MPa"` 返回工程文档片段
- Claude Code Agent 能同时调用 `ekrs` 和 `qmd` 的 MCP 工具

### 5.2 Phase 9b — 检索增强（核心价值，3-4 周）

**目标**：在 EKRS 的 Gate 1 召回阶段集成 QMD 的 BM25 检索，提升工程标识符的召回率。

| 任务 | 描述 | 工作量 |
|------|------|--------|
| T9b-1 | 实现 `QMDClient`（Python httpx 异步 HTTP 客户端） | 小 |
| T9b-2 | 在 `EKRSRetriever` 中增加并行 QMD 调用（确定性模式：仅 BM25+向量，禁用 LLM 重排） | 中 |
| T9b-3 | 实现 RRF 融合（Qdrant 结果 + QMD 结果） | 中 |
| T9b-4 | 文档路径映射层（QMD file path → EKRS doc_hash） | 中 |
| T9b-5 | 集成测试：golden set 回归（现有 50 case 不退化） | 中 |
| T9b-6 | 性能基准：p99 延迟测量（目标 < 5s） | 小 |
| T9b-7 | 审计日志扩展：记录 QMD 调用 + 响应（支持回放） | 中 |

**验收标准**：
- golden set 50 case 全部通过
- 新增 ≥3 个 golden case：包含精确标识符查询（如 `A312-TP316`、`GB/T 12459`）
- BM25 增强后，精确标识符查询的 recall@10 显著优于纯向量
- 回放测试确定性匹配（使用缓存的 QMD 响应）

### 5.3 Phase 9c — Deep Read + MCP 适配（Agent 体验，3-4 周）

**目标**：Agent 能通过 MCP 工具链完成"约束求解 → 证据验证"的完整工作流。

| 任务 | 描述 | 工作量 |
|------|------|--------|
| T9c-1 | EKRS 实现 `GET /v1/blocks/{block_id}` 端点（Deep Read 基础） | 小 |
| T9c-2 | EKRS 实现 MCP Server（Python `mcp` 包，stdio + HTTP transport） | 中 |
| T9c-3 | MCP 工具注册：query（→ /v1/constraints）、get_block（→ /v1/blocks/{id}）、status（→ /healthz） | 中 |
| T9c-4 | Agent 工作流文档：如何组合使用 EKRS + QMD 工具 | 小 |
| T9c-5 | EKRS Skill 文件（类似 QMD 的 SKILL.md）— 教 Agent 如何使用 EKRS | 小 |

**验收标准**：
- Claude Code Agent 能通过 MCP 调用 EKRS 的约束查询
- Agent 能在约束结果返回后，自动调用 QMD 的 `doc_read` 验证证据
- EKRS MCP Server 通过 `mcp inspector` 验证工具发现正常

### 5.4 Phase 9d — LLM 重排集成（可选，延迟敏感，4-6 周）

**目标**：在 Agent 直接调用（非 EKRS 流水线内）的场景下，利用 QMD 的 LLM 重排提升检索精度。

| 任务 | 描述 | 工作量 |
|------|------|--------|
| T9d-1 | 在 QMD collection 配置中启用 CJK 优化的嵌入模型（如 Qwen3-Embedding-0.6B） | 中 |
| T9d-2 | 延迟基准测试：含 LLM 重排的 QMD 查询 p99 | 小 |
| T9d-3 | 评估 cross-encoder 对工程文档的精度提升（A/B 测试） | 中 |
| T9d-4 | 缓存策略：LLM 重排结果缓存（减少重复计算） | 中 |

**决策门槛**：此阶段仅在 Phase 9b 的 golden set 显示纯 BM25+向量融合仍有精度不足时启动。

### 5.5 路线图总结

```
Phase 9a (MVP)          Phase 9b (核心价值)      Phase 9c (Agent体验)     Phase 9d (可选)
    │                        │                        │                       │
    ▼                        ▼                        ▼                       ▼
┌─────────┐           ┌─────────────┐         ┌──────────────┐       ┌───────────────┐
│ Docker  │           │ QMDClient   │         │ EKRS MCP     │       │ LLM 重排      │
│ 编排    │──────────▶│ + RRF 融合  │────────▶│ Server       │──────▶│ A/B 测试      │
│ QMD     │           │ + 路径映射  │         │ + Deep Read  │       │ + 缓存策略    │
│ 服务    │           │ + 审计扩展  │         │ + Skill 文档 │       │               │
└─────────┘           └─────────────┘         └──────────────┘       └───────────────┘
   2-3 周                  3-4 周                  3-4 周                 4-6 周
```

**总预估**：Phase 9a+9b+9c = 8-11 周（核心集成）；Phase 9d 可选 +4-6 周。

---

## 6. 结论与建议

### 6.1 核心结论

1. **技术可行**：mineru-explorer 与 EKRS 的集成在技术上是可行的。两个系统通过 HTTP REST API 对接（`POST /query`）是最简洁的跨语言集成路径，无需修改任一项目的核心代码。

2. **高价值互补**：mineru-explorer 为 EKRS 补齐了三个关键短板——BM25 关键词检索（工程标识符精确匹配）、文档精读工具链（Agent 证据验证）、MCP Agent 集成（原生工具调用）。这些正是前序研究推荐的 Phase 9 方向。

3. **确定性是核心挑战**：mineru-explorer 的 LLM 重排和查询扩展引入非确定性，与 EKRS 的 R2 规则冲突。解决方案是在 EKRS 流水线内仅使用 QMD 的确定性组件（BM25 + 向量），将 LLM 组件留给 Agent 直接调用的场景。

4. **双索引是可接受成本**：两个系统使用不同的嵌入模型和存储引擎，文档需要双索引。对于工程文档的典型规模（千级别），存储和计算成本可接受。

### 6.2 与前序研究的关系

前序两份研究文档（feature-mapping、deep-dive）的结论是"将 QMD 的设计模式移植到 EKRS 内部"——例如在 EKRS 中自建 FTS5 表、自建 reranker 集成。本报告提出了一个**替代路径**：直接集成 mineru-explorer 作为外部服务。

| 方面 | 前序方案（移植模式） | 本报告方案（集成模式） |
|------|---------------------|----------------------|
| BM25 | 在 EKRS 自建 FTS5 表 + AuditWriter 同步 | 直接调用 QMD 的 BM25（已成熟） |
| 重排器 | 在 EKRS 集成 cross-encoder 模型 | 直接调用 QMD 的重排（已有 qwen3-reranker） |
| Deep Read | 在 EKRS 新建 4 个端点 | 直接使用 QMD 的 6 个 Deep Read 工具 |
| MCP | 在 EKRS 新建 MCP Server | EKRS 新建 MCP + 代理 QMD 的工具 |
| 工作量 | ~10-11 任务（移植代码 + 测试） | ~6-8 任务（集成 + 适配） |
| 依赖 | 无新外部服务 | 新增 QMD daemon 服务 |
| 确定性 | 完全可控（EKRS 内部） | 需要隔离 QMD 的 LLM 组件 |
| 演进独立性 | EKRS 完全自主 | 依赖 mineru-explorer 的版本兼容性 |

**建议**：两种方案不互斥。Phase 9 可以先走集成模式（快速验证价值），如果 BM25+检索增强确实有效，再在 Phase 10+ 考虑将关键组件内化到 EKRS（减少外部依赖）。

### 6.3 最终建议

**推荐启动 Phase 9a（MVP）**，以最小成本验证集成价值。具体行动项：

1. 编写 mineru-explorer 的 Dockerfile（预下载 GGUF 模型）
2. 在 EKRS 的 docker-compose.yml 中添加 QMD 服务
3. 配置共享文档卷 + QMD collection
4. 验证 Agent 能同时使用两个系统的工具

如果 Phase 9a 验证成功，按路线图推进 Phase 9b（检索增强）和 Phase 9c（MCP + Deep Read）。

---

## 附录 A：mineru-explorer REST API 参考

### POST /query（或 /search）

非 MCP 的 REST 搜索端点，接受预扩展查询。

**请求**：
```json
{
  "searches": [
    {"type": "lex", "query": "关键词 查询"},
    {"type": "vec", "query": "语义查询"},
    {"type": "hyde", "query": "假设性文档段落"}
  ],
  "collections": ["collection_name"],
  "limit": 10,
  "minScore": 0.0,
  "intent": "领域提示"
}
```

**响应**：
```json
{
  "results": [
    {
      "docid": "#abc123",
      "file": "path/to/document.pdf",
      "title": "文档标题",
      "score": 0.85,
      "context": "集合上下文描述",
      "snippet": "  45: 匹配的文档片段..."
    }
  ]
}
```

### GET /health

```json
{"status": "ok", "uptime": 3600}
```

### POST /mcp（MCP Streamable HTTP）

标准 MCP JSON-RPC 端点，支持 15 个工具。需要先 `initialize` 获取 session ID，然后 `tools/call`。

## 附录 B：EKRS 现有 API 参考

### POST /v1/constraints

```json
// 请求
{
  "query": "设计压力 1.6MPa 的容器壁厚要求",
  "context": {},
  "strict": false,
  "top_k": 40
}

// 响应
{
  "branches": {
    "general": {"wall_thickness": {"value_type": "interval", "interval": {...}}},
    "高温环境": {"wall_thickness": {"value_type": "interval", "interval": {...}}}
  },
  "primary_branch": "general",
  "conflicts": [],
  "trace": [{"gate": "recall", "chunks": 15}, ...],
  "mode": "multi_branch"
}
```

### POST /v1/ingestion/notify

```json
{
  "doc_hash": "sha256...",
  "version": 1,
  "output_path": "/shared/path/to/data.jsonl",
  "callback_url": "http://parser:5000/callback"
}
```

## 附录 C：交叉引用

- 前序研究：[`2026-07-24-mineru-explorer-feature-mapping.md`](2026-07-24-mineru-explorer-feature-mapping.md)
- 深度分析：[`2026-07-24-mineru-deep-dive-extensions.md`](2026-07-24-mineru-deep-dive-extensions.md)
- EKRS 架构文档：`docs/ARCHITECTURE.md`
- EKRS 项目规则：`CLAUDE.md`（七条铁律 R1–R8，注：CLAUDE.md 标题写"Seven"但实际 R1–R8 为八条）
- Phase 8 关闭文档：`docs/superpowers/plans/2026-07-23-phase8-scope.md`
- 铁律全文：`ekrs-handbook.md` §1
- Phase 6+ 延迟项：`ekrs-handbook.md` §6.1
- 部署技术债务：`ekrs-handbook.md` §6.2（PD-1 至 PD-6）
- mineru-explorer 源码：`/home/pangzy/code_project/mineru-explorer/`
- mineru-explorer 架构文档：`mineru-explorer/docs/architecture.md`
- mineru-explorer MCP 文档：`mineru-explorer/docs/mcp.md`
- mineru-explorer SDK 文档：`mineru-explorer/docs/sdk.md`
- Karpathy LLM Wiki gist：<https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
