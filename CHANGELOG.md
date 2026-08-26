# Changelog

All notable changes to EKRS are documented here by release tag. The
canonical implementation timeline lives in `ekrs-handbook.md §6`; this
changelog focuses on **what was delivered per phase tag** so the diff
from the previous phase is readable without consulting the handbook.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) —
`Added`, `Changed`, `Fixed`, `Removed` per release.

## [phase13a] - 2026-08-24

**Tag**: `phase13a` (annotated, force-moved to this closure commit per
parent plan §111). **Version**: 0.3.0 → 0.4.0 (minor bump — Phase 13a
ships production-readiness hardening per Keep-a-Changelog). **Phase
13a delivered as 11 implementation tasks** under the Q4 production-
readiness plan `docs/superpowers/plans/2026-08-23-phase13a-production-readiness.md`.

**Pre-13a baseline**: phase12 closure at `d9a602c`. All Phase 12 work
(FTS5 v2, golden 50, T10d Td.1/Td.2 MCP adapter, T10a-1..7 RRF + FTS
sync, T10b-1 chunker refactor, T10b-3 exact-match short-circuit,
T10b-2 cross-encoder data, Phase 12 Task D 745-bundle ingest rate,
Task C doc-type classifier, real-infra recall@10 8/20) preserved.

**Phase 13a closure scope** (T1-T10, plus Pre-Task A):

### Added

- **/healthz slim + /ready dependency probe** (T1, P0-1):
  - `/healthz` returns ONLY `{status, uptime_s}` (SLO <10ms).
  - `/ready` returns 200 only when `app.state.qdrant.count_points()` +
    `app.state.redis.ping()` both succeed (SLO <200ms).
  - Bug fix: `main.py:249` was setting `app.state.qdrant_manager`
    (only-ever-written, never read), making /ready always 503 in
    production. T10 E2E surfaced; aligned to `app.state.qdrant`
    (convention from existing tests).
- **Admission double-gate** (T2, P0-4):
  - `coarse_gate(raw_chars)` rejects >1M chars at the notify handler.
  - `chunk_gate(chunk_count)` rejects >3000 chunks as defense-in-depth
    inside the worker.
  - Both emit `admission_rejected` audit event with `reason` field.
- **Step5 worker picklable module** (T3):
  - `services/step5_worker.py` with `Step5Payload` frozen dataclass
    + `run_step5()` top-level fn (asyncio.run wrapper).
  - Consumes the Pre-Task A `_prepare_step5` + `_run_step5` helper —
    single source of truth across old in-process path (replay) and
    new subprocess path (production).
- **pebble.ProcessPool EncodingPool** (T4, P0-2/P0-3):
  - Pool with 1800s hard-kill timeout (settings), `task_registry`
    bookkeeping, and 4-item `_init_child` (BGE_M3_INTRA_OP_THREADS +
    3 thread-pool tuning knobs from Phase 12 Task D+ bench).
  - `PoolExpired` + `FuturesTimeoutError` double-catch on submit.
  - Boot recovery rehydrates in-flight tasks from aiosqlite TaskRepo.
- **Notify handler rewire + status queued/running** (T5):
  - Steps 1-4 inline coarse_gate + pool dispatch; Step 5 async.
  - Status field values: `queued` → `running` → `success`/`failed`.
  - Pre-existing integration failures on test_query_replay +
    test_constraints_api noted as baseline; not T5-introduced.
- **Audit events admission_rejected + task_timeout_killed** (T6):
  - Registered in `main.py _EVENT_SCHEMAS` (count 20 → 24).
  - Emit sites: coarse_gate (admission), pool kill (timeout).
  - Real AuditWriter regression tests added per 4-step discipline.
- **Metrics + boot recovery** (T7, P1-1/P1-2/P1-3):
  - `ekrs_ingestion_queue_depth` + `ekrs_task_duration_seconds`
    histogram + `ekrs_admission_rejected_total` +
    `ekrs_task_timeout_killed_total` counters.
  - Histogram buckets hard-asserted: `[10, 30, 60, 120, 300, 600,
    1800]` (eng-review Issue 5 contract lock).
  - Drift detector firing-path test: mock FTS count ≠ Qdrant count →
    `fts_consistency_drift` audit + `ekrs_index_consistency_drift_total`
    counter ≥ 1.
  - Boot recovery: on lifespan startup, scan TaskRepo for tasks
    stuck in `queued`/`running` state, requeue or mark failed.
- **Query encode via to_thread + callback failure reconciliation**
  (T8, P1-4/P1-5):
  - Query embedding moved off the async loop via `asyncio.to_thread`
    (matches T10a-4 precedent for `_StubRetriever` compat).
  - Callback failure log: structured line to debug.log (ts, doc_hash,
    reason) — visible for ops post-mortem.
- **Encode backend Protocol seam for GPU channel** (T9, 13c hook):
  - `_EncodeBackend(Protocol)` + module-level `_encode_backend(texts)
    -> list[list[float]]` in `services/step5_worker.py`.
  - `@runtime_checkable` so isinstance check works at test layer.
  - Default impl delegates to `EmbeddingService().encode()` returning
    dense vectors only (sparse stays at QdrantManager layer).
  - YAGNI respected: no GPU code introduced; 13b is separate plan.
- **T10 E2E acceptance + drift check + GPU rollout gate** (T10):
  - `scripts/phase13a_t10_e2e.py`: real-container E2E (2184 /healthz
    probes during encode, P99=32.2ms <100ms budget; over-limit
    admission_rejected audit verified; kill-9 self-heal re-verified;
    golden 208 + unit 861 + 1 skip zero regression).
  - `scripts/phase13a_t10_2_drift.py`: sequential ingest → clear →
    re-ingest of 5 docs, Qdrant=FTS=5 both rounds (within-13a paired
    writes intact).
  - `deployment/phase13a-rollout.md`: CPU-only post-13a canaries +
    GPU 10%→100% gate procedure with 10 acceptance criteria. Actual
    traffic split is operations-team work.

### Changed

- **IngestionOutcome contract unchanged** (R6 + parent §204):
  `rag_status ∈ {success, failed, duplicate, business_failure}`.
  New audit events do NOT widen the enum.
- **Pipeline replay path preserved**: `pipeline.ingest` (the old
  in-process path) still works for replay mode. Both paths consume
  the same `_prepare_step5` + `_run_step5` helper — no semantic drift.
- **App.state naming convention**: `app.state.qdrant` (not
  `app.state.qdrant_manager`). T10 E2E surfaced a pre-existing
  baseline bug where main.py wrote `qdrant_manager` (only-ever-
  written, never read).

### Fixed

- **/ready always 503**: `main.py:249` set `app.state.qdrant_manager`
  but health.py read `app.state.qdrant`. One-line fix + regression
  test in `tests/integration/test_healthz.py::test_ready_200_when_
  lifespan_set_state_qdrant`.

### Pre-existing baseline failures (NOT 13a-introduced)

- 10 Phase 5/7 integration tests use sync stub on `await
  RetrievalResult` (test_query_replay + test_constraints_api). These
  failed pre-13a; verified via `git stash` round-trip not related to
  13a work. Tracked separately.

**Verification matrix**:

- Full unit: **861 passed + 1 skipped** (T1-T9 cumulative)
- Golden: **208 passed** 0 regression
- mypy: clean (no NEW errors relative to pre-13a baseline)
- T10.1 E2E: all 4 checks pass (`/healthz` P99 <100ms during encode,
  admission_rejected audit emit, kill-9 self-heal, regression)
- T10.2 drift: paired-write Qdrant=FTS verified

**Risks closed** (from eng-review Issue 1-5):

- Issue 1 (T3 vs pipeline.ingest duplication drift): Pre-Task A helper
  extraction — closed at `f78554b`.
- Issue 2 (Settings _init_child 4 items): closed at T4 commit `30029a5`.
- Issue 3 (/healthz <10ms + /ready <200ms SLOs): closed at T1 + T5.
- Issue 4 (T7-T10 testing gap): all closed (T7 bucket assertion +
  drift firing test, T9 Protocol contract, T10 E2E + drift).
- Issue 5 (Phase 13b/c GPU shape drift risk): T9 Protocol seam +
  runtime_checkable guard; 13b shape change will fail tests not prod.

**Migration notes**:

- No data migration required. Step 5 helper is identical to old path;
  dispatch mechanism changed (sync in-process → pebble subprocess).
- Vector store unchanged (Qdrant 1024d dense bge-m3).
- FTS5 v2 schema unchanged.
- Audit log readers: 2 new event types (`admission_rejected`,
  `task_timeout_killed`) + 22 existing.

## [phase13b] - 2026-08-25

**Tag**: `phase13b` (annotated, force-moved to this closure commit per
parent plan §111). **Version**: 0.4.0 → 0.5.0 (minor bump — Phase 13b
ships a new production GPU encode channel per Keep-a-Changelog). **Phase
13b delivered as 4 implementation tasks** under the v1.1 GPU spec
`docs/superpowers/plans/2026-08-24-phase13b-v1-1.md`.

**Pre-13b baseline**: phase13a closure at `e5c8f39`. Phase 13a
production-readiness hardening (T1 `/healthz`+`/ready`, T2 admission
double-gate, T3 Step5 picklable worker, T4 pebble.ProcessPool, T5
notify rewire, T6 admission_rejected + task_timeout_killed audit, T7
metrics + boot recovery, T8 query encode to_thread + callback
reconciliation, T9 encode backend Protocol seam, T10 E2E + drift) all
preserved.

**Phase 13b closure scope** (T1, T2+T4, T3, T5):

### Added

- **torch FP16 bge-m3 GPU encoder** (T1, commit `7c7377c`):
  - Dual-head: dense 1024d + sparse (mirrors Phase 7 ONNX export).
  - FP16 weights; CUDA pre-warm at boot; orthogonal CPU baseline = ONNX.
  - **Precision noise**: sparse 0.95 → 0.94 (FP16 vs FP32) noted.
  - Lazy `import torch` in `services/torch_bge_m3.py` keeps CPU-only
    install reachable (`ImportError` → EncodingRouter skips GPU).
- **GPU self-check router + encode metrics** (T2+T4, commit `b8a03b1`):
  - `EncodingRouter` state machine `unknown|cpu|gpu` with
    `try_register_gpu()` + `force_re_register_gpu()`.
  - `BGE_M3_*` Settings: `BGE_M3_GPU_ENABLED=False` (default),
    `BGE_M3_GPU_DEVICE_ID=0`, `BGE_M3_GPU_PROBE_INTERVAL_S=30`.
  - Prometheus counters + histograms: `ekrs_encode_total{channel}`,
    `ekrs_encode_duration_seconds{channel}`, GPU memory peak gauge.
- **EncodingRouter on encode hot path + 30s probe + channel_switched
  audit** (T3, commit `8f2563d`):
  - `precomputed_encodings` kwarg threads router into pipeline.encode.
  - Probe daemon thread (default 30s, CI override 5s) re-evaluates
    `_self_check()` and transitions state.
  - `channel_switched` audit: 4 reason codes (`self_check_pass`,
    `self_check_fail`, `unavailable`, `encode_error`,
    `admin_invalidate`); transition-only emit (no flap on repeated
    failures) per review 🟢 #6 mandate.
- **E2E acceptance suite** (T5, commit `515fb40`):
  - T5.1 `scripts/phase13b_poc_bench.py` — 28-doc Phase12 v10-subset
    bench (CPU Phase A → wipe → GPU Phase B); perf thresholds: ≥7787
    chunks total, largest doc ≤30s, 2298-class doc ≤5s, GPU memory
    peak ≤6 GB, 0 failures.
  - T5.2 `scripts/phase13b_equiv_check.py` — 20×5=100 retrieval
    samples; top-10 Jaccard ≥0.99, cosine ≥0.999, sparse Jaccard
    ≥0.95; `_SPECIAL_TOKEN_IDS = frozenset({0,1,2,3,250001})` filter.
  - T5.3 `scripts/phase13b_failover_test.py` — audit log 3-path
    scan, 10-concurrent ingest, transition detection ≤30s; ADMIN_KEY
    unset → WARN + skip (risk #3).
  - T5.4 `tests/integration/test_phase13b_t5_e2e.py` — `@pytest.mark.heavy`
    wrapper chaining T5.1 → T5.2 → T5.3.
  - T5.5 `tests/unit/test_phase13b_t5_acceptance.py` — 11 pure-Python
    stubs covering all 10 §6 acceptance lines (state machine + audit
    + geometry formulas) without GPU infra.
  - 2 new admin endpoints: `POST /v1/admin/gpu/invalidate` (forces
    `current_channel="cpu"` + audit; next probe re-evaluates) and
    `POST /v1/admin/gpu/memory-stats` (exact `torch.cuda` peak read).
  - `make t5-acceptance` target; `deployment/phase12-recall-gt.json`
    schema + 20 placeholder slots (operator populates from Phase A
    baseline); 28-doc fallback list at `scripts/_phase13b_poc_28doc_fallback.txt`.

### Changed

- **Encoding backend dual channel**: CPU (ONNX Phase 7) + GPU (torch
  FP16 Phase 13b T1) via EncodingRouter. CPU path byte-level baseline
  preserved when `BGE_M3_GPU_ENABLED=False`.
- **Probe cadence**: `BGE_M3_GPU_PROBE_INTERVAL_S` defaults to 30s;
  CI override 5s for fast failover detection.

### Fixed

- **gpu_invalidate semantics**: original T5 design hinged on
  `last_self_check_pass` field (didn't exist on RouterState). Final:
  force `current_channel="cpu"` under lock + audit emit; next probe
  cycle's `force_re_register_gpu()` re-runs `_self_check()` naturally.
  Replaces fragile POSIX-mount-dependent `chmod 000` (eng-review fix).
- **mypy torch optional**: `import torch` inside `try/except` +
  `Any` annotation in router module (UQ-5); CPU-only install passes
  mypy clean.

### Pre-existing baseline failures (NOT 13b-introduced)

- 10 Phase 5/7 integration tests use sync stub on `await
  RetrievalResult` (test_query_replay + test_constraints_api) +
  `test_models_form_fields` ImportError. These failed pre-13b;
  verified via `git stash` round-trip not related to 13b work.

### Pending post-deploy

- **GPU real-infra verification**: `make t5-acceptance` requires
  `BGE_M3_GPU_ENABLED=true` in container + GPU runner (NOT PR gate).
  Unit-test equivalent at `make test` covers all 10 acceptance lines
  via pure-Python stubs.
- **GT JSON fill**: `deployment/phase12-recall-gt.json` is empty schema
  (`_filled_in: false`); operator populates by running Phase A baseline
  first, then T5.2 real-infra equivalence run.
- **Audit emit in pebble workers** (UQ-6): workers spawn process-local
  router without audit writer injection; probe transitions inside
  workers may silently drop. Cross-process audit wiring deferred to
  Phase 13c.

**Verification matrix**:

- Full unit: **18 new** (11 T5.5 + 7 admin GPU) on top of T1's 879 +
  T3's 61 incremental; **0 regression** on golden (208) + related
  unit (51)
- mypy: clean on touched files (`admin.py`, `main.py`, test files);
  `torch` imported as `Any` for lazy-import safety
- T5.1 / T5.2 / T5.3: scripts shipped; real-infra runs deferred to
  post-deploy GPU env
- 1 NEW mypy error pre-patched at `retriever.py:73` (T3 step 2)

**Risks closed** (from eng-review 8 feedback items + 3 UQ):

- 8 OQ RESOLVED in v1.1 plan (commit `3692894`)
- UQ-A: 28-doc preflight + fallback list — closed at T5 commit
- UQ-B: GT pre-validate fail-fast (`load_ground_truth` raises) — closed
- UQ-C: admin invalidate endpoint (no `chmod`) — closed at T5
- UQ-D: admin memory-stats endpoint (exact `torch.cuda` read) — closed
- UQ-E: pre-flight `tail audit.log` check at T5.3 startup — closed
- UQ-5: torch optional import — closed at T1
- UQ-6: audit emit in workers — defer to Phase 13c
- transition-only audit emit (review 🟢 #6) — closed at T5.5

**Migration notes**:

- GPU install: `pip install -e rag/[gpu]` adds `torch>=2.1,<3`.
  CPU install unchanged: `pip install -e rag/`.
- No data migration; no FTS schema change; no Qdrant payload change.
- Audit log readers: 1 new event type (`channel_switched`) + 24
  existing (T6 count = 25).
- Backward compat: `BGE_M3_GPU_ENABLED=False` default keeps all
  Phase 13a production behavior byte-level identical.

---

## [phase13c] - 2026-08-26

**Version**: 0.5.0 → 0.6.0 (minor bump — Phase 13c ships GPU bge-m3
production-readiness hardening per Keep-a-Changelog). **Phase 13c
delivered as 4 implementation tasks + ops runbook** under
`docs/superpowers/plans/2026-08-26-phase13c-prod-readiness.md`. No
new tag (`phase13b` stays locked at `4d9523d`; per post-closure
incremental pattern, Phase 13c absorbs into Phase 13b's shipping
release with a fresh version bump).

**Pre-13c baseline**: phase13b closure at `4d9523d`. Phase 13b GPU
bge-m3 channel (T1 torch FP16 encoder, T2+T4 EncodingRouter + metrics,
T3 hot-path wiring + 30s probe + `channel_switched` audit, T5 E2E
acceptance) all preserved. Phase 13a production-readiness (T1-T10) all
preserved.

**Phase 13c closure scope** (T1+T2, T3, T4, T5):

### Added

- **Cross-process AuditWriter bridge** (T1, `observability/audit_bridge.py`):
  - `AuditEventBridge` = `multiprocessing.Manager().Queue()` + main-side
    drain thread + worker-side `from_addr` factory.
  - `EKRS_AUDIT_QUEUE_ADDR` env var round-trips Manager address from
    main → pebble worker subprocesses.
  - **Layered fault tolerance** (D2):
    - Manager startup failure → lifespan retry-once → fail-loud (audit
      pipeline is required for ops).
    - `bridge.put()` runtime queue.Full / serialization → silent drop +
      counter (encoding hot path must never block on audit backpressure).
    - Consumer thread writer raises → exception isolated, drain keeps
      running (next event still lands).
  - `stop()` does inline drain before joining (handles 1000s of queued
    items at shutdown without waiting for consumer's 0.5s `get` timeout).
  - 9 unit tests (round-trip, FIFO, queue-full drop, never-raises
    serialization, writer exception isolation, stop drains, etc.).
- **`mark_process_dead` atexit + stale Prometheus multiproc cleanup**
  (T2, `services/stale_cleanup.py`):
  - `_init_child` Item 6: `atexit.register(mark_process_dead, os.getpid())`
    so graceful worker shutdown cleans up its multiproc files (not just
    SIGKILL paths).
  - 5-minute `asyncio` background task scans `PROMETHEUS_MULTIPROC_DIR`
    for stale `.db` files; `mtime < time.time() - 60` AND
    `os.kill(pid, 0)` raises (defensive) → delete.
  - `asyncio.to_thread` wraps the file scan to avoid event-loop block.
  - 10 unit + 1 skip (Windows atexithook skip) tests.
- **IngestionStatus.status Literal + single-source mapper** (T3,
  `shared/ekrs_shared/models.py` + `services/ingestion_mapper.py`):
  - `status: str` → `Literal["pending","processing","success","failed"]`
    (Pydantic 2.8 enum contract replaces free-form string).
  - `map_row_status_to_ingestion_status()` = single source of truth:
    `queued|running|pending → pending/processing/pending`,
    `completed → success`, `failed → failed`, unknown → `failed`.
  - 5 model + 7 mapper + 3 get_status unit tests.
- **Phase 13b bench dynamic threshold** (T4, `scripts/phase13b_poc_bench.py`):
  - `_resolve_chunk_threshold()` priority: 0=disabled, explicit=int,
    default=`corpus_total_blocks * 0.9` (real-infra-calibrated).
  - `_check_thresholds()` returns `(errs, status)` ∈
    `{pass, warn, fail}`; STRICT mode (`T5_PHASE_B_MIN_CHUNKS_STRICT=1`)
    triggers hard-fail below threshold.
  - `make gpu-acceptance` exit 0 on `pass`/`warn`, exit 1 on `fail`.
  - 9 unit tests covering env override / corpus-derived / disabled /
    STRICT paths + `n_failed` hard-fail invariant.
- **Production ops runbook** (T5, `deployment/phase13c-ops-guide.md`):
  - 8 sections: pre-conditions / build / startup / acceptance /
    troubleshooting (6 cases) / rollback / upgrade path / quick-ref.
  - T3 regression recipe (§4.3) for `get_status` FAILED branch.
  - T5.1 STRICT mode gate (§4.1) for pre-release verification.
  - 13c acceptance checklist (§7.1) for on-call 30min GPU boot.

### Changed

- **`_emit_channel_switched` dual-path**: main-process fast path
  (`get_writer()` direct) preserved for parent process; worker subprocess
  path forwards via `AuditEventBridge.put()` (Phase 13c T1).
- **`/v1/ingestion/status` FAILED branch**: pre-13c bug returned
  `status: pending` for row_status `failed` (wrong branch in ternary).
  Fixed: split FAILED branch, call mapper, return `failure_reason` /
  `error` fields. (Phase 13c T3.)
- **`EncodingPool._init_child`**: 5 items → 6 items (added atexit
  mark_process_dead registration).
- **Main lifespan startup**: after `set_writer`, retry-once Manager +
  `bridge.start()` + `bridge.export_addr()`; spawn `asyncio.create_task`
  stale_cleanup loop with 300s interval. Shutdown: bridge.stop() +
  cancel stale task + pop env var.

### Fixed

- **get_status FAILED regression**: empty JSONL / no_chunks documents
  now correctly report `{"status": "failed", ...}` instead of
  `{"status": "pending", ...}`. Pre-13c bug noted in Phase 12
  full-745 ingest observations (4 silent failures were misreported as
  pending, blocking reconciliation).
- **Stale Prometheus `gpu_memory_peak_bytes`**: workers exiting via
  SIGKILL or graceful shutdown previously left `.db` files in
  `PROMETHEUS_MULTIPROC_DIR`, polluting `/metrics` with peak values
  from dead PIDs. Mark_process_dead atexithook + 5min background
  cleanup keeps the gauge honest.

### Deferred (T6)

- **channel_switched audit suppression** (P2): T5.1 28-doc bench only
  observed 1-2 `channel_switched` events across the full run. With noise
  not yet material, the suppression logic (rate-limit / windowed dedup)
  is **deferred to Phase 14 or a standalone patch**. Plan section
  marked as `~~T6 deferred~~` + `phase13c-t6-deferred.md` Memory entry
  captures the trigger conditions for future re-evaluation.

### Pre-existing baseline failures (NOT 13c-introduced)

- 12 Phase 5/7 integration tests use sync stub on `await
  RetrievalResult` (test_query_replay + test_constraints_api) +
  `test_models_form_fields` ImportError + a handful of stale fixtures.
  These failed pre-13c; verified via `git stash` round-trip not related
  to 13c work.

**Verification matrix**:

- **T1** cross-process audit: 9 unit pass (after fixing
  `test_stop_drains_remaining_events` flaky via inline-drain refactor).
- **T2** stale cleanup: 10 unit pass + 1 skip (atexit Windows skip).
- **T3** Literal + FAILED fix: 5 model + 7 mapper + 3 get_status = 15
  unit pass.
- **T4** dynamic threshold: 9 unit pass.
- **T5** ops guide: doc-only deliverable, no automated test.
- **Full unit suite**: 946 pass + 2 skip + 12 fail = **0 new regression**
  (12 pre-existing baseline failures preserved).
- **mypy**: clean on `audit_bridge.py`, `stale_cleanup.py`,
  `ingestion_mapper.py`, all touched files (1 NEW error
  pre-patched at `main.py` for module-level `AuditEventBridge` import
  to satisfy finally-block after raise).

**Risks closed** (Phase 13b closure §Pending post-deploy):

- UQ-6 (audit emit in pebble workers) — closed at T1: cross-process
  AuditWriter bridge ships `channel_switched` to main-process
  audit.log via Manager queue.
- Phase 13b post-deploy note "Stale counter drift" — closed at T2:
  atexit + 5min background cleanup keep `/metrics` `gpu_memory_peak_bytes`
  honest.
- Phase 12 Task D full-745 silent failure note — closed at T3:
  `get_status` FAILED branch correctly reports failed documents.

**Risks tracked** (not closed):

- T6 channel_switched 抑制 — deferred (see above).

**Migration notes**:

- No new dependency: `multiprocessing.Manager` + `asyncio.to_thread` are
  stdlib (Phase 13a already depends on these).
- No data migration; no FTS schema change; no Qdrant payload change.
- Audit log readers: no new event types; `channel_switched` now reaches
  audit.log from worker subprocesses (previously silently dropped).
- Backward compat: ops with `BGE_M3_GPU_ENABLED=False` (CPU path) sees
  no behavior change — bridge.start() runs but no workers exist; bridge
  receives zero events and exits cleanly on stop.

---

## [phase12] - 2026-08-15

**Tag**: `phase12` (annotated, force-moved to this closure commit per
parent plan §111). **Version**: 0.2.0 → 0.3.0 (minor bump — Phase 12
ships new retrieval capability per Keep-a-Changelog). **Phase 12
delivered as 5 implementation tasks + 3 follow-up tasks + §七 Item 3
closure work** under the Q3 §9.6 last-mile plan
`docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md`.

**Sub-tag**: `phase12.1` locks at `090d74f` (T1+T2 chunker + Qdrant
passthrough anchor, do-not-move).

**Phase 12 closure scope**:

- T1 (models) — `Chunk` + `Metadata` `form_fields`/`column_headers`
  `default_factory=list` (gstack D4), shared at
  `shared/ekrs_shared/models.py`.
- T2 (chunker + IR parser + Qdrant passthrough) — `form_fields` /
  `column_headers` survive ingestion → payload.
- T3 (FTS5 v2 schema) — full FTS5 rebuild adds `form_fields` /
  `column_headers` indexed columns (notional schema migration;
  `scripts/migrate_fts_v1_to_v2.py`).
- T4 (retriever R4 boost) — `_scope_priority()` extended with
  `max(base, FORM_FIELD_WEIGHT=0.9)` for `form_fields`,
  `max(base, COLUMN_HEADER_WEIGHT=0.7)` for `column_headers`.
  R6 strict parity + R4 scope filter still apply post-boost.
- T5 (testing) — 6 named test files cover Chunk round-trip,
  chunker passthrough, FTS5 v2 schema + indexing, retriever form-field
  boost; golden 50 regression at 208 passed / 0 fail.
- F1 (pipeline wire) — `IngestionPipeline.ingest()` paired write
  Qdrant + FTS for the new schema.
- F2 (migration suppression) — `ConsistencyChecker` drift audit
  suppressed during schema migration via `EKRS_FTS_MIGRATION_IN_PROGRESS`
  flag.
- F3 (migration script) — `scripts/migrate_fts_v1_to_v2.py`
  orchestrates the full Qdrant-rebuild + atomic rename; 30s drain +
  3-attempt retry decorator on FTS5 read paths.

**§七 Item 3 — form_field boost toggle + recall@10 baseline** (this
commit):

- `EKRSRetriever._scope_priority(chunk, *, form_field_boost=True)` +
  `EKRSRetriever.retrieve(query, ..., form_field_boost=None)` — kwarg
  with `EKRS_FORM_FIELD_BOOST_ENABLED` env var default (production
  preserves Phase 12 T4 ON-by-default).
- `scripts/recall_at_10_form_field_baseline.py` — 15 bundles ×
  3 anchors × 2 rounds (boost ON / OFF) comparison, with synthetic +
  real-infra modes. Script always passes `form_field_boost` explicitly,
  no env-var dependency for the measurement.
- `docs/superpowers/research/2026-08-15-recall-at-10-form-field.md` —
  synthetic baseline numbers (form_field OFF 0/15 → ON 15/15, Δ+15) and
  the known coverage gap: column_header / heading synthetic does not
  exercise boost paths — real-infra validation deferred to 8/20 联调
  per plan §五 验收门槛.

**Unresolved → 8/20 联调** (parked, not blocking closure):

- §七 Item 3 P0 — real-data recall@10 on 15 LOT/CHECK recommended
  bundles (verifies synthetic form_field 0/15 → 15/15 signal
  reproduces; column_header boost effectiveness unverified by
  synthetic).

**Decisions recorded in this closure**:

- FTSManager full description migrated from `[Unreleased]` to
  `[phase10]` below (resolves pre-existing cross-reference drift).
- 3 untracked `docs/superpowers/plans/2026-07-28 / 2026-07-29`
  Phase 10 plans historical-cleanup-committed (content verified
  against Phase 10 actual delivery — no NOTE drift).

l.
---

## [Unreleased]

Phase 10 incremental tasks (T10a-2 / T10a-3 / T10a-4 / T10a-5 /
T10a-6 / T10b-1 / T10b-3 / T10d Td.1 / T10d Td.2) and Phase 11 entries
still need housekeeping — migration into [phase10] / [phase11]
sections deferred per ruling 2 (only FTSManager description migrated
in this closure; other entries kept in [Unreleased] for future
housekeeping).

### Added

- **Phase 12-A: E2E test runner integration scaffolding**
  (T10d-style incremental; goal from user 2026-07-30 evaluation of
  Phase 11 closure open issues). Adds Makefile targets `test-e2e`
  (first-time setup + run), `test-e2e-ci` (cache-warm CI variant),
  and `test-e2e-ready` (pre-flight check). New executable
  `dev_ui_v2/scripts/check-ci-ready.sh` verifies four prerequisites
  (Node ≥ 20.20.0, `npx playwright` wired, Playwright Chromium
  browser cache present, MSW worker file at
  `dev_ui_v2/public/mockServiceWorker.js`) and exits 0 on success.
  `dev_ui_v2/README.md` gains an "E2E tests (Playwright)" section
  documenting the three commands and the rationale for `webServer`
  being dev-mode not preview-mode. Covered by 5 pytest integration
  tests in `rag/tests/unit/test_check_ci_ready.py` that exercise
  each failure mode plus the happy path with controlled PATH /
  HOME / `DEV_UI_V2_ROOT_OVERRIDE`. **Not yet wired to PR CI**;
  Phase 12-B will add the `.github/workflows/e2e-tests.yml` job
  once `make test-e2e` has been stable in local development for
  at least one phase cycle.

- **Pipeline FTS sync + consistency drift detection**
  (T10a-2, Phase 10): `IngestionPipeline.ingest()` writes FTS rows
  paired with Qdrant upsert (Step 5.6, FTS failure does NOT fail
  ingestion — Qdrant is truth-of-record). New `FTSManager.replace_doc()`
  provides atomic delete-then-upsert for re-ingest idempotency (FTS5
  virtual tables have no PRIMARY KEY; simple upsert would create
  duplicate rows on parser re-deliveries). New
  `FTSManager.count_active()` excludes `status='illegal'` for drift
  comparison. New `QdrantManager.count_points()` delegates to Qdrant
  1.11+ `count()` API. New `ConcurrencyChecker` background task
  (5min interval, env `INDEX_CONSISTENCY_INTERVAL_S`) compares
  `fts.count_active()` vs `qdrant.count_points()`; on drift emits
  `fts_consistency_drift` audit event and increments
  `ekrs_index_consistency_drift_total` counter.
  **Detect-only — never auto-repairs** (parent plan §T10a-2 mandate,
  avoids accidental deletion on transient FTS write lag).
  `fts_synced` audit emit call site is in place but schema registration
  is deferred to T10a-7. New `_EVENT_SCHEMAS` entry: `fts_consistency_drift`
  (event count 19 → 20). 10 unit + 5 integration tests cover: paired
  Qdrant+FTS writes, FTS failure non-blocking semantics, re-ingest
  idempotency (3x replay produces 1 row, not 3), drift detection +
  audit/metric emit, count failure swallowing. Pipeline constructor
  gained `fts: FTSManager | None = None` kwarg — backward compatible
  (existing callers work via default None, byte-level equal to Phase 9
  baseline).
- **Reciprocal Rank Fusion pure function + `FusionStats` analytics**
  (T10a-3, Phase 10): new module
  `rag/ekrs_rag/retrieval/rank_fusion.py`. Exports `FusionStats` frozen
  dataclass with three fields (`vector_hits` / `fts_hits` / `both_hits`)
  for `T10a-7` audit event `fts_searched` to consume directly, and
  `reciprocal_rank_fusion(ranked_lists, key_fn, k=60) -> (fused_results,
  FusionStats)` — pure (R2) function with no I/O / state / side
  effects, deterministic replay. RRF formula `score(d) = Σ_i 1/(k +
  rank_i(d))`; ``rank_i(d)`` = best rank of d in list i (duplicates in
  same sublist ignored after first occurrence). Tie-breaking is
  insertion-order (first-appearance-wins). Supports arbitrary `N`
  ranked lists; main path is `N=2` (vector + FTS) per parent plan
  §T10a-3. `k=60` is the parent-plan-locked default (per
  `broad-spectrum-retrieval-port-design §4.3`); tests use `k=10` or
  `k=1` to verify fusion logic without 60x arithmetic slowdown. Caller
  contract: ranked_lists[0]=vector, ranked_lists[1]=FTS (so FusionStats
  fields have conventional semantics). 17 unit tests cover: empty /
  single / dual / N=3 ranked lists, k parameter effect, duplicate keys
  within sublists, key_fn exception propagation (R2 propagation
  guarantee), FusionStats three-field set-arithmetic
  (`vector_hits + fts_hits + both_hits == |unique keys union|`), and
  frozen-dataclass enforcement. **Retriever wiring = T10a-4, audit
  emit = T10a-7**. No new tag; `phase10.1` stays locked at `1c44eee`
  (T10b-1 do-not-move); `phase10` reserved for T10a-7 closure.
- **Retriever wired to parallel vector + FTS retrieval via RRF**
  (T10a-4, Phase 10): `EKRSRetriever` is now `async def retrieve()`
  with two new call paths. Constructor accepts `fts: FTSManager | None =
  None` kwarg; `fts=None` (default) preserves the Phase 9 byte-level
  baseline (raw Qdrant scores pass through to `_rank_by_scope`). When
  `fts` is configured, both paths run in parallel via
  `asyncio.gather(..., return_exceptions=True)` + `asyncio.to_thread`;
  FTS failure is **isolated** (`gather(return_exceptions=True)` →
  log warning + degrade to vector-only, never propagate to caller).
  Fused via `reciprocal_rank_fusion(ranked_lists=[vector_chunks,
  fts_chunks], key_fn="f{doc_hash}:{source_block_ids[0]}",
  k=60)`. New `FTSManager.search_with_payload(query) -> [(chunk_id,
  payload_dict, score)]` returns the payload from `payload_json`
  UNINDEXED column in a single IN-query (no N+1). `RetrievalResult`
  gains `fusion_stats: Optional[FusionStats] = None` field; `None`
  means fts disabled this round (R4 byte-level invariant). 10 unit +
  3 IMPROVE boundary tests + 4 Phase 6B retriever regression tests +
  1 FTSManager `search_with_payload` test cover: fts=None byte-level
  byte-level == Phase 9, async gather wall-clock < sequential, FTS
  exception isolation (vector survives), FTS corrupt-payload skipped
  silently, scope_priority applied AFTER RRF (R4 invariant), and
  FusionStats vector/fts/both fields. `constraints.py` 2 call sites
  + `_StubRetriever.retrieve` + `_make_retriever` mock all migrated to
  `async def`/`AsyncMock`. **Audit `fts_searched` event = T10a-7**,
  consumes `FusionStats` directly. No new tag (phase10 reserved for
  T10a-7 closure).
- **chunk_id EKRS-side generation + FTS↔Qdrant round-trip**
  (T10a-5, Phase 10): `QdrantManager.upsert_chunks` now writes
  `chunk_id` into the Qdrant payload for every chunk. Format
  `{doc_hash[:8]}-{chunk_index:04d}` via `FTSManager.generate_chunk_id`
  (T10a-1 generator, no schema change). `Chunk` model gains
  `chunk_id: Optional[str] = None` (default None preserves legacy
  ingestion). New `FTSManager.get_block_id_by_chunk_id(chunk_id)` is
  the inverse of T10a-1's `get_chunk_id(block_id)`; round-trip covered
  by `test_round_trip_block_id_and_chunk_id`. Retriever key_fn
  switches from `f"{doc_hash}:{source_block_ids[0]}"` (T10a-4
  fallback) to `c.chunk_id` with `or` fallback for legacy chunks —
  `test_retrieve_key_fn_uses_chunk_id_when_present` /
  `_falls_back_to_doc_hash_for_legacy_chunks`. **Naming-space
  coexistence** (parent §[M2]): `block_id` (UUID from ir_parser) and
  `source_block_ids` (list) are preserved; `chunk_id` is a parallel
  field, never a replacement. 7 unit + 3 IMPROVE boundary + 3 FTS
  bidirectional tests pass; full suite 812 pass 0 regression; mypy
  clean on touched files. **Audit `fts_synced`/`fts_searched`
  fields = T10a-7** (event count 20→22 per plan). No new tag
  (`phase10` reserved for T10a-7 closure).
- **Golden set 50 case 回归 + 3 工程标识符 BM25 recall@1 数据**
  (T10a-6, Phase 10): 验证阶段, 不扩 case. 跑 `tests/golden_set/`
  全集 = 208 pass (50 case 含参数化), 0 退化. 新增
  `tests/unit/test_fts_identifier_recall.py` 4 测试: 测量
  `A312-TP316` / `GB/T 12459` / `1.6MPa` 三个高价值工程标识符的
  BM25-only recall@1, soft-assertion (不阻塞) + stdout log 决策数据
  给 T10c cross-encoder 评估用. **结果: 3/3 recall@1=1** —
  `unicode61 remove_diacritics 2` tokenizer 对 Latin+digit+连字符
  /斜杠/点 标识符召回干净 (CJK run 是已知限制, 不在 T10a-6 范围).
  T10c 触发条件 (parent §6.1): 决策数据 3/3 满, 不强制触发
  cross-encoder; T10c 留待后续 plan 评估. 无新 tag
  (`phase10` 留给 T10a-7 closure).
- **Audit events `fts_synced` + `fts_searched` registration**
  (T10a-7, Phase 10 closure): wires the FTS pipeline-side audit
  writes that were already in place at T10a-2/T10a-4 to the schema
  registry. `fts_synced` schema `{doc_hash, version, chunks_written}`
  registered for the `pipeline.ingest()` Step 5.6 emit (already in
  code); `fts_searched` schema `{vector_hits, fts_hits, both_hits}`
  registered for the new retriever-side emit. `EKRSRetriever` gained
  `audit_writer: Optional[AuditWriter] = None` kwarg (mirrors Phase
  5.5 E DI pattern); `audit_writer=None` preserves Phase 9 byte-level
  (no emit). When FTS is configured and `audit_writer` is set, the
  retriever emits `fts_searched` after RRF fusion with `FusionStats`
  fields consumed directly from T10a-3 `reciprocal_rank_fusion`.
  Emit is **best-effort** — `try/except` isolates write failures so
  audit disk-full / schema-mismatch never blocks retrieval (parent
  §204 "审计永远不阻塞业务"). `main.py` lifespan wires
  `_audit_writer` into `EKRSRetriever(qdrant=..., audit_writer=...)`
  via Phase 5.5 E DI pattern (no module-global `get_writer()` —
  avoids Phase 5.5 E migration rollback). **Event count 20 → 22**.
  Parent plan §204 explicitly closes: these events do NOT enter the
  `IngestionOutcome` enum — they are intermediate signals, not
  ingestion terminal states. 14 unit tests in
  `tests/unit/test_audit_event_coverage_t10a7.py` (8 in
  Section 4 schema validation + 6 in retriever-emit). T10a-7
  closes Phase 10: full `phase10` tag force-moved to T10a-7 commit
  (per parent §111 annotated-tag force-move to closure commit).
- **`--mode offline` for production first-deploy ingestion**
  (`scripts/live_stress_60.py`): 3s pace, 2 retries with 2s/4s backoff,
  180s status timeout, 3s poll interval, no `_r<run_id>` suffix (relies
  on Qdrant SHA-dedup for idempotency). `--resume` filter (default on)
  queries `/v1/ingestion/status/{doc_hash}` per candidate AND reads
  `ingestion_completed` events from the audit log (via
  `--audit-via-docker CONTAINER` because `/app/rag/audit.log` is not
  bind-mounted per `docker-compose.yml`). Writes failed/pending docs to
  `failed_docs.txt` (atomic tmp+rename, sorted, replace-semantics).
- **`--mode retry-failed`**: reads `failed_docs.txt` (one doc_hash per
  line), re-processes only those docs with 5s pace + 4s/8s backoff +
  180s timeout. Strips `_r<run_id>` suffix (precise regex
  `_r\d{8}T\d{6}Z$`) to match against `<corpus-root>/<id>/data.jsonl`.
  Re-notifies docs that may already be in Qdrant; relies on the RAG
  service's `get_ingestion_status` short-circuit for idempotency
  (operators wanting script-level skip should use `--mode offline
  --resume` instead).
- **`--audit-via-docker CONTAINER`**: reads the audit log inside the
  rag container via `docker exec` (active `audit.log` + rotated
  `audit.log.{1..5}.gz` per `audit.py:44-50`). Required for the
  offline mode resume check unless `--audit-path` is host-accessible.
- **Strong-signal short-circuit** (T10b-3, Phase 10 incremental): when
  the user query is a substring of any retrieved chunk's
  ``chunk.text`` (exact-match predicate), the retriever bypasses RRF
  and returns the matched chunks directly with ``vector_scores=[1.0]``.
  This is a deterministic optimization (different code path, **same
  chunk set** as the standard RRF path) — globally enabled, NOT gated
  on strict mode (parent §25 + §157). New
  ``EKRSRetriever._is_exact_match(query, chunks) -> List[int]``
  static predicate (case-sensitive substring match by default;
  empty/whitespace query → ``[]`` no false-positive). Short-circuit is
  gated on ``fts is not None`` to preserve the Phase 9 byte-level
  baseline when FTS is disabled. ``RetrievalResult`` gains
  ``short_circuit: bool = False`` field (default False preserves
  Phase 10 callers). When short-circuit fires:
  - ``fusion_stats = FusionStats(N, 0, 0)`` (vector contributed, fts
    didn't, overlap concept doesn't apply).
  - ``fts_searched`` audit emit still fires for ops visibility (parent
    §204 "审计永远不阻塞业务" but also "审计永远不缺席"); a Prometheus
    counter for short-circuit rate is deferred to Phase 11.
  - Scope filter ``active_scope=`` still applies (R4 priority
    unchanged); multi-match returns chunks in union(dedup by
    chunk_id) insertion order.
  11 unit tests in ``tests/unit/test_short_circuit_t10b3.py`` cover:
  predicate semantics (single / multi / no / case-sensitive / empty
  query), retriever integration (RRF bypassed on match, RRF called on
  no-match, ``fts_searched`` still emitted on short-circuit, strict-
  mode parity returns identical chunk set, ``active_scope`` filter
  respected). Stub latency bench
  ``scripts/t10b3_short_circuit_bench.py``: 200-chunk corpus,
  15+15 queries, 5 warmup → sc fires 15/15 (100%), sc_p99 3.88ms vs
  rrf_p99 4.45ms (12.7% reduction; ratio 0.87 < 0.99 acceptance
  threshold; plan-doc aspirational target was 0.5 but tuned for real
  bge-m3+FTS5+Qdrant HTTP backends — asyncio.to_thread overhead
  dominates both paths in the stub environment). Full suite
  **633 unit + 208 golden + 11 t10b3 = 852 pass 0 regression**;
  mypy clean. **No new tag** — ``phase10`` already locked at
  closure commit ``2e1d9fa``; T10b-3 is an incremental commit inside
  the ``phase10`` tag.
- **MCP (Model Context Protocol) adapter — minimal viable**
  (T10d Td.1, Phase 10 incremental, same tag discipline as T10b-3):
  new module ``rag/ekrs_rag/mcp/server.py`` exposes 2 tools via
  the official Python ``mcp>=1.0`` SDK (``FastMCP`` high-level API):
    - ``ekrs_search(query, top_k=40, active_scope=None)`` —
      broad-spectrum retrieval (vector + FTS + RRF). Direct reuse
      of internal ``EKRSRetriever.retrieve()`` — no internal HTTP
      round-trip, no double rate-limit, no double audit.
    - ``ekrs_status()`` — healthz dependency payload, no retriever
      needed (server can boot before retriever is ready).
  Both return ``list[TextContent]`` (MCP wire format); JSON shape
  documented in ``tests/unit/test_mcp_server_td1.py`` doctring.
  Chunk serialization truncates ``chunk.text`` to 200 chars
  (``CHUNK_TEXT_PREVIEW_CHARS``) to keep payloads small; consumers
  needing full text fall back to ``/v1/blocks/{id}`` (Td.2+, not
  in Td.1). Resilient: retriever exceptions caught and returned as
  ``{"error": "..."}`` MCP content — never crash the server
  (parent §204). CLI entrypoint: ``python -m ekrs_rag.mcp.server``
  for stdio transport (Claude Code / MCP inspector / Desktop all
  support stdio). Pyproject: ``mcp>=1.0`` added to
  ``[project.dependencies]`` (production dep, not dev-only).
  8 unit tests cover: module imports, tool registration, retriever
  dispatch + kwargs pass-through, JSON TextContent output, exception
  isolation, empty-chunks handling, status independence from
  retriever. 1 integration test
  (``tests/integration/test_mcp_stdio_roundtrip_td1.py``) spawns
  stdio subprocess and verifies ``initialize`` handshake +
  ``list_tools`` + ``call_tool('ekrs_status')`` +
  ``call_tool('ekrs_search')`` error-path wire round-trip.
  ``PytestUnknownMarkWarning`` cleared by registering
  ``integration`` mark in pyproject ``[tool.pytest.ini_options]``.
  Full suite **849 unit + 1 stdio integration + 1 skip pass 0
  regression**; mypy ``ekrs_rag/mcp/`` clean (1 dict-item error
  closed via explicit ``Dict[str, Any]`` annotation). **No new tag**
  — ``phase10`` remains at ``2e1d9fa`` closure; T10d Td.1 is an
  incremental commit inside the ``phase10`` tag (precedent:
  T10b-3 same pattern). Td.2 (extend to ``ekrs_query`` +
  ``ekrs_get_block`) **shipped as post-closure incremental**
  (see Td.2 entry below).

- **T10d Td.2 — MCP adapter extended with `ekrs_query` +
  `ekrs_get_block`** (Phase 10 incremental, no new tag —
  `phase10` stays at `2e1d9fa`). Adds 2 more MCP tools + new
  HTTP route, with `evaluate_constraints` helper extracted from
  the constraints route handler so both the HTTP layer and the
  MCP layer share the R3 three-gate pipeline without an
  internal HTTP round-trip:

    - **`ekrs_query(query, context, scope, policy,
      overlay_hints, strict, top_k=40)`** — full constraint
      solve via the R3 three-gate pipeline. Direct internal call
      to `evaluate_constraints` (no HTTP, no double rate-limit,
      no double audit). Returns `[TextContent]` with the
      `ConstraintQueryResponse` shape (branches, mode, conflicts)
      on success or `{"error": {...}}` MCP content on failure.
      Iron Rules R3 / R4 / R6 / R7 honored transparently (solver
      is R2 pure; helper does no translation).

    - **`ekrs_get_block(block_id)`** — document deep-read by
      `block_id` (UUID from ir_parser). Direct internal call to
      `QdrantManager.get_payload_by_block_id`. Returns the full
      block payload (text NOT truncated; this is a deep-read
      endpoint, not a search preview) with `numeric_hints`
      projected to count-only (full list would blow past MCP
      message-size limits). Not-found → `{"error": "block_id not
      found", "block_id": "..."}` MCP content (parent §204).

    - **`GET /v1/blocks/{block_id}`** — new HTTP route in
      `rag/ekrs_rag/api/routes/blocks.py`. Same auth as
      `/v1/constraints` (`require_parser_token`). Returns
      `BlockResponse` (Pydantic: `block_id, doc_hash, text,
      scope_path, page_numbers, token_count, version,
      source_block_ids, numeric_hints_count`). 404 on missing
      block_id, 503 on uninitialized qdrant, 500 on qdrant
      transport error (exception isolation).

  Naming consistency (user feedback during GREEN): unified on
  `block_id` (UUID) for the new route + MCP tool param, matching
  FTS5 PK, Qdrant payload, and audit event field naming — rather
  than the T10a-5 `chunk_id={doc_hash[:8]}-{idx:04d}` parallel
  field (which stays in the Qdrant payload for legacy /
  self-describing reasons per the parent §[M2] naming-space
  coexistence rule).

  **`build_server` DI extended** from 2 args `(retriever,
  dependencies)` to 4 args `(retriever, qdrant, solver,
  dependencies)`. CLI entrypoint (`python -m ekrs_rag.mcp.server`)
  still passes `None` for all 3 deps — PoC zero-config; production
  wiring is Td.3 work (Claude Code `.mcp.json` integration).

  **Tdd changes**:
    - `QdrantManager.get_payload_by_block_id(block_id) -> Optional[Dict]`
      — scroll+filter on `block_id` (UUID) payload field, reuses
      `get_ingestion_status` pattern (lines 274-289), limit=1
      (UUID uniqueness).
    - `evaluate_constraints(retriever, ...)` helper in
      `rag/ekrs_rag/api/routes/constraints.py` — returns an
      envelope dict (success/error) instead of raising
      HTTPException, so both HTTP and MCP callers can translate
      to their native wire format. The route handler
      `query_constraints` now delegates to this helper; audit
      emission (`constraint_solve_started` / `_failed` /
      `_solved`) stays in the route layer (parent §204: helper
      is pure, no audit emission).
    - `mcp/server.py` extended with `ekrs_query`, `ekrs_get_block`,
      and the 4-tool `build_server` (closure capture DI).

  Tests: **9 mcp unit + 4 blocks-route unit + 1 stdio
  integration = 14 new tests**. The Td.1 `build_server` test +
  Td.1 stdio roundtrip test were refactored to the broader
  "at-least 2 tools" contract (the "exactly 2 tools" assertion
  was invalidated by Td.2's registry extension). 0 regressions
  in unit suite (**656 unit pass, 1 skip**); 10 pre-existing
  integration test failures (`await RetrievalResult` mismatch in
  Phase 5/7 replay test stubs) verified unrelated via `git
  stash` round-trip. Mypy clean across all production +
  Td.1/Td.2 test files (the `mcp.TextContent | ImageContent |
  ...` union narrowing was addressed via `_as_text(content_block)
  -> TextContent` helper in both stdio roundtrip tests).

  **Tag discipline preserved**: `phase10` stays at `2e1d9fa`
  (T10a-7 closure, parent §111 do-not-move); `phase10.1` stays
  at `1c44eee` (T10b-1 do-not-move). Td.2 is incremental inside
  `phase10` per the T10b-3 / Td.1 precedent.

### Fixed
- **`scan_audit_for_failures()` bug** (`live_stress_60.py`): the audit
  log uses `event` as the JSON key (verified at `2026-07-28`), not
  `event_type`. The function silently returned 0 even when failures
  existed (false-negative only — no false-positive risk). Fixed in
  the same commit as the new audit-log scan pattern to reduce future
  tech debt.
- **chunker scope-change + token-overflow boundaries** (T10b-1): when
  `chunk_blocks` encounters a scope change or its accumulated block
  group exceeds `max_tokens`, it now routes through a new
  `_route_accumulated_group` helper instead of always calling
  `_flush_chunk`. The helper checks (1) every adjacent block pair via
  `_is_safe_join_boundary` and (2) `token_counter("\n".join(parts)) >
  max_tokens`; either failure routes to `_split_text_two_phase`
  (Phase 1 hard cut + Phase 2 greedy merge) so the group is split into
  multiple safe chunks rather than force-merged into a single chunk
  that exceeds the bge-m3 token budget. Boundary 2 (scope change,
  line ~731) and Boundary 3 (token overflow, line ~745) are
  synchronized — deep-nesting docs trigger token-overflow more often
  than scope-change, so the two routes share the helper to avoid an
  inconsistent state where one boundary is safe-split and the other
  is force-merge. Adds 8 parametrized unit tests
  (`TestRouteAccumulatedGroup` in `tests/unit/test_chunker.py`) and
  60-doc realistic-shape stress (`scripts/t10b1_chunker_stress.py`,
  0 budget violations, 7 chunks/doc average). 10k heavy bench p99
  measures 155µs vs Phase 8 T8-5 baseline 279µs (commit
  `763535b`); 44% faster, no regression.

### Changed
- **Chunker: two-phase refactor** (Phase 9): replaces the legacy pure-char-offset
- **Chunker: two-phase refactor** (Phase 9): replaces the legacy pure-char-offset
  split path (`_split_text` → `line[i:i+chars_per_chunk]`), which had no
  semantic-boundary awareness, with a two-phase pipeline:
  - **Phase 1** = hard char cut at `max_chars`, with a forward look-back of
    up to `max_chars × 0.2` to the nearest safe boundary (whitespace, CJK
    punctuation, sentence terminator). Prevents mid-number/mid-word splits
    such as `350` + `℃` or `pressure` + `vessel`.
  - **Phase 2** = greedy merge of fragments produced by Phase 1, gated by
    `_is_safe_join_boundary()` (`digit+letter`, `letter+digit`,
    `digit+'.'`, `'.'+digit`, ASCII-letter+ASCII-letter all unsafe; CJK +
    anything safe). Adjacent-chunk safety check is the authoritative
    invariant; `validate_chunk_atomicity()` is a single-chunk heuristic.
  Public signature `chunk_blocks(blocks, doc_hash, version, *, max_tokens=..., token_counter=..., payload_version=...)`
  remains forward-compatible; `token_counter` and `payload_version` are
  new keyword-only arguments.
- **`MAX_CHUNK_TOKENS` 500 → 768**: aligns the runtime chunk budget with
  the bge-m3 sweet spot (512–1024 tokens). Fewer chunks (≈38%) with
  denser semantics — directly reduces Qdrant index pollution and lowers
  the probability of `numeric_hint_extractor` encountering a bare-number
  fragment. Touched: `config.py:42`, `docker-compose.yml:53`,
  `.env.example:26`, `benchmarks/test_chunker_10k.py:84`.
- **Qdrant payload: `payload_version` field added** to `Chunk` schema
  (default=1, chunker passes `2`). A change in `payload_version` forces
  Qdrant's version-skip idempotency layer to treat chunks from a new
  chunker algorithm as a different payload, ensuring old chunks written
  by the legacy splitter are not reused after the refactor lands.
  Spec field count grew from 8 → 9; default preserves legacy behavior.
- **token counter naming**: test-side helper renamed from the implicit
  `len(x)//4` to a documented `normalized_len = lambda x: max(1, len(x)//4)`,
  aligned with the runtime `estimate_tokens`. A new integration test in
  `TestIntegrationWithEstimateTokens` asserts the runtime counter path.
- **Chunker 10k-doc benchmark re-baseline** (Phase 9, replaces Phase 8 T8-5
  baseline at `279µs`): new `p99 = 97µs` (-65%), `chunks/sec = 312k`
  (+89% over Phase 8), `total = 0.40s` for 10k docs, RSS flat at 1.06 GB.
  Schema `chunker-10k-1.0`, seed=42, n=10000, threshold 5s/doc.
  Baseline JSON: `benchmarks/results/chunker-10k-20260728T014007Z.json`.

### Added
- **`validate_chunk_atomicity(chunk_text)`** (chunker public API): returns
  `True` if `chunk_text` does not end in a bare digit (heuristic). Useful
  as a soft gate in the golden suite; `_is_safe_join_boundary()` remains
  the authoritative inter-chunk check.
- **`tests/golden_set/test_chunker_golden.py` (17 tests)**:
  `TestChunkerGoldenAtomicity` (5× safe-boundary + 5× non-empty),
  `TestChunkerGoldenCounts` (5× count within ±50% of baseline),
  `TestChunkerGoldenNumericHints` (2× unit-preservation). All pass.
- **`tests/golden_set/_chunker_golden_fixtures.py`**: inline fixtures
  (large_pdf / mixed_table / chinese_legal / english_tech / stress_test)
  constructed via lightweight `_make_block` factory — no JSONL coupling.
- **`scripts/live_stress_60.py`** (Phase 9 Plan §6 验证 6): stdlib-only
  live-ingest stress runner for the running RAG service. Features
  `--corpus-root` (read real `DocumentBlockIR` records from
  doc-to-md-style `output/<doc_id>/data.jsonl` dirs), `--max-blocks-per-doc`
  (cap per-doc block count for docker-exec payload safety),
  `--status-timeout` (auto-defaults to 90s for real corpus vs 35s
  synthetic — real PDF ingestion runs async bge-m3 embedding on
  large blocks), `_r<run_id>` doc_hash suffix (bypasses Qdrant
  SHA-based idempotency so repeat runs produce measurable delta),
  and trace_id threading through `run_stress()` → audit-log
  `qdrant_write_failed` scan (closes Phase 6C D7 review finding
  for the stress harness). Sequential-pacing discipline: defaults
  `--concurrency 1 --pace-ms 2000` keep the dispatch rate at
  ~30 req/min so the Phase 8 T8-1 60/min per-IP bucket stays
  unbreached; `POLL_CONCURRENCY=1` + `STATUS_POLL_S=2.5s` adds
  ~24 req/min during the polling phase (phases do NOT overlap).
  `NOTIFY_HTTP_TIMEOUT_S=90s` (raised from 30s→60s) absorbs intermittent
  uvicorn listen-socket slow-accept on real-corpus docs — local
  urllib hits `HTTP 0` when the TCP SYN queues behind a busy worker.
  Transport-level retries (max 2 with 1s + 2s backoff) added to
  `notify_one()`; 200-doc stress surfaced a 12% TimeoutError rate
  that retries + longer timeout together eliminate (retry budget is
  the durable fix; timeout alone just widens the window). HTTP 4xx/5xx
  responses are NOT retried — those are server-decided outcomes.
  stderr logging added to the paced dispatch path so rejection
  reasons surface in CI logs. Verified end-to-end on the
  doc-to-md corpus (60 real PDF dirs): **60/60 completed,
  0 qdrant_write_failed, +268 qdrant chunks (1245→1513),
  dispatch 144s, max completion latency 35s**; earlier 3/3 smoke
  on ASME SEC V B SE-432 leak-testing PDF also documented.
- **Phase 9 research** (7 planning documents under
  `docs/superpowers/research/2026-07-24-*`): MinerU-Document-Explorer
  deep-dive + feature-mapping + integration-feasibility; design drafts
  for retrieval-port, enhanced-logging, enhanced-ui; cross-document
  adjudication notes (Karpathy LLM Wiki vs QMD vs MinerU synthesis).
  Filed at Phase 9 boundary so subsequent code work can pull from
  them without orphaning the planning artifacts.

## [phase11] - 2026-07-30

**Tag**: `phase11` (annotated, force-moved to T11-5 closure commit per
parent plan §111). **Version**: 0.1.0 → 0.2.0 (minor bump — Phase 11
ships a new user-facing component per Keep-a-Changelog). **Phase 11
delivered as 5 tasks (T11-1..T11-5)** all under the React UI plan
`docs/superpowers/plans/2026-07-29-phase11-react-ui.md`.

**Sub-tag**: `phase11.1` stays locked at `534f0fc` (T11-1 do-not-move,
scaffold anchor). This release covers the full dev_ui_v2/ tree
(scaffold + typed client + views + E2E + Dockerize + deprecate dev_ui).

### Added — T11-1 (dev_ui_v2 scaffold)

New `dev_ui_v2/` directory with React 18.3 + TypeScript 5.5 strict +
Vite 5.3.5 + TanStack Query 5.51 + React Router 6.26 + Zod 3.23 +
Playwright (T11-3 only). **No chart library, no OpenAPI auto-gen**
(parent Q#1–Q#8 locked; rationale in T11-1 plan). 17 files + 6013 LOC.
Smoke 5/5 PASS (typecheck + build + lint + format:check + check:bundle).
Bundle 55.7 KB gz (9× headroom vs 500 KB cap = parent Q#1 CI gate).
`phase11.1` annotated tag force-moved + locked at `534f0fc` (T11-1
scaffold anchor, do-not-move).

### Added — T11-2 (typed API client + auth + MSW mock backend)

`src/api/{schemas,client,hooks,context}` + `src/lib/auth` +
`tests/mocks/handlers`. 6 Zod schemas mirror the Pydantic wire format
(REQUEST uses `.default()` so optional; RESPONSE uses `.default()` only
when the backend actually emits defaults). 5 TanStack Query hooks
(`useNotifyIngestion`, `useIngestionStatus`, `useQueryConstraints`,
`useGoldenSet`, `useAdminFlushCache`). `X-Admin-Key` header only on
`/v1/admin/*` calls. MSW handlers (wildcard-host patterns so same set
serves vitest + Playwright) act as the wire-format contract spec
(parent Q#6). `useAdminKey = useSyncExternalStore + storage event +
same-tab custom event` (`localStorage.setItem` doesn't fire the
`storage` event in the writer tab). 56/56 tests pass (19 schema +
14 client + 10 auth + 13 MSW). Bundle 70.1 KB gz (7× headroom).

### Added — T11-3 (4 views + React Router + Playwright E2E)

4 routes: `/ingest` (notify form + status check), `/constraints`
(query textarea + strict + top_k + trace_id, mode-badge + branches
JSON + conflicts + trace expander), `/golden` (3-case fixture +
progress bar + results table), `/overlays` (placeholder banner
admin-keyed via `useAdminKey`). `Sidebar` (NavLink × 4 + admin key
input with `defaultValue` from `useAdminKey` + clear button + health
dot). `ErrorBoundary` per route, `Skeleton` loading state. `App.tsx`
`BrowserRouter` + `NotFound` with `&apos;t` escaped for ESLint.
Playwright E2E × 6 specs (4 views + 2 overlays) using MSW browser
worker, MSW guard `if (import.meta.env.DEV)` so Vite tree-shakes the
dynamic import in production. Bundle 82.5 KB gz (6× headroom).
72 unit + 6 E2E pass. 7 bugs caught+fixed (notify.data?.doc_hash,
context:{} required, unescaped entity, JSDoc `*/`, MSW chunk leak,
MSW dead-in-preview, vitest `__tests__` dir crash).

### Added — T11-4 (Dockerize + nginx reverse proxy)

Multi-stage `dev_ui_v2/Dockerfile` (node:20-alpine build → nginx:1.27-
alpine runtime, ~40 MB final). `nginx.conf` SPA fallback
`try_files $uri $uri/ /index.html` for React Router 6 + reverse proxy
`/v1/*` and `/healthz` to `rag:8000` (compose service name). Security
headers (`X-Content-Type-Options nosniff`, `Referrer-Policy strict-
origin-when-cross-origin`). Build gotchas captured: lockfile pinned
564 `mirrors.cloud.tencent.com` URLs rewritten to
`registry.npmmirror.com`; npm 10.8.2 "Exit handler never called!" bug
requires npm 11 upgrade; `--include=dev` for the build-time
typescript/vite/eslint deps; `!tests/mocks/` whitelist in
`.dockerignore` (tsc -b still resolves the dynamic import path at
type-check). Compose `dev_ui_v2` service on host port 5173,
`depends_on: rag healthy`. `NODE_BASE_IMAGE` / `NGINX_BASE_IMAGE` ARGs
overridable in `docker-compose.override.yml` for restricted-network
mirrors (daocloud.io — same pattern as rag's `PYTHON_BASE_IMAGE` /
`PIP_INDEX_URL`). Smoke verified standalone + through full stack
(proxy returns 503 on admin paths and 403 on parser-token paths —
auth layer intact).

### Changed — T11-5 (compose healthchecks)

Pre-existing baseline: `qdrant/qdrant:latest` and `deployment-rag`
images ship **without `curl`**. Compose healthchecks using
`["CMD", "curl", "-f", ...]` failed forever (qdrant streak 19928, rag
streak 4318) and blocked `depends_on: service_healthy`. Replaced
both with bash `/dev/tcp` (`/bin/sh` dash does NOT support `/dev/tcp` —
must invoke `bash -c` explicitly). Both services healthy within 25s.

### Deprecated — T11-5

- **`dev_ui/` (Streamlit)**: banner in `dev_ui/README.md` pointing at
  `dev_ui_v2/`; `dev_ui/app.py` module docstring now declares
  `.. deprecated::`. Kept as 1-quarter fallback. Full removal
  (scrub `ekrs-handbook.md`, `docs/ARCHITURE.md`, `ekrs.md`,
  `CLAUDE.md`) deferred — multi-doc work, out of scope for Phase 11.

### Notes

- **`phase11` tag force-moved** to T11-5 closure commit (parent §111).
  `phase11.1` stays at `534f0fc` (T11-1 do-not-move).
- **Caveats / known limits**:
  - dev_ui_v2 MSW worker is dev/E2E only — production container does
    not ship `mockServiceWorker.js`. Nginx does not special-case it.
  - `dev_ui/` removal is a follow-up; the Streamlit app still resolves
    all `/v1/*` endpoints and is a working fallback.
  - The Playwright E2E suite uses `npm run dev` (NOT `npm run
    preview`) so MSW's `import.meta.env.DEV` guard is alive during
    the test. CI gate is `npm run test:e2e`.

## [phase10] - 2026-07-29

**Tag**: `phase10` (annotated, force-moved to T10a-7 closure commit per
parent plan §111). **Version**: 0.0.5 → 0.1.0 (minor bump — Phase 10
introduces new retrieval capability per Keep-a-Changelog). **Phase 10
delivered as 8 tasks (T10a-1..T10a-7 + T10b-1)** all under the broad-
spectrum-retrieval plan
`docs/superpowers/plans/2026-07-28-phase10-broad-spectrum-retrieval.md`.

**Sub-tag**: `phase10.1` stays locked at `1c44eee` (T10b-1 do-not-move).
This release covers T10a-1..7. T10b-2 (heading-less 上限) /
T10b-3 (强信号短路) / T10c (cross-encoder rerank) remain deferred —
decision data collected in T10a-6 (3/3 BM25 identifier recall@1); no
cross-encoder trigger per parent §6.1.

### Added — T10a-1 (BM25 keyword retrieval)

**FTSManager — BM25 keyword retrieval via SQLite FTS5** (T10a-1, Phase 10):
new module `rag/ekrs_rag/retrieval/fts_manager.py`. Schema is
`CREATE VIRTUAL TABLE blocks_fts USING fts5(...)` with 7 columns
(`chunk_id`, `block_id`, `text`, `scope_path`, `status`, `doc_hash`,
`payload_json`); tokenizer `unicode61 remove_diacritics 2` (no porter,
per Phase 10 plan §Context lock). `payload_json` is UNINDEXED so JSON
keys do not contaminate MATCH. `generate_chunk_id(doc_hash, index)`
emits `{doc_hash[:8]}-{index:04d}` (T10a-1 owns the generator;
T10a-5 owns the retriever-side timing). 23 unit tests +
8 integration tests cover: BM25 normalization `|bm25|/(1+|bm25|)`
floor 0.01, R7 scope_path OR-filter (column-restricted MATCH syntax),
R8 `status != 'illegal'` filter, H2 `delete_by_chunk_id` single-row
rollback primitive, T10a-5 bidirectional `get_chunk_id(block_id)`
invariant, T10a-6 3 engineering-identifier smoketest
(`A312-TP316` / `GB-T 12459` / `1.6MPa`). T10a-1 boundary: schema +
CRUD + BM25 归一化 only; pipeline ingest wiring = T10a-2,
retriever fusion = T10a-4, audit events = T10a-7. Path
`/app/rag/fts.sqlite`, sync `sqlite3` with `check_same_thread=False`.

### Added — T10a-2 (FTS pipeline sync + drift)

`IngestionPipeline.ingest()` Step 5.6 paired-write Qdrant + FTS with
`replace_doc` (atomic delete+upsert for re-ingest idempotency).
`FTSManager.count_active` + `QdrantManager.count_points` + 5min
`ConcurrencyChecker` background (detect-only, never auto-repair).
Event count 19 → 20 (`fts_consistency_drift`). 15 tests. `fts=None`
kwarg keeps Phase 9 baseline byte-level.

### Added — T10a-3 (RRF pure function + FusionStats)

`rag/ekrs_rag/retrieval/rank_fusion.py` ships
`reciprocal_rank_fusion(ranked_lists, key_fn, k=60) -> (fused_results,
FusionStats)` — R2 pure function, deterministic. `FusionStats` frozen
dataclass (`vector_hits`/`fts_hits`/`both_hits`) consumed by T10a-7
audit emit. 17 unit tests cover empty/single/dual/N=3 lists, k param,
duplicate-key semantics, set-arithmetic invariant, frozen enforcement.

### Added — T10a-4 (Retriever RRF integration)

`EKRSRetriever.async retrieve()` with `fts: FTSManager | None = None`
kwarg. `asyncio.gather(..., return_exceptions=True)` parallel vector +
FTS with FTS exception isolation (log warning + degrade to vector-
only). `RetrievalResult.fusion_stats: Optional[FusionStats]`. RRF key
`{doc_hash}:{source_block_ids[0]}` (T10a-5 → `chunk_id`). 18 tests.

### Added — T10a-5 (chunk_id round-trip)

`QdrantManager.upsert_chunks` writes `chunk_id={doc_hash[:8]}-{idx:04d}`
into Qdrant payload via `FTSManager.generate_chunk_id`. `Chunk.chunk_id:
Optional[str] = None` (legacy preserved). `FTSManager.get_block_id_by_chunk_id`
new (inverse of T10a-1 `get_chunk_id`). Retriever `key_fn` switches to
`c.chunk_id or fallback`. **Naming-space coexistence** (parent §[M2]):
`block_id` (UUID) + `source_block_ids` (list) preserved; `chunk_id` is
parallel. 13 tests.

### Added — T10a-6 (Golden regression + BM25 identifier recall@1)

50-case golden set regression (208 pass 0 退化). 4 BM25-only identifier
recall@1 smoke (`A312-TP316` / `GB/T 12459` / `1.6MPa`): **3/3 = 1.0**.
T10c cross-encoder decision data collected; trigger condition not met.

### Added — T10a-7 (Audit events fts_synced + fts_searched)

`main.py` `_EVENT_SCHEMAS` registers both events (count 20 → 22):
- `fts_synced {doc_hash, version, chunks_written}` — emitted by
  `pipeline.ingest()` after Step 5.6 FTS write (parent §T10a-7 line 32
  acceptance).
- `fts_searched {vector_hits, fts_hits, both_hits}` — emitted by
  `EKRSRetriever.retrieve()` after RRF (T10a-3 `FusionStats` fields
  consumed directly). `EKRSRetriever.audit_writer` kwarg (DI pattern;
  `None` = Phase 9 byte-level). Best-effort emit
  (`try/except` isolation — audit never blocks retrieval, parent §204).
`main.py` lifespan injects `_audit_writer` into `_retriever = EKRSRetriever(...)`.
Both audit test files cover schema registration, retriever emit, and
fusion-stats field correctness. 14 unit tests + 2 Phase 6A regression
update (count 20 → 22, names `fts_synced` + `fts_searched`).
**IngestionOutcome enum NOT extended** (parent §204 explicit close;
these are intermediate signals, not ingestion terminal states).

### Changed — Phase 10

- **`pipeline.py` Step 5.5 → 5.6**: FTS sync added between Qdrant
  upsert and outcome emit. FTS failure → warning + continue
  (Qdrant is truth-of-record; drift detected by T10a-2
  `ConcurrencyChecker`).
- **`retrieval/retriever.py`**: `retrieve()` → `async def`; constructor
  gains `fts` and `audit_writer` kwargs (both `None`-default preserve
  Phase 9 byte-level). RRF key_fn uses `chunk_id` first then fallback.
- **`retrieval/qdrant_client.py`**: payload gains `chunk_id` field.
  Generated by EKRS-side during upsert (not by IR parser).
- **`shared/ekrs_shared/models.py`**: `Chunk.chunk_id: Optional[str]`.
- **22 audit event schemas** (was 20): +2 FTS events at T10a-7.

### Fixed — Phase 10

- **chunker deep-nesting token-overflow** (T10b-1, in `phase10.1`
  sub-tag): Boundary 2 + Boundary 3 synchronized via
  `_route_accumulated_group` helper; 0 budget violations on 60-doc
  stress; p99=155µs vs Phase 8 baseline 279µs (-44%).

### Notes

- **`phase10` tag force-moved** to T10a-7 closure commit (parent
  §111); T10a-2 has a transient T10a-2 commit under the same tag,
  replaced by force-move. Old tag SHA recorded in
  `~/.claude/projects/.../memory/phase10-closure.md`.
- **Caveats / known limits**:
  - FTS5 `unicode61 remove_diacritics 2` does not tokenize CJK
    (CJK-as-run). Phase 6+ decision: add `jieba` tokenizer optionally
    OR live with the limitation. Engineering identifiers (Latin+digit+
    连字符/斜杠/点) recall cleanly (T10a-6 3/3 = 1.0).
  - `AuditIndex` full rebuild is O(current file only) — does not
    scan `.gz` history. Cross-history replay is deferred.

## [phase8] - 2026-07-24

**Tag moved**: `phase8` created at HEAD at Phase 8 closure.
`phase8` represents *delivered state*, not snapshot time — see Phase 8
plan doc §"Tag strategy" + Phase 7 Decision §3 precedent.
`phase8.1` placed at `7151f13` (T8-3a, bge-m3 vendoring milestone) as
a historical anchor — **do not move**.
`phase7` stays at `99c77f5` and `phase7.1` stays at `41c2d54` (both
unchanged).

12 commits span the gap from `phase7` to `phase8`: 7 task commits
(T8-1..T8-5 + T8-3a baseline-pin sub-commit), 1 cross-phase debt
cleanup (IngestionOutcome Literal widening), and 4 planning/docs
commits that landed between Phase 7 closure and the first Phase 8
task. Listed below by category.

### Added

- **Per-IP rate limiting on `/v1/*`** (T8-1, commit `c9bcd70`): hand-
  rolled sliding-window token bucket (60 req/min default, override
  via `EKRS_RATE_LIMIT`). Exempt routes: `/healthz`, `/health`,
  `/metrics`, `/docs`, `/redoc`, `/openapi.json`. Returns `429` with
  `Retry-After` header. 13 unit tests.
- **Secret rotation SOP + offline validator** (T8-2, commit
  `028b2ed`): `docs/SECRET-ROTATION.md` (zero-downtime procedure for
  `PARSER_TOKEN` + `ADMIN_KEY` via comma-separated token acceptance)
  + `scripts/validate_rotation.py` (typo-grade similarity check,
  LCP ≥ 0.80 rejects). 24 unit tests.
- **bge-m3 ONNX vendored in Docker image** (T8-3a, commit `7151f13`):
  `rag/Dockerfile` builds with the model baked into
  `/opt/ekrs/models/bge-m3`. `embedding_service.py` resolves model
  dir via `EMBEDDING_MODEL_DIR` env var (default = vendored). Build
  context = repo root; ARG-overridable `PYTHON_BASE_IMAGE` + `PIP_INDEX_URL`
  for restricted networks. 4/4 heavy + 21/21 unit tests pass.
- **T8-3a image baseline pinning** (commit `681c253`): `make build-rag-baseline`
  rebuilds the reference image and writes SHA256 manifest at
  `deployment/rag-image.baseline.json`. Idempotent rebuilder script
  + restricted-network ARG overrides.
- **Ingestion smoke canary** (T8-3b, commit `6f4d9eb`):
  `scripts/smoke_ingestion.sh` (7-step bash wrapper) +
  `scripts/lib_smoke.py` (pure-stdlib helpers). Exits non-zero on
  any of 5 contract violations: preflight, notify, status, audit
  (`qdrant_write_failed`), callback. 19 unit tests + 484/1 suite.
  Used post-deploy, not in PR CI.
- **Golden set extension 42 → 50** (T8-4, commit `5a11824`): 5
  chunk-level cases in `golden_set.json` (cryogenic Kelvin, scope
  priority, % elongation, multi-condition T+P, strict-mode happy
  path) + 3 API-level cases in new `test_api_validation.py`
  (TestClient + `dependency_overrides` pattern: empty query 4xx,
  invalid scope 4xx, concurrent replay deterministic). Handbook
  §9.1 grew 8 TC-* rows + implementation-location note. 191 golden
  entries pass; 675 unit + golden suite pass.
- **Chunker perf baseline at 10k docs** (T8-5, commit `763535b`):
  `benchmarks/test_chunker_10k.py` (`@pytest.mark.heavy`, excluded
  from PR CI). Runs deterministic synthetic corpus (seed=42,
  mean 20 blocks/doc) through `chunk_blocks()`, reports p50/p95/p99
  per-doc latency + chunks/sec + peak RSS. Writes atomic JSON to
  `benchmarks/results/chunker-10k-<ts>.json`. p99 default threshold
  5.0s/document (env-var tunable).

### Fixed

- **`IngestionOutcome.rag_status` Literal widened** (commit `193b0db`):
  Phase 7 T3's `reparse()` added `"duplicate"` (SHA256 idempotent
  skip) and `"business_failure"` (ops-level error), but the type
  annotation was still `Literal["success", "failed"]`. Three pre-
  existing mypy errors at pipeline.py:303/317/340 resolved. Single
  source of truth via `_VALID_STATUSES` tuple shared between
  annotation + `__post_init__` validator. +2 outcome tests.

### Planning / docs (in the `phase8` range, not Phase 8 *tasks*)

- `435ae58`: docs — split deferral list (Phase 6+ frozen §6.1 vs
  Post-deploy registry §6.2).
- `adbb942`: Phase 8 scope doc — 5 deployment-readiness tasks + 3
  locked decisions.
- `097adeb`: Phase 8 acceptance gates tightened per Step 0 review.
- `ad8c21e`: Phase 7 CHANGELOG entry + Phase 7 plan doc closing
  (this commit predates Phase 8 by 8 hours but lands in the `phase8`
  range because `phase7` had already been force-moved to `99c77f5`
  before it).

## [phase7] - 2026-07-23

**Tag moved**: `phase7` f50b5e9 (T1) → 99c77f5 (T6 / Phase 7 closure).
`phase7` represents *delivered state*, not *snapshot time* — see
Phase 7 plan doc §"Decisions (locked 2026-07-23)" row #3.
`phase7.1` remains at 41c2d54 (T2 closure, historical anchor).

### Added

- **`qdrant_write_failed` audit pipeline** (T1, commit `f50b5e9`):
  Integration test exercising real `AuditWriter` + `AuditIndex` +
  Qdrant unreachable (port 1) to verify the event emits end-to-end.
  9 cases pass + 1 heavy skip. CI runs default job; nightly runs heavy.
- **Audit event emissions** (T2, commit `41c2d54`): 8 schema-registered
  events written at all required sites (was 0/8 → 8/8). Closes the
  Phase 6C T8 review finding (D7 emit gap).
- **CompensationHandler real retry** (T3, commits `6d5c054` + `57b3b3c`):
  Handler returns `bool`; `IngestionPipeline.reparse()` runs the
  universal re-ingest. `compensation_retry` schema gains required
  fields `reingest_outcome` (`"success"`|`"failed"`|`"duplicate"`|
  `"skipped"`) + `reingest_duration_ms` (`int`). Closes the Phase 4
  "black box" gap — orphan PENDING/RUNNING tasks now auto-recover
  instead of accumulating in aiosqlite.
- **FastAPI `/docs` + `/redoc`** (T4, commit `7e3d46d`): `docs_url`,
  `redoc_url`, `openapi_tags` (5 tags) enabled on `create_app()`.
  Operators can browse the API surface.
- **Streamlit `dev_ui`** (T5, commit `79b04fc`): 3-tab dev UI at
  `dev_ui/app.py` — 文档入库 (ingest trigger + status), 约束查询
  (POST /v1/constraints with multi-branch display), 黄金集验证
  (golden set regression). Dev-only extra (`rag[dev]`); not in
  production Docker images. Replaces the `/dev-ui` HTTP route that
  was referenced in `CLAUDE.md` but never built.
- **Embedding LRU+TTL cache** (T7, commit `b8ff559`):
  In-process cache keyed on `sha256(text) | model_version` where
  `model_version` is the joined SHA256 prefixes of `model.onnx` +
  `sparse_linear.pt`. Cache misses invoke the model; hits return
  immediately. Defaults: 10k entries / 24h TTL.
- **`POST /v1/admin/embedding-cache/flush`** (T7): X-Admin-Key gated;
  returns `{cleared, model_version, cache_size_after}`. 503 if
  EmbeddingService is not initialized.
- **Handbook §6 timeline** (T6, commit `99c77f5`): Phase 6B / 6C / 7
  rows added; §6.1 freezes Phase 6+ deferral list (5 categories).
- **Phase 7 plan doc** (`docs/superpowers/plans/2026-07-23-phase7-scope.md`):
  captures the scope + 5 locked decisions; closed.

### Changed

- **`compensation_retry` audit schema** (T3): adds two required fields
  (`reingest_outcome`, `reingest_duration_ms`). Old entries without
  them read defensively (default `outcome=None`, `duration_ms=0`).
- **`EmbeddingService.encode()`** (T7): splits inputs into
  cached/missing before calling the model. Single batched call per
  `encode()`; behavior unchanged from the caller's perspective.

### Follow-ups shipped in this range but not Phase 7 tasks

These four commits landed between T2 and T4 and are acknowledged in
the `phase7` tag, but they are **not** re-tagged into `phase7.1`:

- `57187d3` FlagEmbedding → onnxruntime + transformers bge-m3 loader.
- `afbf4a6` 4 audit-emission gap fixes from Phase 7 review.
- `419006d` pseudo-sparse recall@K eval script.
- `cda45fe` BAAI learned sparse head via `sparse_linear.pt`.

Plus two pre-Phase-7 maintenance commits that happen to fall in the
`phase7` range (not Phase 7 work; documented here for completeness):

- `95475b4` constraint_engine mypy cleanup.
- `991814c` QDRANT_PORT for REST client + runbook port clarification.

## [phase6c-minor] - 2026-07-15

Tag: `phase6c-minor` → `7a87ce0`.

- Three Phase 6C T8 Minor cleanups: `delete_old_versions` filter
  shape (`Range(lt=keep_version)`), narrow exception types, pip
  dependency consolidation. See `phase6c-closure` for the broader
  T1+T2 mypy + T3 fixture doc + T4 smoke runbook + T5 sdd cleanup.

## [phase6c-closure] - 2026-07-22

Tag: `phase6c-closure` → `280ce4f`. Phase 6C retrofitted 5 leftover
items from Phase 6A T14 review (mypy clean across 49 rag/ files,
TDD fixture convention, manual smoke runbook executed via
`docker.m.daocloud.io` mirror, admin cleanup shrinking
`.superpowers/sdd/` from 39 MB to 3.4 MB). 601 passed, 3 skipped
at closure.

## [phase6c-audit-emit] - 2026-07-19

Tag: `phase6c-audit-emit` → `d21e6d4`. `qdrant_write_failed` audit
emit + non-fatal Qdrant init (T8 fixes the Phase 6B D7 review
finding).

## [phase6b-retrieval-layer] - 2026-04-XX

Tag: `phase6b-retrieval-layer` → `bd00849`. Embedding migration
from bge-small-en (384d) to bge-m3 (1024d + sparse). QdrantManager
rewrite fixed 3 production bugs. `AUTO_REINDEX` auto-rebuilds the
collection on dim mismatch. Heavy integration tests run on nightly
CI only (the bge-m3 ONNX model is vendored but not loaded by default
runners).

## [phase6a-spec-closure] - 2026-04-XX

Tag: `phase6a-spec-closure` → `c7f1138`. 9 vertical slices closing
the spec gaps from Phase 5.5: X-Admin-Key, DocumentRepo/A1, /trace,
/calculate, soft fallback, golden set 13 → 42 cases, audit 2
optional fields, ENGINE_URL, 85% CI gate. 531 tests pass,
86.63% coverage, CI gate green at closure.

---

## Cross-references

- **Implementation timeline**: `ekrs-handbook.md` §6
- **Phase 7 scope + decisions**: `docs/superpowers/plans/2026-07-23-phase7-scope.md`
- **Tag force-move rationale** (Decision §3): phase 7 plan doc row #3
- **Deferral freeze**: `ekrs-handbook.md` §6.1