# EKRS 系统增强后 UI 研究报告

> **研究文档 — 增强后 EKRS 系统的 UI 设计研究。**
> 日期：2026-07-24
> 关联文档：
> - [`2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md`](2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md)（广谱检索移植设计）
> - [`2026-07-24-ekrs-mineru-integration-feasibility.md`](2026-07-24-ekrs-mineru-integration-feasibility.md)（外部集成方案）
>
> 本报告研究范围：EKRS 在 Phase 8 完成后、Phase 9 广谱检索增强后的完整 UI 体系设计。

---

## 目录

1. [现有 UI 现状分析](#1-现有-ui-现状分析)
2. [增强后系统的 UI 需求](#2-增强后系统的-ui-需求)
3. [技术选型分析](#3-技术选型分析)
4. [UI 信息架构设计](#4-ui-信息架构设计)
5. [核心界面详细设计](#5-核心界面详细设计)
6. [数据流与状态管理](#6-数据流与状态管理)
7. [可视化方案设计](#7-可视化方案设计)
8. [MCP/Agent 交互层设计](#8-mcpagent-交互层设计)
9. [性能与延迟设计](#9-性能与延迟设计)
10. [安全与认证设计](#10-安全与认证设计)
11. [实施路线图](#11-实施路线图)
12. [结论与建议](#12-结论与建议)

---

## 1. 现有 UI 现状分析

### 1.1 当前 UI 架构

EKRS 当前只有一个 **Streamlit 调试 UI**（`dev_ui/app.py`，315 行），定位为"开发调试工具"而非生产级用户界面。

**技术栈**：Python + Streamlit + httpx（直接调用 REST API）

**现有四个 Tab**：

| Tab | 功能 | 后端 API | 状态 |
|-----|------|---------|------|
| 文档入库 (Ingest) | 触发 Parser 通知、查看任务状态 | `POST /v1/ingestion/notify`, `GET /v1/ingestion/status/{doc_hash}` | ✅ 可用 |
| 约束查询 (Constraints) | 提交自然语言查询，查看多分支结果 | `POST /v1/constraints` | ✅ 可用 |
| 黄金集验证 (Golden Set) | 运行 golden_set.json 回归测试 | 批量调用 `POST /v1/constraints` | ✅ 可用 |
| 覆盖关系 (Overlays) | 查看 provision_overrides（只读） | 占位（无后端端点） | ⚠️ 占位 |

### 1.2 当前 UI 的局限性

| 维度 | 现状 | 问题 |
|------|------|------|
| **定位** | 开发调试 UI | 非生产级，`rag[prod]` 不安装 Streamlit |
| **部署** | `streamlit run dev_ui/app.py` | 需要 dev 依赖，不打包到生产 Docker 镜像 |
| **认证** | 无（localhost 直连） | 仅支持 X-Admin-Key 可选输入，无完整认证 |
| **广谱检索** | ❌ 不支持 | 无法展示 BM25/向量并行、RRF 融合、重排结果 |
| **检索溯源** | ❌ 不支持 | 无法显示 hint 的 source_span、evidence 链 |
| **文档管理** | ❌ 不支持 | 无文档列表、批量入库、版本管理界面 |
| **可视化** | 仅 `st.json()` | 无区间图、冲突矩阵、检索路径追踪 |
| **实时反馈** | 阻塞式（st.button → 等待） | 无流式输出、无进度指示器（golden set 除外） |
| **Agent 集成** | ❌ 不支持 | 无 MCP 工具浏览、无 Agent 会话视图 |
| **移动端** | ❌ 不支持 | Streamlit 在小屏上体验差 |
| **多语言** | 中文为主 | 界面元素中英混用，无 i18n 框架 |

### 1.3 当前 API 端点全览

增强后的 EKRS 暴露以下 HTTP 端点（来自 `rag/ekrs_rag/api/routes/`）：

| 方法 | 路径 | 认证 | 用途 | UI 对接 |
|------|------|------|------|---------|
| POST | `/v1/ingestion/notify` | X-Parser-Token | 触发摄取 | Tab 1 |
| GET | `/v1/ingestion/status/{hash}` | X-Parser-Token | 查询摄取状态 | Tab 1 |
| POST | `/v1/ingestion/replay` | X-Parser-Token | 重放摄取 | ❌ 无 |
| POST | `/v1/constraints` | X-Parser-Token | 约束查询（三闸门） | Tab 2 |
| POST | `/v1/constraints/trace` | X-Parser-Token | 查询回放 | ❌ 无 |
| POST | `/v1/calculate` | X-Parser-Token | 直接计算 | ❌ 无 |
| POST | `/v1/admin/rebuild-index` | X-Admin-Key | 重建索引 | ❌ 无 |
| POST | `/v1/admin/embedding-cache/flush` | X-Admin-Key | 清空嵌入缓存 | ❌ 无 |
| GET | `/healthz` | 无 | 健康检查 | 侧边栏 |
| GET | `/metrics` | 无 | Prometheus 指标 | ❌ 无 |

**Phase 9 新增端点（规划）**：

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/v1/search` | 广谱检索（BM25+向量+RRF） |
| GET | `/v1/blocks/{block_id}` | 文档块读取（Deep Read） |
| GET | `/v1/documents` | 文档列表 |
| GET | `/v1/documents/{doc_hash}` | 文档详情 |
| GET | `/v1/metrics/dashboard` | 聚合指标（非 raw Prometheus） |

---

## 2. 增强后系统的 UI 需求

### 2.1 用户角色分析

增强后的 EKRS 服务三类用户角色：

| 角色 | 核心场景 | 频率 | UI 复杂度 |
|------|---------|------|----------|
| **工程师** | 输入工程查询，查看约束结果，验证证据溯源 | 日常（多次/天） | 中 |
| **运维管理员** | 文档入库管理、索引重建、监控指标、审计日志 | 偶尔（周/次） | 高 |
| **AI Agent** | 通过 MCP 调用 EKRS 工具，无需可视化 UI | 自动化 | 无（MCP 协议） |

### 2.2 功能需求矩阵

| 功能领域 | 需求 | 优先级 | 对应 Phase 9 特性 |
|----------|------|--------|------------------|
| **广谱检索查询** | 自然语言查询 + BM25/向量双路径可视化 + RRF 融合结果 | P0 | FTS5 + RRF |
| **检索路径追踪** | 展示 Gate 1→1.5→2→3 的完整流水线 + 每步分数 | P0 | 三闸门增强 |
| **约束结果可视化** | 区间图（数轴）、冲突矩阵、多分支对比 | P0 | 求解器（现有） |
| **证据溯源** | 点击约束值 → 跳转到原文档片段（source_span） | P0 | R1 + Deep Read |
| **文档管理** | 文档列表、上传、版本、入库状态 | P1 | 摄取增强 |
| **检索质量仪表盘** | BM25 vs 向量 recall 对比、RRF 提升度、重排效果 | P1 | Phase 9 metrics |
| **系统监控** | Qdrant/FTS5/Redis 健康、延迟直方图、审计事件流 | P1 | Prometheus |
| **MCP 工具浏览** | 可用工具列表、调用历史、参数/返回值查看 | P2 | MCP 适配层 |
| **Golden Set 管理** | 运行回归、添加用例、查看趋势 | P2 | 测试增强 |
| **多语言** | 中文/英文切换 | P2 | i18n |

### 2.3 非功能需求

| 维度 | 要求 |
|------|------|
| **首屏加载** | < 2s（静态资源 CDN 或本地缓存） |
| **查询响应展示** | 流式渲染（不等完整响应，逐步展示 Gate 进度） |
| **移动端适配** | 响应式布局（最小 768px 宽屏可用） |
| **暗色主题** | 默认暗色（工程环境常驻暗色终端） |
| **无障碍** | WCAG 2.1 AA 级（键盘导航、ARIA 标签） |
| **浏览器兼容** | Chrome 100+, Firefox 100+, Safari 15+, Edge 100+ |

---

## 3. 技术选型分析

### 3.1 方案对比

| 方案 | 技术栈 | 优势 | 劣势 | 推荐度 |
|------|--------|------|------|--------|
| **A. 升级 Streamlit** | Python + Streamlit | 零迁移成本，Python 全栈 | 性能瓶颈、定制化差、不适合生产 | ⭐⭐ |
| **B. FastAPI + Jinja2** | Python 模板渲染 | 与后端同语言，部署简单 | 交互能力弱，无前端框架 | ⭐⭐ |
| **C. FastAPI + React SPA** | TypeScript + React + Vite | 生产级、组件丰富、生态成熟 | 双语言栈、构建复杂度高 | ⭐⭐⭐⭐ |
| **D. FastAPI + Vue SPA** | TypeScript + Vue + Vite | 学习曲线低、中文社区强 | 生态略小于 React | ⭐⭐⭐ |
| **E. Next.js 全栈** | TypeScript + Next.js | SSR + API Routes 一体化 | 重框架，与现有 FastAPI 职责重叠 | ⭐⭐ |

### 3.2 推荐：方案 C — FastAPI + React SPA

**理由**：

1. **与 mineru-explorer 技术栈一致**：mineru-explorer 是 TypeScript 项目，其 MCP Server + Web UI 的设计模式可以直接参考
2. **gstack 已有 TypeScript 先例**：EKRS 的 gstack 模块使用 TypeScript（`gstack/browse/`），团队已有 TS 经验
3. **组件生态最丰富**：区间图（recharts/visx）、冲突矩阵（d3）、代码高亮（prism）等都有成熟组件
4. **FastAPI 天然支持**：FastAPI 的 OpenAPI 自动生成就适合 SPA 前端消费
5. **流式渲染**：React 的 SSE/WebSocket 支持查询流水线的实时进度展示

**技术栈明细**：

```
前端：
├── React 18 + TypeScript 5
├── Vite 5（构建工具）
├── TanStack Query（数据获取 + 缓存）
├── Tailwind CSS + shadcn/ui（组件库）
├── recharts / @visx（数据可视化）
├── react-flow（检索路径图）
├── monaco-editor（文档查看/高亮）
├── i18next（多语言）
└── vitest（测试）

后端（已有）：
├── FastAPI（API 服务）
├── Pydantic（数据模型）
├── OpenAPI 自动生成（前端类型生成）
└── SSE / WebSocket（流式推送）
```

### 3.3 前后端数据契约

利用 FastAPI 的 OpenAPI 自动生成能力，前端 TypeScript 类型从后端 Pydantic 模型自动推导：

```bash
# 生成前端类型
npx openapi-typescript http://localhost:8000/openapi.json -o src/types/api.d.ts
```

这样 `ConstraintQueryResponse`、`Chunk`、`NumericHint` 等模型的 TypeScript 类型自动与后端同步。

---

## 4. UI 信息架构设计

### 4.1 整体导航结构

```
┌─────────────────────────────────────────────────────────────────────┐
│  EKRS                                    [🔍 搜索] [👤 用户] [🌙/☀] │
├──────────┬──────────────────────────────────────────────────────────┤
│          │                                                          │
│  📊 仪表盘 │                                                          │
│          │                                                          │
│  🔍 检索  │                  主内容区                                  │
│  ├ 广谱搜索│                                                          │
│  ├ 约束查询│                                                          │
│  └ 检索历史│                                                          │
│          │                                                          │
│  📄 文档  │                                                          │
│  ├ 文档列表│                                                          │
│  ├ 入库管理│                                                          │
│  └ 版本对比│                                                          │
│          │                                                          │
│  📈 分析  │                                                          │
│  ├ 检索质量│                                                          │
│  ├ 冲突矩阵│                                                          │
│  └ 审计日志│                                                          │
│          │                                                          │
│  ⚙ 管理  │                                                          │
│  ├ 索引管理│                                                          │
│  ├ 系统监控│                                                          │
│  ├ MCP工具│                                                          │
│  └ Golden集│                                                         │
│          │                                                          │
├──────────┴──────────────────────────────────────────────────────────┤
│  状态栏：[● Qdrant 正常] [● FTS5 正常] [● Redis 正常] [延迟: 45ms]    │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 页面层级

```
/
├── /dashboard                    仪表盘（概览）
├── /search
│   ├── /search/broad             广谱搜索（BM25+向量+RRF）
│   ├── /search/constraints       约束查询（三闸门）
│   └── /search/history           检索历史
├── /documents
│   ├── /documents                文档列表
│   ├── /documents/:hash          文档详情（分块+索引状态）
│   ├── /documents/ingest         入库管理
│   └── /documents/compare        版本对比
├── /analysis
│   ├── /analysis/quality         检索质量分析
│   ├── /analysis/conflicts       冲突矩阵
│   └── /analysis/audit           审计日志
├── /admin
│   ├── /admin/indexes            索引管理（Qdrant + FTS5）
│   ├── /admin/monitoring         系统监控（Prometheus）
│   ├── /admin/mcp                MCP 工具浏览
│   └── /admin/golden             Golden Set 管理
└── /settings                     系统设置
```

---

## 5. 核心界面详细设计

### 5.1 广谱搜索界面（/search/broad）

这是 Phase 9 的核心新增界面，展示增强后的 BM25+向量+RRF 混合检索。

```
┌─────────────────────────────────────────────────────────────────────┐
│  广谱搜索                                                             │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  🔍 输入查询...                                    [搜索] │  │
│  │                                                               │  │
│  │  Scope: [项目▼]  Strict: [☐]  Top-K: [40]  重排: [☑]      │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ── 检索路径 ────────────────────────────────────────────────────── │
│                                                                     │
│  ┌─────────────┐     ┌──────────────┐     ┌──────────────────┐    │
│  │ Gate 1: 召回 │     │ Gate 1.5:重排  │     │ Gate 2: 提取      │    │
│  │   ✅ 142ms   │────▶│   ⏭ 跳过      │────▶│   ✅ 8ms         │    │
│  │  40 chunks   │     │  强信号短路    │     │  12 hints        │    │
│  └─────────────┘     └──────────────┘     └────────┬─────────┘    │
│                                                     │              │
│                                             ┌───────▼─────────┐    │
│                                             │ Gate 3: 求解     │    │
│                                             │   ✅ 3ms        │    │
│                                             │  2 branches     │    │
│                                             └─────────────────┘    │
│                                                                     │
│  ── 双路径结果对比 ──────────────────────────────────────────────── │
│                                                                     │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │  📊 BM25 路径     │  │  🧠 向量路径      │  │  🔀 RRF 融合     │  │
│  │                  │  │                  │  │                  │  │
│  │  1. A312-TP316   │  │  1. 法兰连接压力  │  │  1. A312-TP316   │  │
│  │     score: 0.92  │  │     score: 0.87  │  │     rrf: 0.031   │  │
│  │  2. 1.6MPa 法兰  │  │  2. 容器壁厚要求  │  │  2. 1.6MPa 法兰  │  │
│  │     score: 0.88  │  │     score: 0.84  │  │     rrf: 0.028   │  │
│  │  3. GB/T 12459   │  │  3. 材料选型规范  │  │  3. 法兰连接压力  │  │
│  │     score: 0.85  │  │     score: 0.81  │  │     rrf: 0.025   │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘  │
│                                                                     │
│  ── 融合后 Top-10 ──────────────────────────────────────────────── │
│                                                                     │
│  #  文档片段                              RRF    BM25   向量  Scope │
│  ─  ───────────────────────────────────  ─────  ─────  ────  ───── │
│  1  A312-TP316 不锈钢管力学性能...         0.031  0.92   0.45  国标 │
│  2  设计压力 1.6MPa 的法兰连接选型...       0.028  0.88   0.72  项目 │
│  3  GB/T 12459 钢制对焊管件...             0.025  0.85   0.38  国标 │
│  ...                                                               │
│                                                                     │
│  [点击行展开 → 显示 source_span + evidence 链]                       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**关键交互**：

1. **实时流水线进度**：搜索提交后，Gate 1→1.5→2→3 的进度通过 SSE 流式推送，每步完成立即更新
2. **双路径对比**：并排展示 BM25 和向量各自的 top 结果，RRF 融合结果在右侧
3. **行展开溯源**：点击任意结果行，展开显示：
   - 原文档片段（source_span 高亮）
   - block_id + doc_hash
   - RRF contributions trace（每个来源的贡献分）
   - scope_path 层级面包屑
4. **强信号指示**：如果触发强信号短路，Gate 1.5 显示"⏭ 跳过"并标注原因（"BM25 top score ≥0.85, gap ≥0.15"），并用虚线箭头连接 Gate 1 → Gate 2（绕过 Gate 1.5）
5. **[已裁决见 ADR] 历史回溯数据可用性**：检索历史页（/search/history）和审计日志页（/analysis/audit）必须区分 Track 1（业务级，~1.8 年可用）和 Track 2（详细级，7 天内可用）。超过 7 天的 trace 记录显示"⚠ 详细检索数据已过期"提示，仅展示聚合统计（branches_count, path, duration）。裁决依据：[`2026-07-24-phase9-cross-doc-adjudication.md`](2026-07-24-phase9-cross-doc-adjudication.md) 不一致 3。

### 5.2 约束查询结果可视化（/search/constraints）

```
┌─────────────────────────────────────────────────────────────────────┐
│  约束查询结果                                                         │
│                                                                     │
│  查询: "高温环境下温度限制"        Mode: multi_branch                 │
│  Primary branch: 高温环境                                            │
│                                                                     │
│  ── 分支对比 ──────────────────────────────────────────────────── │
│                                                                     │
│  ┌─────────────────────┐  ┌─────────────────────┐                  │
│  │ 📌 高温环境 (primary)│  │ 📋 通用条件          │                  │
│  │                     │  │                     │                  │
│  │ 设计温度            │  │ 设计温度            │                  │
│  │ ◄════════════════►  │  │ ◄══════════►       │                  │
│  │ 450°C    540°C      │  │ -29°C    50°C      │                  │
│  │ [━━━━━━━━━━━━━━━━] │  │ [━━━━━━━━━━]       │                  │
│  │                     │  │                     │                  │
│  │ 设计压力            │  │ 设计压力            │                  │
│  │ ◄════════════════►  │  │ ◄══════════►       │                  │
│  │ 1.0MPa   1.6MPa    │  │ 0.5MPa  1.0MPa    │                  │
│  │ [━━━━━━━━━━━━━━━━] │  │ [━━━━━━━━━━]       │                  │
│  └─────────────────────┘  └─────────────────────┘                  │
│                                                                     │
│  ── 证据溯源 ──────────────────────────────────────────────────── │
│                                                                     │
│  📎 设计温度 [450°C, 540°C]                                        │
│  ├─ GB/T 150-2011 §4.2 (国标, authority=100)                      │
│  │  └─ 📄 block_id: blk_abc123, page: 42                          │
│  │     "...高温环境压力容器设计温度不应超过540°C..."                 │
│  ├─ 项目规格书 SA-2024-001 §3.1 (项目, authority=40)               │
│  │  └─ 📄 block_id: blk_def456, page: 12                          │
│  │     "...本项目设计温度下限为450°C..."                             │
│  └─ [查看完整 evidence 链 →]                                        │
│                                                                     │
│  ⚠ 冲突检测 (0 conflicts)                                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**可视化组件**：

1. **区间数轴图**：使用 recharts 的自定义图，在数轴上绘制区间 `[lower, upper]`
   - 不同 branch 用不同颜色
   - 悬停显示来源文档
   - 点击区间段跳转到证据溯源

2. **证据树**：树形展示每个约束值的完整溯源链
   - 按 R4 优先级排序（User > Explicit_Doc > Inferred_Doc > Default）
   - 每个节点显示 authority_score + scope_path
   - 叶节点可点击展开原文片段

3. **冲突矩阵**：当 `conflicts` 非空时显示
   - 行 = 参数，列 = 来源文档
   - 单元格颜色 = 冲突程度（绿/黄/红）
   - 点击单元格查看冲突详情

### 5.3 文档管理界面（/documents）

```
┌─────────────────────────────────────────────────────────────────────┐
│  文档管理                                          [+ 📥 入库]       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  🔍 [搜索文档...]    Scope: [全部▼]    状态: [全部▼]               │
│                                                                     │
│  ── 文档列表 (127 篇) ─────────────────────────────────────────── │
│                                                                     │
│  ☐ 文档名称                    Scope    版本  状态     Chunks  操作 │
│  ─ ─────────────────────────  ───────  ────  ───────  ──────  ──── │
│  ☐ GB/T 150-2011 压力容器      国标     v3    ✅ 已索引  342   ⋮   │
│  ☐ ASME B31.3 工艺管道         国标     v1    ✅ 已索引  218   ⋮   │
│  ☐ 项目规格书 SA-2024-001      项目     v2    ✅ 已索引  156   ⋮   │
│  ☐ PPG 涂装设施规格书          项目     v1    ⏳ 索引中   0    ⋮   │
│  ☐ 管道材料等级汇总            项目     v1    ❌ 失败    —     ⋮   │
│  ...                                                               │
│                                                                     │
│  [全选] [批量重建索引] [批量删除] [导出列表]                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**文档详情页**（`/documents/:hash`）：

```
┌─────────────────────────────────────────────────────────────────────┐
│  ◀ 返回    GB/T 150-2011 压力容器 (v3)                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  基本信息                                                            │
│  doc_hash: a1b2c3d4...    chunks: 342    scope: [国标, GB, 压力容器]│
│  入库时间: 2026-07-20 14:32    版本: 3    状态: ✅ 已索引            │
│                                                                     │
│  ── 分块预览 ──────────────────────────────────────────────────── │
│                                                                     │
│  Chunk #1 (tokens: 120, scope: [国标, GB, 通用要求])                │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  # GB/T 150-2011 压力容器                                     │  │
│  │  ## 第1部分：通用要求                                          │  │
│  │  本标准规定了压力容器的设计、制造、检验和验收要求...              │  │
│  │                                                               │  │
│  │  💡 NumericHints: 3                                           │  │
│  │  ├─ 设计压力: 1.6 MPa (span: 45-52)                          │  │
│  │  ├─ 设计温度: 540 °C (span: 68-74)                           │  │
│  │  └─ 腐蚀裕量: 3 mm (span: 92-95)                             │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  Chunk #2 (tokens: 95, scope: [国标, GB, 材料])                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  ## 材料选用                                                  │  │
│  │  ...                                                          │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  [加载更多 chunks...]                                               │
│                                                                     │
│  ── 索引状态 ──────────────────────────────────────────────────── │
│  Qdrant: ✅ 342 points (dense: 1024d, sparse: ✅)                   │
│  FTS5:   ✅ 342 rows (tokenized: porter+unicode61)                  │
│  Embedding: bge-m3 ONNX (version: a1b2c3)                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.4 检索质量仪表盘（/analysis/quality）

```
┌─────────────────────────────────────────────────────────────────────┐
│  检索质量分析                                                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  时间范围: [最近7天▼]    查询类型: [全部▼]                           │
│                                                                     │
│  ── 检索路径分布 ──────────────────────────────────────────────── │
│                                                                     │
│         BM25 单独命中     向量单独命中     双路径命中                │
│         ████████ 23%      ██████████ 31%  ████████████████ 46%     │
│                                                                     │
│  ── Recall@10 趋势 ────────────────────────────────────────────── │
│                                                                     │
│  100% ┤                    ┌─────── Phase 9a 上线                   │
│       │     ┌─────────────┘│                                       │
│   95% ┤─────┘               └──────────────                        │
│       │                                                            │
│   90% ┤                                                            │
│       └──────────────────────────────────────────────────────▶     │
│        7/17       7/19       7/21       7/23       7/24             │
│                                                                     │
│  ── BM25 vs 向量 — 精确标识符查询 ────────────────────────────── │
│                                                                     │
│  查询                          BM25命中  向量命中  RRF排名           │
│  ──────────────────────────   ────────  ────────  ────────         │
│  "A312-TP316"                  ✅ #1     ❌ 未命中  #1               │
│  "1.6MPa 法兰"                 ✅ #2     ✅ #5     #2               │
│  "GB/T 12459"                  ✅ #1     ❌ 未命中  #1               │
│  "高温环境温度限制"              ❌ 未命中  ✅ #1     #1               │
│                                                                     │
│  ── 重排效果 (A/B) ───────────────────────────────────────────── │
│                                                                     │
│  无重排 Recall@10: 87.3%                                            │
│  有重排 Recall@10: 91.2%  (+3.9pp)                                  │
│  重排平均延迟: 1.2s (p99: 3.8s)                                    │
│  强信号短路率: 34% (省去重排)                                       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.5 系统监控界面（/admin/monitoring）

```
┌─────────────────────────────────────────────────────────────────────┐
│  系统监控                                                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ── 服务状态 ──────────────────────────────────────────────────── │
│                                                                     │
│  ● RAG Service    正常    uptime: 3d 14h    /healthz: 200          │
│  ● Qdrant         正常    points: 12,847    /healthz: 200          │
│  ● FTS5           正常    rows: 12,847      size: 45MB             │
│  ● Redis          正常    connections: 3    used_memory: 12MB      │
│  ● Prometheus     正常    scrape: 15s       retention: 15d         │
│                                                                     │
│  ── 查询延迟 (p50/p95/p99) ────────────────────────────────────── │
│                                                                     │
│  200ms ┤                              ┌───── p99                   │
│        │         ┌───── p95           │                             │
│  100ms ┤─────────┘                    │                             │
│   50ms ┤──────────────────────────────┘                             │
│        └──────────────────────────────────────────────────────▶     │
│         00:00    06:00    12:00    18:00    24:00                   │
│                                                                     │
│  ── 审计事件流 (最近 20 条) ───────────────────────────────────── │
│                                                                     │
│  14:32:15  constraint_solve_started    query="1.6MPa 法兰"         │
│  14:32:15  constraint_solved           branches=2  trace=abc123   │
│  14:31:08  fts_synced                  doc_hash=a1b2c3 blocks=342  │
│  14:30:42  qdrant_upsert               points=342  doc=a1b2c3     │
│  14:30:01  ingestion_completed         doc_hash=a1b2c3  v=3       │
│  ...                                                               │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. 数据流与状态管理

### 6.1 前端状态架构

使用 TanStack Query（React Query）管理服务器状态，避免全局状态膨胀：

```typescript
// src/hooks/useConstraintsQuery.ts
import { useQuery } from '@tanstack/react-query'

interface ConstraintsQueryVars {
  query: string
  context?: Record<string, unknown>
  strict?: boolean
  top_k?: number
}

export function useConstraintsQuery(vars: ConstraintsQueryVars) {
  return useQuery({
    queryKey: ['constraints', vars],
    queryFn: async () => {
      const resp = await fetch('/v1/constraints', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Parser-Token': getAuthToken(),
        },
        body: JSON.stringify(vars),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      return resp.json() as Promise<ConstraintQueryResponse>
    },
    staleTime: 0,        // 每次都重新请求（查询结果不缓存）
    retry: 1,
  })
}
```

### 6.2 流式查询进度（SSE）

搜索提交后，通过 Server-Sent Events 推送流水线进度：

```typescript
// src/hooks/useSearchStream.ts
export function useSearchStream(query: string) {
  const [progress, setProgress] = useState<SearchProgress | null>(null)

  useEffect(() => {
    const eventSource = new EventSource(
      `/v1/search/stream?query=${encodeURIComponent(query)}`
    )

    eventSource.addEventListener('gate', (e) => {
      const data = JSON.parse(e.data) as GateProgress
      setProgress(prev => ({ ...prev, [data.gate]: data }))
    })

    eventSource.addEventListener('result', (e) => {
      const data = JSON.parse(e.data) as SearchResult
      setProgress(prev => ({ ...prev, result: data, done: true }))
      eventSource.close()
    })

    return () => eventSource.close()
  }, [query])

  return progress
}
```

**后端 SSE 端点设计**（Phase 9 新增）：

```python
# rag/ekrs_rag/api/routes/search.py
from fastapi.responses import StreamingResponse
import json, asyncio

@router.post("/v1/search/stream")
async def search_stream(query: SearchQuery):
    async def event_stream():
        # Gate 1: 召回
        vector_hits = qdrant.search(query.text, top_k=query.top_k)
        fts_hits = fts.search(query.text, limit=query.top_k)
        yield sse_event("gate", {"gate": "recall", "status": "done",
                                  "chunks": len(vector_hits) + len(fts_hits)})

        # RRF 融合
        fused = rrf_fuse(vector_hits, fts_hits)
        yield sse_event("gate", {"gate": "fusion", "status": "done",
                                  "fused_count": len(fused)})

        # Gate 1.5: 重排（可选）
        if not query.strict and reranker:
            reranked = reranker.rerank(query.text, fused[:query.top_k])
            yield sse_event("gate", {"gate": "rerank", "status": "done",
                                      "reranked_count": len(reranked)})

        # Gate 2: 提取
        hints = extract_hints(fused)
        yield sse_event("gate", {"gate": "extraction", "status": "done",
                                  "hints_count": len(hints)})

        # Gate 3: 求解
        result = solver.solve(hints)
        yield sse_event("result", result)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 6.3 文档上传流程

```
用户选择文件 → 前端分片上传 → 后端接收到 SHARED_STORAGE_PATH
    → 前端轮询 GET /v1/ingestion/status/{doc_hash}
    → 后端 Parser 处理 JSONL → POST /v1/ingestion/notify
    → 前端收到 202 → 展示进度条 → 完成
```

---

## 7. 可视化方案设计

### 7.1 区间数轴图（Interval Axis）

用于约束查询结果的参数区间可视化。

**设计**：
- 水平数轴，标注刻度（温度 °C / 压力 MPa / 厚度 mm）
- 每个参数一行，绘制区间段 `[lower, upper]`
- 不同 branch 用不同颜色叠加
- 悬停显示来源文档 + authority_score
- 点击区间段跳转到证据溯源

**技术实现**：自定义 SVG 或 recharts `<ComposedChart>`

```typescript
// src/components/IntervalAxis.tsx
interface IntervalData {
  parameter: string      // "设计温度"
  unit: string           // "°C"
  lower: number | null   // 450
  upper: number | null   // 540
  branch: string         // "高温环境"
  evidence: Evidence[]   // 溯源链
}

function IntervalAxis({ data }: { data: IntervalData[] }) {
  // 按 parameter 分组，每组内按 branch 叠加
  // 使用 d3-scale 计算数轴范围
  // SVG 绘制区间段 + 标签 + 悬浮提示
}
```

### 7.2 检索路径图（Pipeline DAG）

用于展示 Gate 1→1.5→2→3 的流水线执行路径。

**设计**：
- 使用 react-flow 绘制 DAG
- 每个节点 = 一个 Gate，显示状态（✅/⏭/❌）+ 耗时 + 统计
- 边 = 数据流向，标注数据量（chunks/hints/branches）
- 强信号短路路径用虚线

### 7.3 双路径对比视图（Dual-Path Comparison）

用于展示 BM25 和向量检索的并排结果。

**设计**：
- 三栏布局：BM25 | 向量 | RRF 融合
- 每栏独立排序列表
- RRF 栏用连接线标注每个结果的来源（BM25 rank → RRF rank）
- 悬停高亮跨栏的同一文档

### 7.4 冲突矩阵（Conflict Matrix）

用于 Gate 3 检测到冲突时的可视化。

**设计**：
- 热力图：行 = 参数，列 = 来源文档
- 单元格颜色 = 冲突程度（绿=一致，黄=部分重叠，红=矛盾）
- 点击单元格查看冲突详情（两个约束值的区间对比）

### 7.5 审计事件时间线（Audit Timeline）

用于展示审计日志的事件流。

**设计**：
- 垂直时间线，最新事件在顶部
- 每个事件 = 图标 + 时间戳 + 事件类型 + 关键字段
- 可过滤事件类型（constraint_solved, ingestion_completed, fts_synced 等）
- 点击事件展开完整 JSON payload

---

## 8. MCP/Agent 交互层设计

### 8.1 MCP 工具浏览界面（/admin/mcp）

```
┌─────────────────────────────────────────────────────────────────────┐
│  MCP 工具                                                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ── 可用工具 (4) ──────────────────────────────────────────────── │
│                                                                     │
│  🔧 ekrs_query                                                     │
│     查询工程约束（三闸门流水线）                                       │
│     参数: query (str), strict (bool), top_k (int)                  │
│     [测试调用 →]                                                    │
│                                                                     │
│  🔧 ekrs_search                                                    │
│     广谱文档检索（BM25+向量+RRF）                                    │
│     参数: query (str), scope (list), limit (int)                   │
│     [测试调用 →]                                                    │
│                                                                     │
│  🔧 ekrs_get_block                                                 │
│     读取文档块内容（Deep Read）                                       │
│     参数: block_id (str)                                           │
│     [测试调用 →]                                                    │
│                                                                     │
│  🔧 ekrs_status                                                    │
│     系统状态                                                        │
│     参数: 无                                                        │
│     [测试调用 →]                                                    │
│                                                                     │
│  ── 最近调用历史 ──────────────────────────────────────────────── │
│                                                                     │
│  14:32  ekrs_query    "1.6MPa 法兰"     → 2 branches  142ms       │
│  14:28  ekrs_search   "A312-TP316"      → 10 results   85ms       │
│  14:25  ekrs_get_block blk_abc123       → 1200 chars    3ms       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.2 Agent 配置指引

界面提供 Agent 配置片段的复制粘贴：

```json
// Claude Code 的 ~/.claude/settings.json
{
  "mcpServers": {
    "ekrs": {
      "url": "http://localhost:8000/mcp",
      "transport": "streamable-http"
    }
  }
}
```

界面提供"复制配置"按钮 + 连接测试。

---

## 9. 性能与延迟设计

### 9.1 前端性能预算

| 场景 | 预算 | 策略 |
|------|------|------|
| 首屏加载 | < 2s | Vite 代码分割 + 路由懒加载 |
| 搜索提交 → 首个结果 | < 200ms | SSE 流式推送 Gate 进度 |
| 文档列表加载 | < 500ms | TanStack Query 缓存 + 分页 |
| 区间图渲染 | < 100ms | SVG 虚拟化（仅渲染可见区间） |
| 审计日志加载 | < 1s | 虚拟滚动 + 增量加载 |

### 9.2 后端 API 延迟（增强后预估）

| 端点 | Phase 8 | Phase 9 (无重排) | Phase 9 (含重排) |
|------|---------|------------------|-----------------|
| POST /v1/constraints | ~100ms | ~140ms (+FTS+RRF) | ~2-8s (+rerank) |
| POST /v1/search | — | ~60ms | ~2-8s |
| GET /v1/blocks/{id} | — | ~5ms | ~5ms |
| GET /v1/documents | — | ~50ms | ~50ms |

### 9.3 大数据集优化

| 场景 | 数据量 | 优化策略 |
|------|--------|---------|
| 文档列表 | 1000+ 篇 | 分页（50/页）+ scope 过滤 |
| 审计日志 | 100K+ 条 | 虚拟滚动 + 时间范围过滤 |
| 区间图 | 100+ 参数 | 仅渲染 top-20 + "显示更多" |
| 检索结果 | 40+ chunks | 前端分页（10/页）+ 懒加载展开 |

---

## 10. 安全与认证设计

### 10.1 认证流程

```
用户访问 UI
    ↓
检查 localStorage 中的 token
    ├─ 无 token → 重定向到登录页
    └─ 有 token → 验证 token 有效性 (GET /v1/auth/verify)
        ├─ 有效 → 加载 UI
        └─ 无效 → 重定向到登录页
```

**登录页**：
- 输入 PARSER_TOKEN（查询权限）和/或 ADMIN_KEY（管理权限）
- Token 存储在 sessionStorage（关闭浏览器即失效）
- 不存储在 cookie（避免 CSRF）

### 10.2 API 调用认证

```typescript
// src/lib/api.ts
const api = {
  getHeaders() {
    const parserToken = sessionStorage.getItem('parser_token')
    const adminKey = sessionStorage.getItem('admin_key')
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    }
    if (parserToken) headers['X-Parser-Token'] = parserToken
    if (adminKey) headers['X-Admin-Key'] = adminKey
    return headers
  },
}
```

### 10.3 权限分级

| 操作 | 需要的 Token | UI 行为 |
|------|-------------|---------|
| 查询约束 | X-Parser-Token | 默认可用 |
| 广谱搜索 | X-Parser-Token | 默认可用 |
| 查看文档 | X-Parser-Token | 默认可用 |
| 入库管理 | X-Parser-Token | 需要输入 |
| 重建索引 | X-Admin-Key | 灰色禁用（无 Admin Key 时） |
| 清空缓存 | X-Admin-Key | 灰色禁用 |
| 查看审计 | X-Admin-Key | 灰色禁用 |

### 10.4 安全注意事项

- **CORS**：FastAPI 配置 `CORSMiddleware`，仅允许 UI 的 origin
- **CSP**：前端配置 Content-Security-Policy 头
- **HTTPS**：生产环境强制 HTTPS
- **Token 过期**：Token 无过期机制（EKRS 使用静态 token），UI 应提示用户定期轮换
- **审计**：所有 UI 操作（查询、入库、管理）都通过后端 API，审计日志覆盖完整

---

## 11. 实施路线图

### 11.1 分阶段计划

| 阶段 | 内容 | 工作量 | 依赖 |
|------|------|--------|------|
| **UI-1: 基础框架** | React 项目搭建 + 路由 + 认证 + API 层 | 1 周 | 无 |
| **UI-2: 约束查询** | 约束查询界面 + 区间图 + 证据树 | 2 周 | UI-1 |
| **UI-3: 广谱搜索** | 双路径对比 + RRF 融合 + 流水线图 | 2 周 | UI-1, Phase 9a |
| **UI-4: 文档管理** | 文档列表 + 详情 + 入库 | 1.5 周 | UI-1 |
| **UI-5: 质量分析** | 检索质量仪表盘 + 冲突矩阵 | 1.5 周 | UI-3 |
| **UI-6: 系统监控** | 服务状态 + 延迟图 + 审计流 | 1 周 | UI-1 |
| **UI-7: MCP 浏览** | 工具列表 + 调用历史 + 配置 | 1 周 | Phase 9d |
| **UI-8: 优化部署** | 性能优化 + 移动端 + i18n | 1 周 | UI-2~7 |

**总预估**：10-11 周（可与 Phase 9 后端开发并行）

### 11.2 与 Phase 9 后端的依赖关系

```
Phase 9a (FTS+RRF) ──────▶ UI-3 (广谱搜索界面)
Phase 9b (分块+短路) ─────▶ UI-5 (质量分析)
Phase 9c (重排) ──────────▶ UI-3 (重排效果展示)
Phase 9d (MCP) ───────────▶ UI-7 (MCP 工具浏览)

UI-1 (基础框架) ─────────▶ UI-2 ~ UI-8 (所有界面)
UI-2 (约束查询) ─────────▶ 可立即开始（后端 API 已就绪）
```

### 11.3 技术债务处理

| 现有技术债务 | UI 中的处理 |
|-------------|------------|
| `dev_ui/app.py` (Streamlit) | 保留作为快速调试工具，生产 UI 独立 |
| 无 `/v1/documents` 端点 | UI-4 需要后端新增此端点 |
| 无 `/v1/blocks/{id}` 端点 | UI-3/UI-4 需要后端新增此端点 |
| 无 SSE 流式支持 | UI-3 需要后端新增 `/v1/search/stream` |
| Overlays tab 是占位 | UI-5 中实现完整冲突矩阵 |

---

## 12. 结论与建议

### 12.1 核心结论

1. **现有 Streamlit UI 无法满足增强后系统的需求**。Phase 9 的广谱检索、双路径对比、RRF 融合可视化、检索路径追踪等核心功能需要生产级前端框架。

2. **推荐 React + TypeScript SPA 方案**。与 mineru-explorer 技术栈一致，组件生态丰富，支持 SSE 流式渲染，可利用 FastAPI 的 OpenAPI 自动生成前端类型。

3. **UI 的核心价值是"让黑盒透明化"**。增强后的 EKRS 有 BM25/向量/RRF/重排多条路径，用户需要看到每一步的分数和决策过程，才能信任系统结果。

4. **证据溯源是工程场景的刚需**。工程师查看约束结果时，必须能追溯到原文档的精确位置（source_span + block_id + page_num）。这是 R1 铁律在 UI 层的体现。

### 12.2 优先级建议

**第一优先级（立即开始）**：
- UI-1 基础框架 + UI-2 约束查询界面（不依赖 Phase 9，可立即启动）

**第二优先级（Phase 9a 完成后）**：
- UI-3 广谱搜索界面（依赖 FTS5 + RRF 后端）

**第三优先级（Phase 9 全部完成后）**：
- UI-4 文档管理 + UI-5 质量分析 + UI-6 系统监控

**第四优先级（按需）**：
- UI-7 MCP 工具浏览 + UI-8 优化部署

### 12.3 与 Streamlit dev_ui 的关系

- **保留 Streamlit dev_ui** 作为开发调试工具（快速原型、临时测试）
- **新建 React SPA** 作为生产级 UI（部署到 `dev_ui/web/` 或独立目录）
- 生产 Docker 镜像仅打包 React 构建产物（`dist/`），不安装 Streamlit
- FastAPI 通过 `StaticFiles` 挂载前端静态文件

```python
# main.py — 挂载前端静态文件
from fastapi.staticfiles import StaticFiles

# API 路由
app.include_router(ingestion_router, prefix="/v1")
app.include_router(constraints_router, prefix="/v1")

# 前端静态文件（生产）
app.mount("/", StaticFiles(directory="dev_ui/web/dist", html=True), name="frontend")
```

---

## 附录 A：API 端点与 UI 页面映射表

| API 端点 | UI 页面 | 组件 | Phase |
|----------|---------|------|-------|
| `POST /v1/constraints` | /search/constraints | ConstraintQueryPanel | 8 (现有) |
| `POST /v1/search` (新) | /search/broad | BroadSearchPanel | 9a |
| `GET /v1/search/stream` (新) | /search/broad | PipelineProgress | 9a |
| `GET /v1/blocks/{id}` (新) | /documents/:hash | BlockViewer | 9d |
| `GET /v1/documents` (新) | /documents | DocumentList | 9 |
| `GET /v1/documents/:hash` (新) | /documents/:hash | DocumentDetail | 9 |
| `POST /v1/ingestion/notify` | /documents/ingest | IngestPanel | 8 (现有) |
| `GET /v1/ingestion/status/:hash` | /documents/ingest | IngestStatus | 8 (现有) |
| `POST /v1/constraints/trace` | /search/history | TraceViewer | 8 (现有) |
| `POST /v1/admin/rebuild-index` | /admin/indexes | IndexManager | 8 (现有) |
| `GET /healthz` | 全局 | StatusBar | 8 (现有) |
| `GET /metrics` | /admin/monitoring | MetricsPanel | 8 (现有) |

## 附录 B：前端技术栈版本推荐

| 依赖 | 版本 | 用途 |
|------|------|------|
| react | 18.3+ | UI 框架 |
| react-dom | 18.3+ | DOM 渲染 |
| react-router-dom | 6.26+ | 路由 |
| @tanstack/react-query | 5.51+ | 服务器状态管理 |
| tailwindcss | 3.4+ | 原子化 CSS |
| @radix-ui/react-* | latest | 无障碍组件原语 |
| recharts | 2.12+ | 数据可视化（区间图） |
| reactflow | 11.11+ | 检索路径 DAG |
| monaco-editor | 0.50+ | 文档查看/代码高亮 |
| i18next | 23.12+ | 多语言 |
| vite | 5.3+ | 构建工具 |
| vitest | 2.0+ | 单元测试 |
| openapi-typescript | 7.0+ | API 类型生成 |
