# Phase 11 — dev_ui (Streamlit) → React 生产 UI

> 计划起稿 2026-07-29
> 选定理由：Phase 10 §Out of scope 第 3 项；现有 dev_ui/app.py 是 Streamlit debug 工具，需要 1:1 port 到 React + Vite + TypeScript，并加 production polish（typed forms / auth-aware routing / Playwright E2E）。
> 前序（必读）:
> - [`2026-07-28-phase10-broad-spectrum-retrieval.md`](2026-07-28-phase10-broad-spectrum-retrieval.md) §Out of scope 第 3 项
> - **[`2026-07-24-ekrs-enhanced-ui-design.md`](../../research/2026-07-24-ekrs-enhanced-ui-design.md) — 基础参考。** 该研究文档（55KB）描述了完整的 8 阶段 / 10-11 周 UI 体系：仪表盘 / 广谱搜索（含 BM25+向量+RRF 三栏 + Pipeline DAG + 区间数轴图）+ 文档管理 + 质量分析 / 系统监控 / MCP 浏览 / i18n / SSE 流式。**Phase 11 不是该研究文档的 1:1 实现** —— 只 port `dev_ui/app.py` 现有 4 tabs + production polish。Operator console 视图（DAG / 区间图 / 审计浏览器 / 监控）属于 Phase 12+ operator console phase（依赖后端 endpoints: /v1/documents, /v1/search/stream, /v1/audit-log）。
> - [`dev_ui/app.py`](../../../../dev_ui/app.py) (200 行, 4 个 tab) — Phase 11 起点
> - `~/.claude/rules/typescript/` (coding-style / testing / patterns)

## 范围分级（明确 Phase 11 vs Phase 12+）

研究文档描绘 8 阶段 UI 体系。Phase 11 只交付 **Tier 1+2**（React 化 + 生产 polish），**Tier 3 operator console 视图表**（区间数轴图 / Pipeline DAG / 冲突矩阵 / 审计浏览器 / SSE 流式 / 文档管理 / 检索质量仪表盘）属于 Phase 12+ —— 这些依赖后端 endpoints（`/v1/documents`, `/v1/search/stream`, `/v1/audit-log`）尚未 ship。

| Tier | 内容 | Phase |
|---|---|---|
| **Tier 1** — 1:1 port | 4 当前 Streamlit tabs 移植到 React（保留现有所有交互） | Phase 11 |
| **Tier 2** — production polish | Typed API client + 持久化 sidebar + ErrorBoundary + skeleton loaders + Playwright E2E | Phase 11 |
| **Tier 3** — operator console | 区间数轴图 / Pipeline DAG / 冲突矩阵 / 审计浏览器 / 文档管理 / SSE 流式 / i18n | Phase 12+ |

## 范围（Tier 1 + Tier 2；Phase 11 实际交付）

**4 view（与当前 4 tab 1:1 对应）**:
1. **Ingest** — POST /v1/ingestion/notify + GET /v1/ingestion/status/{hash}（X-Admin-Key 不需要，read+write ingestion by design）
2. **Constraints** — POST /v1/constraints（含 R3 三闸门响应可视化: mode / primary_branch / conflicts / branches / trace expander）
3. **Golden Set** — runs `tests/golden_set/golden_set.json` 50 case 对 live API（mirror Streamlit 实现, 进度条 + dataframe 报告）
4. **Overlays** — provision_overrides read-only viewer（继承现有 placeholder, writes 走 /v1/admin/*）

**Tier 2 production polish**:
- Sidebar 持久化: API base URL + X-Admin-Key 入 `localStorage`
- API client: typed (Zod schema + Pydantic 模型↔TS 自动生成 via FastAPI OpenAPI) + 内置 `X-Admin-Key` 转发 + 401/403/429 友好错误
- 错误隔离: view-level `ErrorBoundary` + retry button（不 crash 整个 SPA）
- Loading states: 真 skeleton（不是 Streamlit 自动 rerun）
- 暗色主题: 必选 (工程场景常驻暗色终端)

## Out of scope（明确不做 — Phase 12+ / 研究文档 Tier 3）

**Operator console 视图表**（研究文档 §4-7 描绘，但需后端 endpoints 支持）:
- **Audit browser / Audit Timeline** — 需 AuditIndex HTTP endpoint（当前仅文件 + AuditIndex in-process）
- **Embedding cache viewer + flush** — flush 端点已有, list 端点未 ship
- **Blocks deep-read view** — `/v1/blocks/{block_id}` 已存在（Phase 10 Td.2），UI 暴露是 polish 但不是 P0
- **Document management** (`/documents`, `/documents/:hash`, 入库管理, 版本对比) — `GET /v1/documents` 后端 endpoint 未 ship
- **Broad-spectrum search UI** (3 栏 BM25/向量/RRF 对比) — `POST /v1/search` 后端 endpoint 未 ship（MCP `ekrs_search` 已 ship 但不走 HTTP）
- **Pipeline DAG / Search pipeline 实时进度** — SSE endpoint `/v1/search/stream` 未 ship
- **检索质量仪表盘** (Recall@10 趋势 / 重排 A/B) — metrics endpoint 弱（Prometheus raw, 无聚合 API）
- **System monitoring dashboard** — 同上 metrics 聚合 API 缺
- **MCP tool browser** — Phase 10 Td.1 ship stdio only，HTTP transport 未 ship
- **Conflict Matrix 冲突矩阵** — 数据现成 (branches.conflicts)，但 Matrix 是 Tier 3 visualization
- **区间数轴图 / Evidence Tree** — 同上，Tier 3 visualization
- **i18n / 多语言** — 中文 UI 与当前 Streamlit 一致；不做切换（研究 P2）

**部署层 hardening**:
- CSP headers / rate-limit on UI host / CSRF tokens — Phase 12+ 安全加固
- 移动端响应式（研究 §2.3）— Phase 12+

**Operator console features** (system config / user mgmt / etc) — 独立 phase

**后端依赖（Phase 12+ operator console 前必须 ship）**:
- `GET /v1/documents` (列表 + 分页)
- `GET /v1/documents/{doc_hash}` (详情)
- `POST /v1/search` (HTTP 入口对应 MCP ekrs_search)
- `POST /v1/search/stream` (SSE 流式)
- `GET /v1/audit-log` (审计浏览器)
- `GET /v1/metrics/dashboard` (聚合 metrics，非 raw Prometheus)

## T11-* 任务（5 任务 × 5-7 天 = 4-5 周）

| ID | 任务 | 验证 | 文件 |
|---|---|---|---|
| **T11-1** | **Stack + scaffold** — Vite + React + TS + TanStack Query + Zod + React Router 6；package `dev_ui_v2/`（与 `dev_ui/` 并存 dev 期）；npm scripts: `dev / build / preview / typecheck / lint / test:e2e`；bundle 预算 < 500KB gzipped (admin SPA, 不含图表) | `npm run build` 成功 < 500KB；`vite preview` 起 serve；CI 加 typecheck + lint step | `dev_ui_v2/package.json`, `dev_ui_v2/vite.config.ts`, `dev_ui_v2/tsconfig.json` |
| **T11-2** | **API client + auth** — `api.ts` typed client（每个 endpoint 一个 Zod schema + TS type 派生）；`useAdminKey()` hook + `localStorage` 持久化；`apiBase` env + sidebar override；统一 envelope response `{success, data?, error?, meta?}`（[TS rules §patterns](../../../../.claude/rules/typescript/patterns.md)）；401/403/429 友好错误 | 14 unit（每 endpoint ≥ 2：正常 + 错误码）；typed schema 与现有 Pydantic 模型对齐 | `dev_ui_v2/src/api/` (1 文件 = 1 endpoint 类型), `dev_ui_v2/src/lib/auth.ts` |
| **T11-3** | **4 views** — `Ingest / Constraints / Golden / Overlays`；React Router 6 routes；ErrorBoundary per route；Loading skeleton；Constraint view 复刻现有"模式 + primary_branch + conflicts 警告 + branches JSON + trace expander"布局；Golden view 复刻 progress bar + dataframe 报告 | 11 unit (route 注册 4 + ErrorBoundary 3 + Golden progress 1 + auth-gated nav 3)；E2E: Playwright 跑 4 view 端到端 happy path | `dev_ui_v2/src/views/{Ingest,Constraints,Golden,Overlays}.tsx`, `dev_ui_v2/src/App.tsx`, `dev_ui_v2/tests/e2e/*.spec.ts` |
| **T11-4** | **Dockerize + wire compose** — multi-stage: `node:20-alpine` builder → `nginx:1.27-alpine` serve（Nginx 反代 `/v1/*` 到 RAG backend，static 走 `/`），bundle 体积披露；新增 `dev_ui_v2/Dockerfile` + `nginx.conf` + `deployment/docker-compose.dev_ui_v2.yml`（独立 stack, 不污染主 compose）；main compose 加可注释的 `dev_ui_v2` 段 | `docker compose -f deployment/docker-compose.dev_ui_v2.yml up` 起；`curl :8080/` 返 SPA；`curl :8080/v1/healthz`（经 Nginx 反代）返 200 | `dev_ui_v2/Dockerfile`, `dev_ui_v2/nginx.conf`, `deployment/docker-compose.dev_ui_v2.yml` |
| **T11-5** | **Deprecate dev_ui/ + ship** — `dev_ui/` 改 README-only（保留 streamlit 入口给 fallback），`pyproject.toml [dev]` 块保持（不破坏现有 pip install）；CHANGELOG `[Phase 11]` release section；CLAUDE.md `Phase 11 — React UI` 当前态段；CLAUDE.md quick-commands 加 `make dev-ui-v2`；ekrs-handbook.md §6 加 T11 row；memory phase11-closure.md | 0 退化；main suite ≥ Phase 10 baseline 728 pass；新 UI 集成测试按 dev_ui 那批端点对齐 parity | `dev_ui/README.md`, `pyproject.toml`, `CHANGELOG.md`, `CLAUDE.md`, `ekrs-handbook.md`, `memory/phase11-closure.md` |

注：operator console view（audit browser / embedding cache / cross-engine observability）等 Phase 12+ 走。

## 验证闸门（Phase 11 闭合条件）

- [ ] dev_ui_v2 4 view 全部 E2E happy path 通过（Playwright, trace screenshot）
- [ ] bundle 体积 ≤ 500KB gzipped（CI check）
- [ ] mypy + flake8 不退化（49/49 维持）
- [ ] pyproject [dev] 仍可 `pip install -e rag[dev]` + `streamlit run dev_ui/app.py`（1 季度回退窗口）
- [ ] main suite ≥ Phase 10 baseline（728 + 新增 UI E2E）；0 退化
- [ ] all E2E spec 在 mock RAG backend（不依赖 docker）下运行；heavy 标记单独
- [ ] docker-compose.dev_ui_v2.yml 单 stack 起，Nginx 反代 `/v1/*` 通
- [ ] 标签：`phase11` force-move 到 T11-5 commit（参照 `phase8`/`phase9`/`phase10` precedent）

## 标签策略

- **`phase11`** annotated tag force-move 到 T11-5 closure commit. 代表 *delivered state*.
- **`phase11.1`** 锁 T11-1（stack scaffold）完成 commit。do-not-move。后续 T11-* 任务若需回溯 stack 选择时锚定 `phase11.1`。
- **`phase10` / `phase10.1`** 维持现状（`phase10` 在 `2e1d9fa`，`phase10.1` 在 `1c44eee`）。

Tag force-move 命令:
```bash
git tag -f -a phase11 HEAD -m "Phase 11: dev_ui Streamlit → React + Vite + TS UI. Force-moved from T11-5 closure."
git push --force origin refs/tags/phase11:refs/tags/phase11
```

## 风险 + 缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| TS new skillset for team | ramp-up cost / quality variance | T11-1 先 scaffold + 最小 happy path；E2E 锁 shape；eslint + prettier + ts strict 早引入 |
| Bundle bloat | 加载慢 / 移动端体验差 | bundle 预算 500KB gzipped CI check；图表库分阶段（Phase 12+ 再加） |
| Nginx 反代暴露 CORS / X-Admin-Key 转发问题 | 401/403 flood | T11-2 auth hook 入 LocalStorage；T11-4 Nginx 配置严格匹配 `/v1/*` 不放行其他 path；测试用 mock RAG 验证 4 状态码 |
| Streamlit 1 季度回退窗口 + 新 UI 并存 | 文档 drift / 新人迷惑 | CHANGELOG + CLAUDE.md 显式 "deprecation-not-removal"；T11-5 文档强调回退路径 |
| TypeScript ↔ Pydantic schema drift | UI 接受 EKRS 不送字段 | Zod 严格 + e2e 用真 backend（mock RAG 验 Pydantic 模型 wire format）；CI 跑 e2e 时强制 typecheck |
| React strict-mode 双渲染陷阱（TanStack Query + state） | dev 期不一致 | 仅 strict mode 下跑全套 e2e；锁定 React 18.3.x（同 bundle 稳定版） |
| 配套 docs 漂移（CLAUDE.md / ekrs-handbook） | next phase 开工时返工 | T11-5 一次性 ship 4 docs；memory phase11-closure.md 锁定经验 |

## 非功能性要求

- 不引入新 RAG 后端依赖（仍是同 4 endpoint）
- 不破坏 `rag[dev]` pip extra（Streamlit 回退路径）
- bundle CI check 阻止膨胀（500KB gzipped hard cap）
- TS strict mode 必开（`tsconfig.json` `strict: true`）
- ESLint + Prettier 在 dev_ui_v2/ 强制（与团队 PEP 8 / black / isort 等价）
- Playwright E2E 默认跑 CI（不用 `--browser firefox` 之类限制，跟 TS rules §testing 对齐）
- 无 console.log（与 hooks.md 对齐；pino 等价物留给 Phase 12+）

## 切片图（time-budget）

```
Week 1  T11-1 (scaffold) + T11-2 (api + auth) —— 7 days
Week 2  T11-3 4 views (UI) + E2E scaffold — 7 days
Week 3  T11-3 E2E 完成 + T11-4 dockerize — 7 days
Week 4  T11-5 deprecation + docs + ship —— 5 days
─────────────────────────────────────────
Total   ~4-5 周（5 任务 × 5-7 天）
```

## 开放问题（已全部关闭 2026-07-29）

1. **bundle 预算阈值** → **锁定 500KB gzipped** (硬阈值, 不是估算). React + ReactDOM + TanStack Query + React Router 压缩后远低于此; T11-1 baseline 实测若 < 500KB 显著低则此阈值作为"哨兵"防后续误引重型依赖。
2. **Playwright vs Cypress** → **Playwright, 锁定**. 与 `~/.claude/rules/typescript/testing.md` 默认对齐; Trace Viewer + VS Code 扩展 + CI 稳定性优先。
3. **图表库 (recharts / visx)** → **不引入** (Tier 1/2 硬边界). Constraints view 用 JSON tree + Table 1:1 复刻 Streamlit `st.json` + `st.dataframe`。
4. **/mcp 入口 UI** → **不暴露**. Streamable HTTP 是 Phase 12+ scope, UI 给无法使用的入口是糟糕体验。
5. **dev_ui_v2 onboarding 文档** → **T11-5 ship** (CLAUDE.md quick commands 加 `make dev-ui-v2` + ekrs-handbook 加一段)。
6. **E2E mock backend** → **方案 (a)**: mock FastAPI app 放 `dev_ui_v2/tests/mocks/`, CI 不依赖 docker, 前端开发完全解耦. T11-2 之后第一个任务就搭 mock app。
7. **OpenAPI auto-generation (openapi-typescript)** → **不做**, 单一 source of truth (Zod). 4 endpoint 手写 Zod schema 成本低于维护自动化生成管线的成本 + 避免双轨漂移。Pydantic 模型即是 Zod 写法的权威参考, 对照 Pydantic 手写 Zod 即是一种 schema 审查。
8. **目录命名 `dev_ui_v2/`** → 维持现状. 与 `dev_ui/` 物理隔离, 1 季度回退窗口清晰可见; dev_ui/ 移除时自然重组为 `dev_ui/web/` 单层结构。

**全部关闭。T11-1 stack 选型明确**: React 18.3 + TypeScript 5 strict + Vite 5.3 + @tanstack/react-query 5.51+ + react-router-dom 6.26+ + zod 3.23+ + Playwright + ESLint + Prettier. 不引入图表库, 不引入 OpenAPI 自动生成工具。
