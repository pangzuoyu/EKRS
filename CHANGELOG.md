# Changelog

All notable changes to EKRS are documented here by release tag. The
canonical implementation timeline lives in `ekrs-handbook.md §6`; this
changelog focuses on **what was delivered per phase tag** so the diff
from the previous phase is readable without consulting the handbook.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) —
`Added`, `Changed`, `Fixed`, `Removed` per release.

## [Unreleased]

### Added
- **FTSManager — BM25 keyword retrieval via SQLite FTS5**
  (T10a-1, Phase 10): new module
  `rag/ekrs_rag/retrieval/fts_manager.py`. Schema is
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
  retriever fusion = T10a-4, audit events = T10a-7.
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

`FTSManager` (SQLite FTS5 mirror of Qdrant payload) — see [Unreleased]
above for full description. 30 unit + integration tests. Path
`/app/rag/fts.sqlite`, sync `sqlite3` with `check_same_thread=False`.
Tokenizer `unicode61 remove_diacritics 2`. Schema 7 columns with
`payload_json UNINDEXED`. `generate_chunk_id` owner (T10a-5 reader).

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