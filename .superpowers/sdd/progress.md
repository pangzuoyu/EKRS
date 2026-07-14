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
Task 1: complete (commits 163c18b..2c070c2, 1 commit, review clean — SPEC ✅ PASS, QUALITY APPROVED, 0 critical/important)

## Task 2: DocumentRepo (commit c439d50, 1 commit, review verdict REJECTED with deferred fixes)
- Concern 1 (T3 brief bug fix): acceptable, surgical, preserves intent
- Concern 2 (ir → notification.metadata adaptation): acceptable, no separate ir in scope
- Concern 3 (5 integration fixture patches): minimal, mirrors TaskRepo style
- Concern 4 (settings.DOCUMENTS_DB_PATH direct): correct D8 path
- Concern 5 (commit LOC 355 under 500 cap): OK
- Reviewer Important findings (deferred to follow-on tasks):
  * Q-1: document_metadata_failed not in _EVENT_SCHEMAS → Task 4 fix (memory noted)
  * Q-3: A1 path not in HTTP integration → Task 7 fix (will note in dispatch)
  * Q-8: try/except Exception + sync audit-write in async route → perf risk, NOT BLOCKING; record for final whole-branch review
- Minor findings for final review triage:
  * Q-2: T3 test couples documents + provision_overrides scope
  * Q-5: LIKE prefix lacks escape/separator
  * Q-6: first-writer-wins silently drops status updates
  * Q-10: get_document_repo raises RuntimeError → 500 vs 503
  * Q-11: get_document_repo dep unused in this task
  * Q-12: lifespan doesn't close DocumentRepo (matches TaskRepo pattern)

Task 2 complete (commits 2c070c2..c439d50, 1 commit, review accepted with deferred fixes per reviewer recommendation)

Task 3: complete (commits c439d50..638ece0, 1 commit, review clean — SPEC ✅ PASS, QUALITY APPROVED, 0 critical/important, 4 minor noted; 4 deviations all justified: rebuild→build (brief bug), hybrid setattr+setenv for auth.py os.environ, flat JSON shape, State direct-assignment vs monkeypatch.setattr)

Task 4: complete (commits 638ece0..6d8cb04, 1 commit, review clean — SPEC ✅ PASS, QUALITY APPROVED, 0 critical/important, 2 minor: dead-code `_ = _PHASE6A_OPTIONAL` reference + missing trailing newline on shared/ekrs_shared/audit.py). 3 deviations all justified: 15→16 events (document_metadata_failed orphan registered per memory note), test docstring update, defensive no-op whitelist reference. 5/5 new tests pass + 375 full suite (≥360 required, +29 vs Phase 5.5 F's 346 baseline). LOC delta 126 (≤500 cap)

Task 5: complete (commits 6d8cb04..c380fca, 1 commit, review clean — SPEC ✅ PASS, QUALITY APPROVED, 0 critical/important, 1 minor: 4KB lineage_snapshot truncation untested). 3 deviations all justified: solve_with_fallback as NEW method (not kwargs on V2 solve()) for V1/V2 architectural separation; /v1/constraints NOT modified (Task 5 scope = /calculate only, V2 multi-branch path has no soft-fallback concept); shared/ekrs_shared/models.py +21 LOC Pydantic v2 priority string validator (forward-compat for JSON callers). Implementer crashed on API 429 mid-task; controller took over (verified 11/11 tests pass + 386 full suite, wrote report, committed c380fca). 11/11 new tests pass + 386 full suite (≥374 required). LOC delta 421 (≤500 cap)

Task 6: complete (commits c380fca..63c7f8e, 1 commit, review clean — SPEC ✅ PASS, QUALITY APPROVED, 0 critical/important, 1 minor: missing trailing newlines on cases 26-29). Golden set 13→42 entries (29 new from golden.md). 168 pytest invocations pass (42 cases × 4 test classes). Controller dispatch error: golden.md has 29 TC-IDs not 25; amended implementer's commit to add 4 missing cases (RECALL/TRACE/REPLAY/UNIT-EDGE). CQ2 carve-out: 1100 LOC, JSON-only, 0 Python files modified.

Task 4 retro fix: complete (commit b4b45df, 1 commit, review clean — fixes Task 4 regression where `_EVENT_SCHEMAS` unioned `_PHASE6A_FIELDS` into required-field sets for `endpoint_started`/`endpoint_completed`, causing `ValueError` per request and non-JSON traceback noise in `audit.log` via `ekrs.audit.failures` child logger). Fix drops `| _PHASE6A_FIELDS` from 7 event schemas; shared audit base's defensive `_PHASE6A_OPTIONAL` whitelist already allows fields through as extras. Test `test_event_schemas_exclude_phase6a_fields_from_required_set` (renamed from include_…→exclude_…) rewritten to assert NONE of the schemas list the fields. 504 passed + 1 pre-existing flake (`test_ingestion_notify_persists_document_metadata` fails in full suite due to leak in `test_ingestion_phase4.py` `app.dependency_overrides[get_task_repo]` — see Task 7 review Important caveat) + 1 skipped. LOC delta +2.

Task 7: complete (commits 63c7f8e..1b4816e, 1 commit, review clean with non-blocking caveat — SPEC ✅ PASS, QUALITY APPROVED, 0 critical, 1 Important caveat, 2 minor). 3 e2e tests: (1) `/v1/calculate` 200 without Qdrant; (2) `audit.log` JSON contains `constraint_solved` with `lineage_snapshot` matching response + `conflict_details` key present; (3) A1 `POST /v1/ingestion/notify` with `metadata.doc_metadata` → lifespan-attached `app.state.document_repo.get(doc_id)` returns seeded `Document` with all 4 fields (memory note honored). 3/3 pass in isolation; full suite 504 passed + 1 failed (pre-existing test_ingestion_phase4 dependency_overrides leak) + 1 skipped. Important caveat: A1 test flaky in full integration directory due to `test_ingestion_phase4.py` line 84 setting `app.dependency_overrides[get_task_repo]` without teardown — recommend follow-up (a) defensive `app.dependency_overrides.clear()` in `e2e_client` fixture (1 line), (b) yield-finally cleanup in `test_ingestion_phase4` fixture. Minor: `httpx` could move to TYPE_CHECKING (negligible); `AuditLog pathlib` implicit conversion. Final review triage list.

Task 8: complete (commits 1b4816e..033a8a3, 7 commits, brief's ≤500 LOC cap forced splitting — D9 coverage 80→86.63%, CI gate installed). Coverage 80%→86.63% (gate ≥85%). Strategy: (a) `chore: remove unused context manager` (9062a5b, -74 LOC YAGNI orphan Phase 2b with no callers — session/context_manager.py at 0% was a red flag); (b) defensive `app.dependency_overrides.clear()` in `test_phase6_e2e.py::e2e_client` + yield-finally cleanup in `test_ingestion_phase4.py::client` (422e6cf, closes Task 7 reviewer's Important caveat about the pre-existing flake); (c) 7 unit test files across 3 commits (14d0824 embedder+retriever / fea7083 qdrant_client+admin_key / 5318a59 audit_phase6a_fields+audit_index+metrics, 469 LOC new tests, all ≤500 cap each); (d) `.github/workflows/test.yml` with `pytest --cov=ekrs_rag --cov-fail-under=85 -v` (3fe507b); (e) qdrant_client.py:182 `SearchParams(hnsw_ef=128)` API fix (033a8a3, qdrant-client 1.17.1 lacks HNSWParams — required for the new test_search_returns_payload_score_pairs test to pass). Full suite: 531 passed + 1 skipped + 0 failed (gate satisfied WITHOUT --deselect; flake closed). 4 deviations all justified: deletion chosen over tests for context_manager (YAGNI); single commit impossible (≤500 cap forced splits); SearchParams fix in scope (test prerequisite); no --deselect in CI yaml (flake fixed instead). Final review triage list (out-of-scope retrieval bugs): qdrant_client.py:185 `self._client.search(...)` was removed in qdrant-client 1.17.1 → use `query_points`; qdrant_client.py:41 `existing.vectors_config` → `existing.config.params.vectors`; upsert_chunks zero-vector Phase 1 dummy.

## Phase 6A Final Review Triage List

Discovered across Tasks 1-8, deferred to Task 11 whole-branch review:
- **Out-of-scope retrieval bugs** (Task 8): qdrant_client.py:185 `.search()` removed → use `query_points()`; qdrant_client.py:41 `vectors_config` → `config.params.vectors`; upsert_chunks zero-vectors (Phase 1 dummy)
- **Q-8 from Task 2 review**: try/except Exception + sync audit-write in async route — perf risk, not blocking
- **Q-2/Q-5/Q-6/Q-10/Q-11/Q-12** (Task 2 review minor): documents/provision_overrides test coupling, LIKE prefix lack escape, first-writer-wins silent drops, get_document_repo 500 vs 503, get_document_repo dep unused, lifespan doesn't close DocumentRepo
- **Task 4 review minor** (already noted): `_ = _PHASE6A_OPTIONAL` dead reference + missing trailing newline on shared/ekrs_shared/audit.py

## Phase 6A Complete

- Task 9 (Handbook sync): complete (commit 4f02fbd). Updated §6 phase plan with Phase 6A row, §9 golden count to 42, §16 audit from "15 events" to "16 events" with full inventory + lineage_snapshot/conflict_details whitelist note, §18 .env.example expanded + CI gate note.
- Task 10 (Tag): complete (tag `phase6a-spec-closure` created locally, HEAD = 4f02fbd). `git push origin phase6a-spec-closure` failed (no `origin` remote configured). Tag exists in local repo only.
- Total Phase 6A commits since spec base `fcf6f6a`: 22 (Task 8 split into 8 due to ≤500 LOC cap).
- Final test count: 531 passed + 1 skipped + 0 failed. Coverage: 86.63% (gate ≥85% satisfied).
- Task 11 (final whole-branch review) pending.
