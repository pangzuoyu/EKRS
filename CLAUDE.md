# CLAUDE.md

Engineering Knowledge Recovery System (EKRS) — extracts structured engineering constraints (temperature, pressure, material limits) from unstructured documents (PDF/Word/DWG), computes parameter feasible ranges via a deterministic solver, and provides scope-aware conflict detection. Full specification: `ekrs-handbook.md`.

## Quick Commands

```bash
make install      # Install dependencies (shared + rag)
make dev          # Start docker-compose + uvicorn + streamlit
make dev-down     # Stop docker
make test         # pytest rag/tests/ -v --tb=short
make test-cov     # With coverage report
make lint         # flake8 + mypy on shared/ and rag/
make mock-notify  # Simulate parser notification for testing

# Run single test
cd rag && pytest tests/unit/test_solver.py -k "test_name" -v

# Run RAG service locally (without Docker)
make run-local
```

## Architecture

Monorepo with three deployable units:

```
shared/ekrs_shared/   → Pydantic models, normalizer (affine temp conversion), audit base
rag/ekrs_rag/         → FastAPI service: ingestion, retrieval (Qdrant), constraint solving
dev_ui/               → Streamlit debug UI (dev only)
deployment/           → docker-compose, k8s manifests
```

Data flow: Parser (external) → `POST /v1/ingestion/notify` → RAG reads JSONL, vectorizes into Qdrant → callback to Parser. Queries: `POST /v1/constraints` → semantic retrieval → NumericHint extraction → interval solver → structured result.

## Seven Iron Rules (must never be violated)

| ID | Rule | Enforcement |
|----|------|-------------|
| R1 | Every numeric_hint must have source_span, block_id, context_window | Validate on ingestion |
| R2 | Solver is a pure function — no I/O, no state, no side effects | Unit test determinism |
| R3 | Three-gate pipeline: recall → extract → solve; any failure blocks the result | Golden set tests |
| R4 | Context priority: User > Explicit_Doc > Inferred_Doc > Default | Show source in output |
| R5 | Only entity-overlap scoring for KG — no graph DB, no multi-hop | No graph DB dependency |
| R6 | strict=true forbids inference; missing context returns 400 | API test |
| R7 | Every hint carries scope_path; queries can filter by scope | Multi-branch tests |
| R8 | Index layer only filters illegal status; never trims authority | Qdrant payload check |

## Key Dependencies

- **Python 3.11+**, FastAPI 0.115, Pydantic 2.8, Qdrant client 1.11
- **portion** — interval arithmetic library (critical for solver, uses factory functions NOT `Interval(left=, right=)` kwargs)
- **bge-m3** ONNX for embeddings (dense 1024d + sparse)
- **Redis** for distributed locks and replay cache
- **aiosqlite** for task state

## Environment Variables

Minimal set in `.env.example`:
- `PARSER_TOKEN` — shared secret for parser↔RAG auth (≥32 chars)
- `SHARED_STORAGE_PATH` — where parser writes JSONL, RAG reads
- `EKRS_DEBUG` — enables debug UI at `/dev-ui` and verbose logging
- `QDRANT_HOST`, `QDRANT_GRPC_PORT`, `REDIS_URL`

## Code Conventions

- All logs: structured JSON via `python-json-logger` (spec §12)
- Audit log (`audit.log`): permanent, size-bounded by rotation (100MB × 5 gzip), records every solve with evidence
- Debug log: only when `EKRS_DEBUG=true`, rotatable, max 100MB x 5 backups
- `shared/` installed as editable dep from both `rag/` and `dev_ui/`

## Current State (as of 2026-08-27)

Phases 1-10 complete; Phase 11 in progress.

- **Phase 1 — Foundation**: shared/ekrs_shared/ (Pydantic models, normalizer, audit base); rag/ekrs_rag/ingestion/ (IR parser, scope-aware chunker, pipeline); rag/ekrs_rag/retrieval/ (Qdrant client); notify/status routes
- **Phase 2 — Solver core (V2)**: hint extractor, evidence builder, interval solver (`portion`), context manager, IR V2 multi-branch, golden set
- **Phase 3 — Scope-aware retrieval**: scope-priority composite scoring, multi-branch output (high-temp / general-condition branches)
- **Phase 4 — System integration**: callback idempotency, TaskRepo (aiosqlite), RedisLock, CompensationScanner, main.py lifespan wiring
- **Phase 5 — Observability**: AuditLogger base + AuditWriter + AuditIndex, Prometheus metrics (route counters, latency, failures), @audited / @metered decorators, query & ingestion replay, debug.log rotation, /healthz JSON endpoint
- **Phase 5.5 D** — `/metrics` sidecar exporter (`prometheus_client` multiproc mode on :9090), docker-compose prometheus service, dropped in-process `/metrics` route
- **Phase 5.5 E** — Module globals → FastAPI `Depends` migration (`get_retriever`, `get_audit_index`, `get_pipeline`, `get_redis_lock`, `get_task_repo`); removed 5 setters
- **Phase 5.5 F** — `audit.log` rotation 100 MB × 5 gzip backups via `RebuildingRotatingFileHandler`; `/healthz` audit suppression via `ContextVar` skip flag; on-rollover callback rebuilds `AuditIndex` so replay offsets stay valid
- **Phase 8 — Production hardening**: /v1/* rate-limit (60 req/min/IP, token bucket), secret rotation SOP + offline validator, vendored bge-m3 ONNX into Docker, ingestion smoke canary, golden set 42→50, 10k chunker bench baseline p99=279µs
- **Phase 9 — Stress tooling**: scripts/live_stress_60.py 3 modes (offline / retry-failed / stress), 60/60 + 200/200 verified, NOTIFY_HTTP_TIMEOUT_S 60, sequential pacing
- **Phase 10 T10b-1 — Chunker refactor**: `_route_accumulated_group` helper unifies Boundary 2 (scope-change) + Boundary 3 (token-overflow); 8 unit tests + 60-doc stress + 10k bench p99=155µs (44% faster than Phase 8 baseline). Tag `phase10.1` locked at commit `1c44eee`.
- **Phase 10 T10a-1 — FTSManager**: SQLite FTS5 BM25 keyword retrieval; `rag/ekrs_rag/retrieval/fts_manager.py` with `generate_chunk_id` + `delete_by_chunk_id` + R7/R8/H2/T10a-5 invariants; 23 unit + 8 integration tests; mypy clean.
- **Phase 10 T10a-2 — Pipeline FTS sync + drift detection**: `IngestionPipeline.ingest()` Step 5.6 paired Qdrant+FTS write; `FTSManager.replace_doc()` (atomic delete-then-upsert for re-ingest idempotency) + `count_active()` (excludes illegal); `QdrantManager.count_points()`; `ConcurrencyChecker` 5min background task compares counts → `fts_consistency_drift` audit + `ekrs_index_consistency_drift_total` counter on drift. Detect-only — never auto-repairs (parent plan §T10a-2 mandate). 10 unit + 5 integration tests; mypy clean; pipeline `fts=None` kwarg keeps Phase 9 baseline byte-level.
- **Phase 10 T10a-3 — RRF pure function + FusionStats**: `rag/ekrs_rag/retrieval/rank_fusion.py` ships `reciprocal_rank_fusion(ranked_lists, key_fn, k=60) -> (fused_results, FusionStats)` (pure R2 function, deterministic) + `FusionStats` frozen dataclass (vector_hits/fts_hits/both_hits) for T10a-7 audit consumption. 17 unit tests cover empty/single/dual/N=3 lists, k parameter, dedup-within-sublist, key_fn exception propagation, set-arithmetic invariant, frozen enforcement. mypy clean. No new tag (phase10.1 stays locked at 1c44eee T10b-1; phase10 reserved for T10a-7 closure).
- **Phase 10 T10a-4 — Retriever RRF integration**: `EKRSRetriever` `async def retrieve()` with `fts: FTSManager | None = None` kwarg; parallel vector+FTS via `asyncio.gather(..., return_exceptions=True)` + `asyncio.to_thread`; FTS exception isolated (log warning + degrade to vector-only); RRF fusion key `f"{doc_hash}:{source_block_ids[0]}"` (T10a-5 will switch to `chunk_id`); `RetrievalResult.fusion_stats: Optional[FusionStats]` field (None=fts disabled, R4 byte-level invariant); `FTSManager.search_with_payload(query) -> [(chunk_id, payload_dict, score)]` (single IN-query, no N+1); `constraints.py:147,208` + `_StubRetriever` + `_make_retriever` mock all migrated to `async def`/`AsyncMock`. 10 unit + 3 IMPROVE boundary + 4 Phase 6B regression + 1 FTSManager `search_with_payload` test = 18 pass; full suite 800 pass 0 regression; mypy clean (1 NEW error @ retriever.py:73 patched). No new tag (phase10 reserved for T10a-7 closure).
- **Phase 10 T10a-5 — chunk_id EKRS-side + FTS↔Qdrant round-trip**: `QdrantManager.upsert_chunks` writes `chunk_id={doc_hash[:8]}-{idx:04d}` into Qdrant payload for every chunk via `FTSManager.generate_chunk_id` (T10a-1 generator, no schema change). `Chunk` model gains `chunk_id: Optional[str] = None` (legacy preservation). New `FTSManager.get_block_id_by_chunk_id(chunk_id)` is the inverse of T10a-1's `get_chunk_id(block_id)` — full bidirectional round-trip covered by `test_round_trip_block_id_and_chunk_id`. Retriever key_fn switches from `f"{doc_hash}:{source_block_ids[0]}"` (T10a-4 fallback) to `c.chunk_id or fallback` for legacy chunks. **Naming-space coexistence** (parent §[M2]): `block_id` (UUID from ir_parser) + `source_block_ids` (list) preserved; `chunk_id` is a parallel field, never a replacement. 7 unit + 3 IMPROVE boundary + 3 FTS bidirectional tests pass; full suite 812 pass 0 regression; mypy clean. No new tag (phase10 reserved for T10a-7 closure).
- **Phase 10 T10a-6 — Golden set 50 case 回归 + BM25 identifier recall@1 数据**: 验证阶段, 不扩 case. `tests/golden_set/` 208 pass (50 case 含参数化), 0 退化. `tests/unit/test_fts_identifier_recall.py` 4 测试测量 `A312-TP316` / `GB/T 12459` / `1.6MPa` 三个工程标识符的 BM25-only recall@1, soft-assertion + stdout log. **结果: 3/3 recall@1=1** — `unicode61 remove_diacritics 2` tokenizer 对 Latin+digit+连字符/斜杠/点 标识符召回干净 (CJK run 是已知限制, 不在 T10a-6 范围). T10c cross-encoder 触发条件 (parent §6.1): 决策数据 3/3 满, 不强制触发. 完整 suite 816 pass 0 退化; mypy clean. No new tag (phase10 reserved for T10a-7 closure).
- **Phase 10 T10a-7 — 审计事件 fts_synced + fts_searched + phase10 closure**: `main.py _EVENT_SCHEMAS` 注册 2 个事件 (event count 20→22): `fts_synced {doc_hash, version, chunks_written}` (pipeline.ingest Step 5.6 emit, schema 在本任务补全) + `fts_searched {vector_hits, fts_hits, both_hits}` (retriever RRF 完成后 emit, 字段直接来自 T10a-3 FusionStats). `EKRSRetriever` 加 `audit_writer: Optional[AuditWriter] = None` kwarg (Phase 5.5 E DI 模式); `audit_writer=None` 默认保留 Phase 9 byte-level baseline. retriever 在 RRF 后 best-effort emit (`try/except` 隔离, 审计失败不传异常 — parent §204). `main.py` lifespan inject `_audit_writer` 到 retriever. **IngestionOutcome enum 不增字段** (parent §204 关闭). 14 t10a7 unit + 2 phase6a-reg test pass; 完整 suite 622 pass + 1 skip; mypy 干净 (无 T10a-7 NEW error); CHANGELOG `[phase10]` release section + version 0.0.5→0.1.0; **`phase10` annotated tag force-move 到本任务闭合 commit**.
- **Phase 10 T10b-3 — 强信号短路 exact-match short-circuit (post-closure incremental)**: `EKRSRetriever._is_exact_match(query, chunks) -> List[int]` 静态谓词 (case-sensitive substring; 空查询 → `[]`); 当 query 是任何 retrieved chunk 的 `chunk.text` 子串时, 短路 RRF 直接返回匹配 chunk + `vector_scores=[1.0]` + `fusion_stats=FusionStats(N,0,0)`. `RetrievalResult.short_circuit: bool = False` 字段 (Phase 10 默认 byte-level 兼容). 短路 gated on `fts is not None` (Phase 9 fts=None 路径 byte-level 不变), 全局启用 NOT strict 门控 (parent §25 + §157: 短路是确定性优化不是 strict-mode 推断). `fts_searched` audit 仍 emit (运营可见性, parent §204). R4 scope_priority 在短路后仍跑 (scope_filter 仍生效); strict mode parity = 同 chunk 集合 (parent §25 (c) 验收). 11 unit (predicate 5 + retriever 4 + scope/strict parity 2) pass; 完整 suite 633 unit + 208 golden + 11 t10b3 = **852 pass 0 退化**; mypy 干净. stub bench `scripts/t10b3_short_circuit_bench.py` (200 corpus / 15+15 queries / 5 warmup): sc_fire_rate=1.0, sc_p99 3.88ms vs rrf_p99 4.45ms (12.7% 减少; ratio 0.87 < 0.99 acceptance; plan-doc aspirational 0.5 留待真实 bge-m3+FTS5+Qdrant backends 验证). T10b-3 是 `phase10` 闭合 commit 后的 incremental — **无新 tag** (`phase10` 已锁 `2e1d9fa`; `phase10.1` 在 `1c44eee` T10b-1 do-not-move).
- **Phase 10 T10d Td.1 — MCP (Model Context Protocol) 适配层最小可行 (post-closure incremental, same tag discipline as T10b-3)**: 新 `rag/ekrs_rag/mcp/server.py` 模块, 用官方 Python `mcp>=1.0` SDK (`FastMCP` high-level API) 暴露 2 工具: `ekrs_search(query, top_k=40, active_scope=None)` 内部直接调 `EKRSRetriever.retrieve()` (no internal HTTP, no double rate-limit, no double audit — T10a-7 `fts_searched` audit 复用) + `ekrs_status()` 返 healthz payload 不依赖 retriever (server boot 不等 retriever ready). JSON-over-TextContent MCP wire format; 异常隔离 (parent §204: retriever raise → `{"error": "..."}` MCP content 不 crash server). chunk.text 截断 200 字 (`CHUNK_TEXT_PREVIEW_CHARS`). stdio 单 transport (Claude Code + MCP inspector + Desktop 都支持; Streamable HTTP 是 PoC 后期项). CLI: `python -m ekrs_rag.mcp.server`. 测试: 8 unit (module imports + tool 注册 + dispatch kwargs + JSON TextContent 输出 + 异常隔离 + 空 chunks + status 独立) + 1 integration (subprocess round-trip initialize + list_tools + call_tool ekrs_status + call_tool ekrs_search error-path). pyproject 加 `mcp>=1.0` [project.dependencies] (生产 dep, 非 dev-only) + `integration` pytest mark. 完整 suite **849 unit + 1 stdio integration + 1 skip pass 0 退化**; mypy `ekrs_rag/mcp/` 干净 (1 dict-item 错误用 `Dict[str, Any]` 显式注解闭合). Td.1 是 `phase10` 闭合 commit 后的 incremental — **无新 tag** (`phase10` 已锁 `2e1d9fa`; `phase10.1` 在 `1c44eee` T10b-1 do-not-move).
- **Phase 10 T10d Td.2 — MCP 适配层扩展 + GET /v1/blocks/{block_id} (post-closure incremental)**: 加 2 工具 + 1 HTTP route + 1 helper extractor. `ekrs_query(query, context, scope, policy, overlay_hints, strict, top_k)` 内部直接调 `evaluate_constraints(retriever, ...)` helper (从 constraints 路由 handler 抽出, 单一 source of truth, 无内部 HTTP — 避免 double rate-limit + double audit), 完整 R3 三闸门 + R4 scope + R6 strict + R7 scope_path 全部透明透传 (solver 是 R2 纯函数). `ekrs_get_block(block_id)` 内部直接调 `QdrantManager.get_payload_by_block_id(block_id)` (scroll + filter, 复用 `get_ingestion_status` lines 274-289 模式, limit=1 利用 UUID 唯一性), 返回完整 block payload (text NOT 截断; 是 document 精读, 不是 search preview); `numeric_hints` 投影为 count-only (避免 MCP message-size 限制); 404 → `{"error": "block_id not found"}` MCP content. **新 HTTP route** `GET /v1/blocks/{block_id}` (新 `rag/ekrs_rag/api/routes/blocks.py` 模块, `require_parser_token` auth 跟 `/v1/constraints` 一致, `BlockResponse` Pydantic 模型, 404/503/500 三态, qdrant 异常隔离), `main.py` lifespan 注册. **Naming 统一 (user feedback)**: 用 `block_id` (UUID) 统一新 route param + MCP tool param, 跟 FTS5 PK + Qdrant payload + 审计事件命名一致; T10a-5 `chunk_id={doc_hash[:8]}-{idx:04d}` 作为并行字段保留 (parent §[M2] 命名空间共存规约), 不替换. **`build_server` DI 扩展** 从 2 args `(retriever, dependencies)` → 4 args `(retriever, qdrant, solver, dependencies)`, closure capture 保留 (Phase 5.5 E DI 模式), 不引全局. CLI entrypoint 仍传 None (PoC zero-config, Td.3 才是 production wiring — Claude Code `.mcp.json` 集成). 测试: 9 mcp unit (`tests/unit/test_mcp_server_td2.py` — 4 imports/build_server + 4 ekrs_query + 3 ekrs_get_block) + 4 blocks-route unit (`tests/unit/test_blocks_route.py` — happy path 200 + 404 + 503 + 500 异常隔离) + 1 stdio integration (`tests/integration/test_mcp_stdio_roundtrip_td2.py` — 4 工具 stdio round-trip). Td.1 `test_build_server_registers_two_named_tools` + Td.1 stdio roundtrip 同步放宽到 "at least 2 tools" contract (Td.2 的 registry 扩展让"exactly 2"失效). 0 退化: 656 unit pass + 1 skip; 完整 stdio integration 2 pass; mypy 干净 (`TextContent` union narrowing 用 `_as_text(content_block) -> TextContent` helper 在两个 stdio roundtrip 测试文件). 10 个 Phase 5/7 integration 失败 (`await RetrievalResult` 在 test_query_replay + test_constraints_api 的旧 sync stub 上) 经 `git stash` round-trip 验证为 pre-existing baseline failure, 跟 Td.2 无关. **Td.2 是 `phase10` 闭合后 incremental — 无新 tag (`phase10` 锁 `2e1d9fa`, `phase10.1` 锁 `1c44eee`)**.
- **Phase 11 T11-1 — dev_ui_v2 scaffold**: 新 `dev_ui_v2/` 目录, React 18.3 + TypeScript 5.5 strict + Vite 5.3.5 + TanStack Query 5.51 + React Router 6.26 + Zod 3.23 + Playwright (T11-3 only). 17 文件 + 6013 LOC. 无 chart library, 无 OpenAPI auto-gen (parent Q#1–Q#8 locked). bundle 55.7 KB gz (9× headroom vs 500 KB cap = parent Q#1 CI 哨兵). `phase11.1` annotated tag force-move + 锁在 `534f0fc` (T11-1 scaffold anchor, do-not-move).
- **Phase 11 T11-2 — typed API client + auth + MSW mock backend**: `src/api/{schemas,client,hooks,context}` + `src/lib/auth` + `tests/mocks/handlers`. 6 Zod schemas mirror Pydantic (REQUEST `.default()` / RESPONSE `.default()`-when-backend-emits). 5 TanStack Query hooks. `X-Admin-Key` 仅 `/v1/admin/*`. MSW handlers = wire-format contract spec (Q#6); wildcard-host patterns (`*/v1/...`) 让同一组 handler 服务 vitest + Playwright. `useAdminKey` = `useSyncExternalStore` + `storage` event + 同 tab custom event (`localStorage.setItem` 在写入 tab 不 fire storage). 56/56 tests pass (19 schema + 14 client + 10 auth + 13 MSW). bundle 70.1 KB gz (7× headroom). **无新 tag** (`phase11.1` 锁 `534f0fc` 不动).
- **Phase 11 T11-3 — 4 React views + Router 6 + Playwright E2E × 6 specs**: Sidebar + ErrorBoundary + Skeleton + Ingest/Constraints/Golden/Overlays views; `BrowserRouter` 5 routes (HomeRedirect, /ingest, /constraints, /golden, /overlays, NotFound) 各包 ErrorBoundary. `tests/mocks/browser.ts` MSW browser worker (Playwright 用, vitest 用 node server); `src/main.tsx` `if (import.meta.env.DEV)` guard 让 Vite tree-shake MSW dynamic import (prod bundle 不含). bundle 82.5 KB gz (6× headroom). 72 unit + 6 E2E pass. 7 bugs caught+fixed (notify.data?.doc_hash, context:{} required, unescaped entity, JSDoc `*/`, MSW chunk leak, MSW dead-in-preview, vitest __tests__ dir crash). **无新 tag** (`phase11` 留给 T11-5 closure).
- **Phase 11 T11-4 — dev_ui_v2 Dockerize + nginx reverse proxy + compose wire**: multi-stage `dev_ui_v2/Dockerfile` (node:20-alpine build → nginx:1.27-alpine runtime, ~40 MB). `nginx.conf` SPA fallback `try_files $uri $uri/ /index.html` + reverse proxy `/v1/*` 和 `/healthz` 到 `rag:8000` (compose service name). compose `dev_ui_v2` service on host :5173, `depends_on: rag healthy`. `NODE_BASE_IMAGE` / `NGINX_BASE_IMAGE` ARGs overridable in `docker-compose.override.yml` for restricted-network mirrors (daocloud.io). Build gotchas: lockfile pinned 564 tencent URLs rewritten to npmmirror; npm 10→11 for "Exit handler never called!" bug; `--include=dev` for build-time devDeps; `.dockerignore` whitelist `!tests/mocks/` (tsc -b resolves dynamic import path). Smoke verified standalone + full stack (proxy returns 503 on admin paths, 403 on parser-token paths — auth layer intact). 5 CI gates green: typecheck + lint + 72 tests + format + bundle 82.5 KB gz. **无新 tag**.
- **Phase 11 T11-5 — deprecate dev_ui + phase11 closure**: `dev_ui/README.md` + `dev_ui/app.py` 加 DEPRECATED banner (跟 dev_ui_v2 共存 1-quarter fallback; 完整 removal 推迟 — multi-doc scrub). **Pre-existing baseline fix**: qdrant + rag compose healthchecks 用 `curl` 但两 image 都没装; 改用 `bash -c "exec 3<>/dev/tcp/..."` (dash `/bin/sh` 不支持 `/dev/tcp` 需显式 bash). 修了 qdrant streak 19928 + rag streak 4318 的长期 unhealthy. Full stack smoke 验证: qdrant healthy / redis healthy / rag healthy / dev_ui_v2 healthy. **CHANGELOG `[phase11]` release section + version 0.1.0→0.2.0**. **`phase11` annotated tag force-move 到本任务闭合 commit**.

> **NOTE (2026-08-27)**: Phase 12 / 13a / 13b / 13c / 13c-patch entries are pending in this Current State list (housekeeping gap). The CHANGELOG (top-level) is authoritative — see `## [phase13c]`, `## [phase13c-patch]`, `## [phase13c-c13]` sections. Closing the gap is deferred.

- **Phase 13c-C13 — GPU corpus re-ingest end-to-end + ops hardening (post-13c closure incrementals)**: 5 commits spanning the closure boundary. Pre-closure `357579f` actually unblocked GPU path end-to-end (3 latent bugs fixed: Makefile `pipefail` missing + Dockerfile.gpu `torch verify` absent + stale image lacking corpus bind-mount); pilot 949 s→41 s **23× speedup** verified on 949-bgs GPU encode. Re-ingested 3809 bundles → 3502/3598 = **97.3% success**, Qdrant v=2 = **319,896 chunks**. 96/3598 = 2.7% failed (all `no_chunks`, all classified into 6 structural patterns: 61 `single_table_monolith` + 13 `mixed_type_large` + 11 `tiny_content_fragmented` + 5 `single_block_small` + 4 `few_blocks_any` + 2 `oversized_image_block`). Root cause for 61: doc-to-md `content.structured` 输出占位符（仅含标题 cell), chunkerr 走结构化路径但 `data_rows=[]` → 0 chunks; **不是 chunker bug, 是 doc-to-md 输出契约违反**. Coord hand-off at `docs/coordinations/2026-08-27-doc-to-md-monolithic-tables-and-fragmentation.md` + failed-bundle manifest `deployment/phase13c-c13-failed-bundles-manifest.json` (96 entries, gitignored). Post-closure fixes (commit `3b2c848`): `make gpu-down` precision (was tearing down whole stack via compose `down` ignoring profiles) + ADMIN_KEY plumbing across dual `.env` files (`./.env` for `make run-local`, `deployment/.env` for compose; CPU rag service block had no ADMIN_KEY env at all). Post-closure `0a36e70`: new `make ingest` target wraps gpu-up + re-ingest + gpu-down for one-shot GPU-first corpus load (`RAG_URL=:8001` + `DOCKER_TARGET=deployment-rag-gpu-1` + `--skip-cp`); `ARGS=` required (exits 2 with usage when missing); exit code propagates so wedged ingests still clean up. Post-closure `78df0ec`: GPU container baseline pinning mirrors CPU `rag-image.baseline.json` — `scripts/build_rag_gpu_baseline.sh` writes `deployment/rag-gpu-image.baseline.json` (image SHA + host bge-m3 SHA + Dockerfile.gpu SHA + torch `2.11.0+cu130` + build args); `make gpu-baseline` target wraps it; `make gpu-up` ends with drift check (`docker inspect .Image` ≠ baseline → WARNING; uses `.Image` not `.Id` because container ID changes per restart); WARNING-only (legit build-arg changes produce new SHA without "real" drift). **No new tag** (post-closure incremental; follows Phase 10 T10b-3 + T10d Td.1/Td.2 + Phase 13c-patch discipline).

Tags: `phase5.5-d-metrics-exporter`, `phase5.5-e-retriever-depends`, `phase5.5-f-audit-rotation`, `phase5-observability`, `phase8`, `phase9`, `phase10` (closure at `2e1d9fa`; T10b-3 + T10d Td.1 + T10d Td.2 incrementals 仍在 phase10 内, 无新 tag), `phase10.1` (T10b-1 do-not-move anchor at `1c44eee`), `phase11` (closure at T11-5 commit), `phase11.1` (T11-1 do-not-move anchor at `534f0fc`), `phase13a` (closure at `e5c8f39`), `phase13b` (closure at `4d9523d`), `phase13c` (closure at `7c1865a`; C13 incrementals absorbed under [phase13c-c13] CHANGELOG section, 无新 tag).

## Development Phases (from spec §6)

1. Foundation: DB, versioning, heartbeat, callback server
2. Deterministic solver core: hint extractor, evidence builder, interval solver, context manager
3. Scope-aware retrieval & multi-branch output
4. System integration: idempotent callbacks, distributed locks, reconciliation
5. Observability: Prometheus metrics, audit log, CI gate, Replay mode

All five phases shipped. Phase 5.5 D/E/F were Phase-5 retrofits (sidecar exporter, Depends migration, audit rotation). Next scope (Phase 6) not yet defined in `ekrs-handbook.md`.

## Important Code Patterns

- **portion.Interval**: Use factory functions (`portion.closedopen`, `portion.openclosed`, `portion.open`) NOT `Interval(left=, right=)` kwargs
- **Priority dedup**: Dedup key = (parameter, operator, value, unit) — excludes scope_path. Priority from scope_path prefix: national(100) > industry(80) > enterprise(60) > project(40) > reference(20)
- **Temperature conversion**: Affine (F→C uses (F-32)*5/9, not scalar)
