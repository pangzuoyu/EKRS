# Phase 10 T10d — MCP Adapter (AI Agent → EKRS)

## Context

Parent plan `2026-07-28-phase10-broad-spectrum-retrieval.md` line 44: T10d (MCP adapter) "2-3 周 — 仅依赖 10a search 能力, 可并行". T10a 已闭合 (`phase10` 锁 2e1d9fa, 检索通道就绪). T10b-3 已 ship (incremental inside phase10). T10d 是 10a-* 上方的 Agent 集成层.

研究 `2026-07-24-ekrs-broad-spectrum-retrieval-port-design.md §4.6 + §9.2` 给了完整 design 但估测 2-3 周 (4 子任务) — 与 Phase 10 cadence 不符 (1-2 天/任务). 本计划**压缩到最小可交付**, 大幅裁剪到一个 tight first task + 一个 conditional extension.

**触发问题 (为什么现在)**: Phase 11 路线里 LLM query expansion "移交 Phase 11 via MCP" (parent plan line 128). 在做 Phase 11 前需要 MCP 通路验证可行. 但消费方未到 — 当前 EKRS 没 MCP consumer 在 CI 里.

**决策**: Td.1 = minimal viable (`ekrs_search` + `ekrs_status`, stdio 单 transport), Td.2+ 视 Td.1 集成测试结果 + 真消费方诉求决定.

## Scope 切片

```
Td.1 (本计划范围, 1-2 天)
  rag/ekrs_rag/mcp/server.py — FastMCP server, 2 工具
    ekrs_search(query, top_k=40, active_scope=None)
      → 直接调 EKRSRetriever.retrieve() (内部 reuse, 不走 HTTP)
      → MCP content blocks: JSON 编码 List[Chunk]
    ekrs_status()
      → 返回 /healthz payload (deep checks: retriever/pipeline/...)
  rag/tests/unit/test_mcp_server_td1.py — 6-8 unit tests
    1. server exports exactly 2 tools named ekrs_search + ekrs_status
    2. ekrs_search dispatch (mock retriever) → 调 retrieve(query, top_k)
    3. ekrs_search 输出格式 = MCP TextContent (JSON 字符串, schema 固定)
    4. ekrs_status 输出 = healthz 必需字段 (status + dependencies)
    5. ekrs_search 错误隔离 (retriever raise → MCP ToolError, 不 crash server)
    6. ekrs_search 数据流: FTS 字段 null → OM 字段 (返空 list, 不 5xx)
    7. ekrs_search scope filter 透传 (active_scope → retrieve())
    8. ekrs_status 不依赖 retriever (server start 也可测)

  rag/ekrs_rag/cli.py — `python -m ekrs_rag mcp` 子命令启 stdio server
    (可选; 一期可省, 走 mcp.run 入口)
  rag/tests/integration/test_mcp_stdio_roundtrip.py — 1 integration test
    spawn server subprocess, 通过 mcp.client.stdio 调 ekrs_search, 验证 round-trip
    (heavy integration, @pytest.mark.integration not heavy)

Td.2 (条件, 触发: 消费方出现)
  ekrs_query (POST /v1/constraints 的 MCP 包装, R3+R4+R6 全保留)
  ekrs_get_block (GET /v1/blocks/{id}, 需要新 route — **评估后决定是否加**)

Td.3+ (out of scope unless triggered)
  Claude Code 集成验证 (.mcp.json 配置 + Agent 实跑 EKRS)
  Streamable HTTP transport (stdio 已够 Claude Code; HTTP 是其他 host)
  Cross-process 并发 (server in-process 与 FastAPI 共享 retriever singleton)
```

## 设计要点 (Td.1)

1. **MCP 包** = `mcp>=1.0` (官方 Python SDK, 已 pip install v1.27.0). 用 `FastMCP` (high-level API, 自动生成 `list_tools` + `call_tool` 路由).
2. **stdio-only transport** — Claude Code / MCP inspector / Desktop 都支持 stdio. Streamable HTTP transport 是 PoC 阶段后考虑.
3. **直接复用 EKRSRetriever** — 不走 HTTP, 不绕 FastAPI lifespan. 通过 `FastMCP` Context 注入 retriever 引用 (DI, 模仿 Phase 5.5 E 模式, 不引模块全局).
4. **Chunk → MCP content** — MCP content blocks 只接受 `TextContent` / `ImageContent` / `EmbeddedResource`. Chunks 序列化成 JSON 字符串 + 一段 human-readable summary (chunk.text 前 200 字 + chunk_id).
5. **错误模型** — retriever 抛异常 → 包成 `mcp.McpError("retrieval failed: {e}")`, 不让 server crash (parent plan §204: 业务路径必须 resilient).
6. **审计** — MCP 调用**不**触发新审计事件. MCP 是 wrapper 层, R3 三闸门 + audit 已全在 `retriever.retrieve()` 内完成. 在 mcp_server 加审计会重复 (一个 search 被计两次).
7. **Iron Rules** — R1-R8 全部 ✅ — MCP 只 wrap 现有 API, 不触及 solver / retrieval 逻辑.

## 不做 (Td.1 范围外)

- Streamable HTTP transport — stdio 已够 Claude Code
- `ekrs_get_block` — 需要新 route, 用户消费诉求未到
- `ekrs_query` (全约束求解) — Td.1 只 expose search; query 进 Td.2 视消费方诉求
- 新审计事件 — MCP 是 wrapper, audit 在 retriever 层已做
- MCP resource 提供 (只读 Files 之类) — research 列了但不在 R3 必做里
- 跨进程并发 — server in-process 共享 FastAPI lifespan 的 retriever singleton
- `.mcp.json` 注册文档 / Claude Code 集成手动跑 — Td.3 才做

## Td.1 验收

- [ ] `pytest rag/tests/unit/test_mcp_server_td1.py -v` 6-8 全 pass
- [ ] `pytest rag/tests/integration/test_mcp_stdio_roundtrip.py -v` round-trip pass
- [ ] `mypy rag/ekrs_rag/mcp/` 干净 (沿用 phase 11 clean standard)
- [ ] 完整 suite 不退化 (现 633 unit + 208 golden, 加 ~7 mcp unit = ~640 unit pass)
- [ ] 启动方式: `python -m ekrs_rag mcp` 或 MCP client 直连 stdio (Doc 在 Td.1 任务 docstring)

## Tag + Memory 策略 (延用 Phase 10)

- **`phase10`** 仍是 `2e1d9fa` 锁 (T10a-7 closure, parent §111).
- Td.1 ship = 新 commit inside `phase10`, 沿用既有 incremental 模式 (T10b-3 precedent: 无新 tag, `[Unreleased] ### Added` + handbook §6 加 row).
- memory 文件 `phase10-t10d-td1-closed.md` (pattern 延用 `phase10-t10b-3-closed.md`).
- Td.2+ 不开新 tag until Phase 11 (那时 `phase11` 抬头, T10d closure 是 phase11 任务).

## Decisions to confirm (gstack-review self-check)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Td.1 只 2 tools (search + status), 不引入 query / get_block | Consumption evidence 缺; query 需要 solver 重 verify; get_block 需要新 route |
| D2 | stdio 单 transport, 不做 Streamable HTTP | Claude Code / MCP inspector / Desktop 都支持; HTTP 是 PoC 后期项 |
| D3 | 直接复用 EKRSRetriever, 不走 HTTP internal call | 避免 double rate-limit + double audit; in-process DI 一致 Phase 5.5 E |
| D4 | MCP tool 不发新审计事件 (mcp_search_executed 之类) | Wrapper 层, audit 在 retriever 已做 (fts_searched); 重复 = 双倍写入 |
| D5 | `mcp` 包加进 pyproject.toml [project.dependencies] | Td.1 必需; 不是 dev-only (生产 server 启动 stdio 模式) |
| D6 | FastMCP high-level API (vs low-level Server + 手写 list_tools/call_tool) | python-sdk 1.x 推荐; 代码量 -60%; tool 路由自动 |
| D7 | Td.1 ship 不动 `phase10` tag (incremental inside, 延用 T10b-3 precedent) | parent §111 锁 phase10 = T10a-7 closure; 新 commit picked up by annotated tag |
| D8 | Plan doc 复用现有 plan 路径 (`docs/superpowers/plans/`) + 当天日期文件名 | Project convention (Phase 10 plans 都在这) |

## Open questions (实施前关闭)

1. **`mcp` 包 transitive deps**: mcp 1.27 拉 `httpx` / `pydantic` / `anyio` / `starlette`. EKRS 已经有 httpx + pydantic; anyio + starlette 增量. → **决策**: 直接装, 不做 dep audit (与 FastAPI 共存).
2. **stdio server 的 lifespan**: FastMCP `run()` 是 blocking, 不能与 uvicorn 同 process. **决策**: Td.1 用独立进程 (`python -m ekrs_rag mcp` 启 stdio). 与 FastAPI app **不同进程**, retriever 必须从外部传入 (CLI args) 或重启时重建. Td.1 用 "新建 retriever (无 FTS stub)"; Td.3 评估是否需要共享 (HTTP transport 替代).
3. **MCP server name**: `ekrs` (单一 server) 还是 `ekrs-rag` (多 tool host)? **决策**: `ekrs` per research doc line 978.
4. **测试 stdio round-trip 怎么 trace**: `mcp.client.stdio` 异步启动 server, 调 `session.list_tools()` + `session.call_tool()`.  → **决策**: 用 `pytest-asyncio` (已有) + `mcp.client.stdio`.

---

## GSTACK REVIEW REPORT (self)

**Run**: 1 (rev) · **Status**: pending user confirmation
**Reviewer**: gstack-review self-pass (eng-review lens)
**Scope**: Td.1 minimal viable only

### Findings

| # | Sev | Conf | Finding | Action |
|---|-----|------|---------|--------|
| [C1] | MED | 6/10 | R2 提及 "Td.2 视消费方诉求" 但无触发信号定义 — 主观判定风险 | 加注: Td.2 触发 = (a) 用户明示需要 ekrs_query, OR (b) Claude Code 集成测试实跑需要 query |
| [C2] | MED | 5/10 | "审计不重复" 缺对 Phase 5 audit-pipeline 影响分析 | mcp_server 不调 audit emit; audit 走 retriever. 已在 D4 + 范围"不"段注明 |
| [C3] | LOW | 4/10 | "stdio-only" 排除 HTTP 没给理由 | D2 已注 (Claude Code/inspector/Desktop 全支持); 升级理由到 D2 |

### Quality: 7.5/10 (single pass, no CRITICAL/HIGH)

- 3 项 MED/LOW 可在 ship 前 close (上面 Action 列).
- Td.1 scope 合理 (8 tests + 1 integration 是 Phase 10 单任务体量).
- 风险点 2 (stdio + 独立进程) 决策明确, 不留尾巴进 Td.3.

### Verdict

Plan 可启动 Td.1 — RED phase. D1-D8 已列, 3 self-findings 在 ship 前 closure.

---

## 实施顺序

```
Td.1.1 (RED)    test_mcp_server_td1.py 6-8 测试全 fail (server.py 不存在)
Td.1.2 (GREEN)  实现 rag/ekrs_rag/mcp/server.py minimal (FastMCP + 2 tools)
Td.1.3 (IMPROVE) 1 integration test + 完整 suite 0 退化
Td.1.4 (ship)   CHANGELOG + handbook §6 + memory + FF push master
```

无新 tag (incremental inside phase10, 延用 T10b-3 precedent).
