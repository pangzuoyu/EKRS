# Phase 4 SDD Progress

Branch: master
Plan: docs/superpowers/plans/2026-06-25-phase4-system-integration.md
Started: 2026-06-25

## Tasks

- [x] Task 1: fakeredis dep (9072dd4, approved)
- [x] Task 2: idempotency (f95b63d, approved)
- [x] Task 3: TaskRepo (cbcdbb7, approved; aiosqlite→sqlite3 deviation accepted)
- [x] Task 4: RedisLock (ce3f1cc, approved; brief len=36→32 correction accepted)
- [x] Task 5: CompensationScanner (95dbbba + 19218cb + 67c19b5, approved after 2 fix rounds)
- [x] Task 6: config + main wiring (3ff5af1, approved)
- [x] Task 7: ingestion route integration (18096bd + 72e09b8 fix, approved)
- [x] Task 8: integration tests (b532e1d, approved)
- [x] Task 9: coverage check (a57d75d + bbe4e50, all 3 new modules at 100%, 252 tests pass)
- [x] Task 17: final whole-branch review (REVIEWED: 2 Critical + 6 Important)
- [x] Task 18: final-review fix wave (7 commits: 241195c..9a7cbca, 207 tests pass across 18 files, 100% coverage held, all 8 findings closed)
- [x] Task 19: Phase 4 complete

## Phase 5 Tasks (in progress)

- [x] Task 1: prometheus-client 依赖 (e6d33b9..056c266, review clean)
- [x] Task 2: AuditLogger 基类 (056c266..8e0d8aa, review clean)
  - Minor: audit.py + test_audit_base.py 缺尾换行; docstring "idempotent" 略误导; test 3 仅断言不抛异常; test 2 未还原 root handler。final review 复审
- [x] Task 3: AuditWriter (8e0d8aa..9102413, review clean)
  - Minor: audit.py:71 死代码 (noqa F841, brief verbatim); __init__.py 暂为 bare docstring (Task 14 再补 re-export)。final review 复审
- [x] Task 4: trace + middleware (9102413..7c76c0b, review clean)
  - Minor: 3 新文件缺尾换行; middleware status_code 写死 200 (异常响应审计不准, Task 14 可考虑 raw ASGI); dict type hint → Mapping[str,str]。final review 复审
- [x] Task 5: Metrics 注册表 (7c76c0b..62c910e, review clean)
  - Subagent crashed mid-task on API 429; controller 验证文件 + 写 report + commit
  - Minor: 报告声称 test_safe_set_works 但测试文件无该测试 (实际 5 测试都在,只是命名错位); Task 8 接线后 series 总数可能超 80 (unit-level guard 已就位)。final review 复审
- [x] Task 6: AuditIndex (62c910e..f035ed3, review clean)
  - 范围外修改 audit.py (Task 3 文件) 修复 test 隔离问题 (stale FileHandlers); reset_index_for_test 在生产代码; _file_handler 未关闭 (Phase 4 healthz 阶段处理); test_audit_index.py 有未用 import workaround。final review 复审
- [x] Task 7: @audited / @metered (f035ed3..783edd0, review clean)
  - Dispatch 描述与 brief 不一致(6 tests + 3-event vs brief 3 tests + single-event); implementer 正确按 brief 行事
  - Minor: status_code 写死 200/500; @metered 不计失败 counter; 未用 import Any; test 3 名字与断言不匹配。final review 复审
- [x] Task 8: /metrics 端点 (783edd0..b8a6622, review clean)
  - Minor: 2 新文件缺尾换行; test 2 名字与 brief 略异(更好); test 1 用 split-and-strip CONTENT_TYPE_LATEST 而非字面量。final review 复审
- [x] Task 9: debug.log RotatingFileHandler (b8a6622..49834ce, review clean)
  - Implementer 未重写报告文件(controller 补写); 测试路径与 brief 略异(test_logging_rotation.py 而非 core/test_logging.py)
  - Minor: 无 debug_log_path 默认值测试; _HANDLER_TAG 常量缺失(stylistic)。final review 复审
- [x] Task 10: Phase 4.5 schema (49834ce..f33b1ab, approved with notes)
  - Important: Test 4 substitution UNIQUE→legacy-NULL 未在报告披露; 3/4 测试名与 brief 略异
  - Minor: test_task_repo_phase45.py:10-11 未用 import tempfile/Path; test 4 用 raw SQL 合成 legacy 而非 ALTER 回填路径
  - Reviewer verdict: "Approved with notes" — 推荐 (a) 恢复 UNIQUE 测试 或 (b) 显式记录替换理由。已记录入册; final review 复审
- [x] Task 11: Query Replay 集成 (f33b1ab..af0afe6, 待 reviewer verdict)
  - Subagent produced files but did not commit/report within timeout; controller 接管 commit + report
  - 5/5 新测试通过 + 6/6 constraint 集成测试通过 (无回归)
  - 范围外变更: 不变(只动 brief 列出的 3 个文件)
- [x] Task 12: Ingestion Replay 端点 (af0afe6..99809e3, 待 reviewer verdict)
  - Brief `ingestion_pipeline.run(...)` 与实际 `_pipeline.ingest(notification)` 签名差异
  - 解决: 在 `IngestionPipeline` 加 `replay(jsonl_path, doc_hash, version)` 方法(parse+chunk+upsert 复用,跳过 callback)
  - 修改了 brief Files 列表外的 `ingestion/pipeline.py`(+35 行,纯增量) — 在报告中披露
  - 5/5 新测试通过 + 16/16 ingestion 集成测试(无回归)
  - MockPipeline fixture 注入路由; brief 未指定,实现中补足
- [x] Task 13: Audit durability (99809e3..c5d1878, 待 reviewer verdict)
  - 3/3 新测试通过; audit_index.py 未修改(已有 JSONDecodeError 处理 + Python 文件迭代天然支持 truncated)
  - 发现 pre-existing 失败: test_query_replay.py 中 2 个测试在全套件运行时报 ValueError(retriever.py:109 unpack),隔离运行通过。Task 13 implementer 正确识别为 Task 11 引入的状态污染。final review 决定是否修复
  - Implementer 未及时写报告(controller 接管补写)
- [x] Task 14: main.py wiring + /healthz (c5d1878..0ea8d24, 待 reviewer verdict)
  - create_app() 工厂 + 15 event schemas + AsyncToThread index build + /healthz JSON endpoint
  - 保留 /health (test_ingestion 引用); UPPER_CASE 配置字段遵循现有约定
  - 1/1 新测试通过; 128 个总测试通过(34 集成 + 92 单元 + 14 phase 4.5)
  - 同样的 pre-existing 2 个 test_query_replay 失败(Task 13 已识别)
- [x] Task 15: .env.example + 测试隔离 (0ea8d24..d38b333, 待 reviewer verdict)
  - .env.example: +4 行 Phase 5 段; conftest.py: 新增 _isolate_prometheus_registry autouse fixture
  - 222/222 单元测试通过; 31/36 集成测试通过 (5 pre-existing 失败)
  - Pre-existing failures: 3× test_metrics_endpoint (Task 8 范围外) + 2× test_query_replay (Task 11 状态污染)
  - 覆盖率 91% aggregate(6 模块); brief 期望 100% — 差距在 error/edge-case handlers(try/except, JSONDecodeError 恢复)。Pre-existing,与 Task 15 无关
  - Self-review checklist 全过(10/10)
- [ ] Task 16: Final whole-branch review (9a7cbca..d38b333, APPROVED with follow-ups)
  - 评审 verdict: Approved with follow-ups; 14-file 实现正确完整,spec-合规
  - Critical: None
  - Important (should fix before merge):
    - I1: Task 11 `deterministic_match` 未返回 response body (brief 明确要求"返回 deterministic_match")
    - I2: Task 15 conftest.py 是 test_metrics_endpoint 失败的根因(autouse fixture 太激进,需 session scope)
    - I3: Task 4 middleware 用 `request.url.path` 而非 `request.scope["route"].path`(当前无 path param,但若未来加 path param 会 cardinality 爆炸)
    - I4: test_query_replay 状态污染(`constraints._retriever` module-level global 未在 test 后重置)
  - Minor: 12 项 (见最终报告; trailing newlines, M1-M11 等)
- [x] Phase 5 fix wave (d38b333..9cfb4c5, 4 Important fixes applied)
  - I1: ConstraintQueryResponse.deterministic_match: bool|None=None + replay branch 返回; test_query_replay test 1 tightened
  - I2: conftest.py fixture scope="session"; test_metrics_endpoint 3/3 现在通过
  - I3: observability.py 用 request.scope["route"].path + url.path fallback
  - I4: test_query_replay autouse fixture 重置 _retriever/_audit_index/set_writer 在 test 前后
  - 验证: 222 unit + 36 integration = 258 测试 + 0 失败
- [x] Phase 5 minor cleanup (9cfb4c5..f8c53a4, 11 M-items + spec back-fill, 14 files)
  - M1: middleware endpoint_completed 移入 try-block,用真实 response.status_code (writer.write 仍带 status_code)
  - M2: @metered(operation=...) 在 except 计 METRICS.route_failures_total; 新 Counter rag_route_failures_total
  - M3: @audited 读 result.status_code (Response)
  - M4: 删除未用 safe_set (YAGNI)
  - M5: 缩短 _file_handler 注释
  - M6: 已有 try/finally + _reset_module_globals 还原,无需改
  - M7: 删 test_task_repo_phase45.py 未用 import
  - M8: conftest.py 补尾换行
  - M9: AuditIndex.build 包 try/except,失败设 None + warning,healthz 已检查 None-safe
  - M10: register_event_schema docstring "idempotent" → "sets; overwrites if already registered"
  - M11: test_audit_base 验证 schema 实际注册并触发 ValueError
  - + 多个 Python 文件补尾换行 + spec schema count 漂移回填 (§Audit 15 个事件, §Prometheus 12 spec + 2 内部)
  - 验证: 315 rag + 6 shared = 321 tests pass, 1 skipped, 0 failed; 78% coverage (无回退)
  - Self-verified inline M1/M2/M3/M9 (spot-checked critical diffs); 任务级 reviewer 未分派(Minor finding cleanup + Task 16 opus 已审批 baseline, 增量低风险)

## Phase 5.5 D Tasks (in progress)

- [x] T1: delete routes/metrics + test_metrics_endpoint (aa224fc, DONE_WITH_CONCERNS→Approved; main.py:19 import preserved per brief option b, T4 restores green)
- [x] T2: conftest multiproc collector cleanup (3ee5cd3, Approved with notes; brief said "12 lines" prose but code block has 15 lines, implementer followed the code block — minor doc drift in brief not in code)
- [x] T3: RED tests + free_port helper (4fdee39, Approved; spec ✅; raised brief-level concern: sync `with app.router.lifespan_context(app):` doesn't work because it's an async ctx mgr — addressed in T4)
- [x] T4: lifespan start_http_server + import cleanup (01fef7f, ❌ review → da63e9f fix → ✅ Approved)
  - Reviewer Critical: MultiProcessCollector(REGISTRY) duplicates metrics; multi-worker bind conflict
  - Reviewer Important: try/finally around teardown; test fixture can't activate multiproc
  - Reviewer Minor: _sync_lifespan exception semantics; free_port can hit 9090; logs before setup_logging
  - Fix commit addressed all 8 items:
    1. Fresh CollectorRegistry (no REGISTRY pollution)
    2. Bind-conflict try/except OSError → log warning + app.state.metrics_httpd=None
    3. try/finally wraps post-exporter startup so teardown runs on exceptions
    4. Test fixture removes PROMETHEUS_MULTIPROC_DIR (multiprocess validated via subprocess probe)
    5. METRICS_HOST default 0.0.0.0 (was 127.0.0.1, breaks docker-compose cross-container scrape)
    6. RuntimeError if PROMETHEUS_MULTIPROC_DIR missing at lifespan startup (MmapedValue import-time)
    7. Doc comment: PROMETHEUS_MULTIPROC_DIR must be wiped between restarts
    8. Dropped _sync_lifespan; tests use fastapi.testclient.TestClient (reuse fix from peer review)
  - Re-review Minor remaining:
    - M1: redundant mkdir after RuntimeError gate (stylistic)
    - M2: no trailing newline in test file (cosmetic)
    - M3: multiproc path has no automated CI test (out of scope, manual subprocess probe acceptable)
  - Verification: 3/3 targeted + 315 passed, 1 skipped full suite + runtime probes pass (default bind 0.0.0.0, port release, occupied-port survival, multiproc exposition without duplicates)

## Phase 5.5 F: Audit Rotation + /healthz Filter

- Status: DONE
- Tag: phase5.5-f-audit-rotation
- Commits: T1=c05a98c (handler), T2=8019f22 (AuditWriter), T3=56436e4 (skip_audit ContextVar), T4=1a30b8a (write honors skip), T5=a7e2acb (middleware), T6=532f060 (lifespan rollover callback), T7=3a582d9 (CLAUDE.md), T8=78afc13 (ledger), C1=c9e84c6 (hoist namer/rotator), C3=f2a64de (handler accumulation fix + missing C2/spec/plan)
- Tests: 346 passed, 1 skipped, 0 failed
- Trigger: audit.log = 1377 MB / 61122 events; endpoint_started/completed = 64.8% volume
- Solution: `RebuildingRotatingFileHandler` (gzip rotator, 100 MB × 5 backups) + `ContextVar` skip_audit on /healthz + on_rollover callback → `AuditIndex.build()`
- gstack-plan-eng-review findings resolved:
  - P2-1 (namer/rotator hoist): defaults set in `RebuildingRotatingFileHandler.__init__` (overridable via kwargs)
  - P2-2 (rollover → index rebuild test): `tests/integration/test_audit_rollover_rebuild.py` 3 tests
  - P3 (lazy `get_skip_audit` import): deferred
  - P3 (`_current_offset` defensive branches): deferred
- C3 fix-up: `AuditWriter.__init__` closes & removes prior `RebuildingRotatingFileHandler` from shared `ekrs.audit` singleton logger (was causing cross-test handler accumulation after C2 test additions)
- Known issues: none
## Phase 5.5 E: Retriever Depends Migration

- Status: DONE
- Tag: phase5.5-e-retriever-depends
- Commits: T1=df9443c, T2=e021721, T3=34e9fa3, T4=3ff7108, T5=e6fd663, T6=1b94401, T7=cfb58d9, + 3 fixup (PARSER_TOKEN env var, sanity test auth, ledger)
- Tests: 325 passed, 1 skipped, 0 failed
- Globals removed: _retriever, _audit_index, _pipeline, _lock, _repo + 5 setters (set_retriever/audit_index/pipeline/redis_lock/task_repo)
- Dep functions: get_retriever (strict 503), get_audit_index (optional None), get_pipeline/get_redis_lock/get_task_repo (strict 503)
- Migration pattern: app.dependency_overrides[get_X] = lambda: mock (tests); app.state.X = ... (main.py lifespan)
- gstack-plan-eng-review findings resolved:
  - P1 (MagicMock→State): adopted starlette State in contract tests (T1)
  - F3 P0 (4th test file folded in): test_ingestion migrated alongside test_ingestion_replay (T5)
  - F8 (main.py state writes): 8 app.state writes present; pipeline was only missing one (T7)
- Fixups during T8 verify:
  - test_ingestion.py client fixture sets PARSER_TOKEN env var (T3 moved auth from settings to env, breaking missing/invalid-token tests)
  - T3 sanity test sets PARSER_TOKEN="" so wrong X-Parser-Token header doesn't 403 before dep overrides resolve
- Known issues: none
