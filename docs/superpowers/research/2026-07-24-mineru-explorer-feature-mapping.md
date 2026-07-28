# MinerU-Document-Explorer → EKRS Feature Mapping

> Research artifact — **research only, no implementation**.
> Date: 2026-07-24
> Source: <https://github.com/opendatalab/MinerU-Document-Explorer> (cloned to `/tmp/mineru-explorer/`)
> Purpose: Identify which MinerU-Document-Explorer (a.k.a. **QMD** — "Query Markdown") features could inspire EKRS Phase 9+ extensions, given EKRS's existing architecture (Phases 1-8 complete, Iron Rules R1-R8) and the §6.1 / §6.2 deferral freezes.

---

## Context: why bother

EKRS and QMD solve related-but-different problems:

| Aspect | EKRS | QMD |
|--------|------|-----|
| Domain | Engineering constraint extraction (T, P, material) | General document exploration / RAG |
| Output | Structured numeric intervals per parameter (via `portion`) | LLM-maintained wiki + hybrid search |
| Storage | Qdrant (vector) + aiosqlite (tasks) + Redis (locks) | sqlite-vec + FTS5 + content-addressable |
| Auth model | `X-Parser-Token` (write) + `X-Admin-Key` (admin) | MCP client (local trust) |
| Retrieval | Composite scope-aware vector scoring (Phase 3) | BM25 + vector + RRF + cross-encoder reranker |
| Models | bge-m3 ONNX (vendored) | 3 GGUF models (embeddinggemma-300M, qwen3-reranker-0.6b, qmd-query-expansion-1.7B) |

QMD's research value for EKRS is **the design vocabulary** (hybrid search pipeline, boundary-aware chunking, MCP tool taxonomy, wiki-lint health checks) — not the specific tech choices. EKRS already has vector search; what QMD contributes is *patterns* that improve recall and operability without violating the Iron Rules.

This document maps QMD's features against EKRS's existing capabilities and Iron Rules, and recommends Phase 9 scope candidates.

---

## Direct inspiration candidates (high fit)

### 1. FTS5 BM25 alongside Qdrant vector (QMD hybrid-search.ts)

**What QMD does.** Parallel BM25 (FTS5) + vector (sqlite-vec) per query, fused via RRF k=60, with the original query weighted ×2 and a top-rank bonus. Strong-signal short-circuit at score ≥0.85 with gap ≥0.15 skips expansion entirely.

**What EKRS has.** Single retrieval path: Qdrant vector search via bge-m3 (1024d dense + sparse). Composite score = `vector_score * (1 + scope_priority)`. No keyword index.

**Why it's a fit.**
- Engineering documents contain *exact* identifiers that vector search handles poorly: `1.6MPa`, `A312-TP316`, `GB/T 12459`, `20#`. A user querying "1.6MPa flange" gets better recall when BM25 matches the exact token.
- The Iron Rules don't preclude keyword search — R5 forbids graph DB, not lexical retrieval. R8's "index layer only filters illegal status" is compatible with an FTS5 layer as long as it filters `status != "illegal"`.
- Implementation footprint is small: SQLite FTS5 virtual table mirrored from the Qdrant payload, kept in sync via the existing ingestion `AuditWriter` (one new event: `fts_index_synced`).

**Tradeoff.** Two indexes to keep in sync → two write paths. Mitigation: same idempotency guarantees as Qdrant upserts (Phase 7 T1's `qdrant_write_failed` integration test pattern).

**Recommended Phase 9 candidate.** Yes — small, high-value. Estimated effort: medium (3-5 tasks).

### 2. Cross-encoder reranking (QMD search.ts rerank step)

**What QMD does.** `qwen3-reranker-0.6b-q8_0` GGUF (~640 MB) cross-encoder produces logprob-confidence scores per (query, doc) pair. Position-aware blending: top 1-3 RRF candidates blend 75% RRF / 25% reranker (preserve exact matches); top 11+ blend 40% RRF / 60% reranker.

**What EKRS has.** Composite scope-aware scoring (Phase 3). No semantic reranker — the scope modifier is a structural heuristic, not a learned relevance signal.

**Why it's a fit.** A reranker would refine the top-K before the hint extractor runs, reducing false positives in `evidence_builder.match_hints_to_intervals()`. This is particularly valuable for *ambiguous* queries where scope priority alone can't disambiguate.

**Tradeoff.**
- Cross-encoder latency (~50-200 ms/pair) at K=40 candidates ≈ 2-8 s/query, eating into the 5 s solver budget (`T8-5` chunker baseline).
- GGUF model adds ~640 MB to the Docker image on top of bge-m3's 2.1 GB. PD-6 (vendor distribution) already considers bge-m3 weight; adding another model is a known cost.
- Phase 7 T7's embedding cache must NOT invalidate when the reranker model version changes independently — cache key composition needs review.

**Recommended Phase 9 candidate.** Yes — but later than FTS5, and gated on a latency budget measurement. Effort: large (model integration + cache versioning + perf baseline).

### 3. Markdown-aware chunking with boundary scoring (QMD chunking.ts)

**What QMD does.** 900-token target with 15% overlap. Break point scoring: H1=100, H2=90, H3=80, code-fence=80, hr=60, blank-line=20, list-item=5. Distance-decay within a 200-token window: `finalScore = baseScore × (1 - (distance/window)² × 0.7)`. Code blocks protected (no breaks inside fences).

**What EKRS has.** `rag/ekrs_rag/ingestion/chunker.py` — scope-aware chunker with block-level boundaries, R1-compliant (source_span + block_id + context_window). Chunk size and overlap configurable but currently fixed.

**Why it's a fit.**
- Boundary scoring is *additive* to the existing scope-aware logic — doesn't replace it.
- The `code-block protected` rule directly improves engineering PDF extraction: code/IO-list snippets are common in instrumentation diagrams and shouldn't be split mid-block.
- The distance-decay window means we can keep EKRS's current chunk size targets while improving coherence at section breaks.

**Tradeoff.** Boundary scoring adds a few microseconds per chunk — irrelevant given T8-5's p99=279µs headroom.

**Recommended Phase 9 candidate.** Yes — small effort, additive to existing logic.

### 4. MCP tool surface for AI agent integration (QMD docs/mcp.md)

**What QMD does.** 15 tools in 3 groups: Retrieval (query, get, multi_get, status), Deep Reading (doc_toc, doc_read, doc_grep, doc_query, doc_elements, doc_links), Knowledge Ingestion (wiki_ingest, doc_write, wiki_lint, wiki_log, wiki_index). Stdio + Streamable HTTP transports.

**What EKRS has.** HTTP API only (`/v1/ingestion/notify`, `/v1/constraints`, `/v1/admin/*`, `/healthz`, `/metrics`). OpenAPI spec auto-published via FastAPI. No MCP integration.

**Why it's a fit.**
- Phase 7 already enabled `/docs` + `/redoc` (T4, commit `7e3d46d`) — the API surface is designed for agent consumption.
- An MCP adapter would let Claude / Cursor / VS Code agents query `/v1/constraints` natively without re-implementing HTTP + token auth.
- The 3-group taxonomy maps cleanly: Retrieval = `/v1/constraints`, Deep Reading = a future `/v1/blocks/{block_id}` endpoint (read block content by ID), Knowledge Ingestion = `/v1/ingestion/notify` (write).

**Tradeoff.** MCP transport requires either `mcp` Python package (new dep) or hand-rolling JSON-RPC over HTTP. The Streamable HTTP transport matches QMD's pattern but introduces a new server lifecycle (PID file, daemon mode) — operations work that PD-2 (multi-region) already notes is deferred.

**Recommended Phase 9 candidate.** Yes — high visibility, low risk (read-only tools first; write tools later). Effort: medium.

### 5. Schema versioned migrations (QMD db-schema.ts v1-v3)

**What QMD does.** Explicit migration steps with version stamps (v1, v2, v3). Each migration is a function that takes the current DB and applies schema changes idempotently.

**What EKRS has.** `rag/ekrs_rag/concurrency/task_repo.py` (aiosqlite) — schema is created on first connect via inline `CREATE TABLE IF NOT EXISTS`. No version tracking.

**Why it's a fit.**
- aiosqlite migrations are currently manual: schema lives in code, and breaking changes require a separate "ensure schema" step.
- A versioned migration framework catches schema drift in integration tests (e.g., Phase 8 T8-3a's image baseline pinning discovered the bge-m3 SHA mismatch via runtime check, not schema check).
- The Iron Rules don't preclude migrations — R5 forbids graph DB specifically.

**Tradeoff.** Existing tables don't need migration, only forward-compatible evolution. Migration framework adds ~100 LOC.

**Recommended Phase 9 candidate.** Yes — small effort, prevents future drift.

---

## Tangentially relevant (medium fit)

### 6. Query expansion (QMD search.ts expand step)

**What QMD does.** `qmd-query-expansion-1.7B-q4_k_m` GGUF generates lex / vec / hyde variations of the user query, deduplicated. Strong-signal short-circuit skips it when BM25 has a clear winner.

**What EKRS has.** Direct retrieval — user query → vector → composite score. No expansion.

**Why it's only medium fit.** Engineering queries are usually *precise* ("design pressure of the reflux drum") rather than exploratory ("tell me about heat exchangers"). Expansion helps the latter more than the former. The bge-m3 model already handles semantic similarity; expansion's marginal value is unclear without measurement.

**Recommended Phase 9 candidate.** Defer — measure FTS5 + reranker impact first (items 1 + 2). If recall@K is still weak on engineering terms after those, reconsider expansion.

### 7. Wiki concept (QMD src/wiki/)

**What QMD does.** Wiki collections with `wiki_ingest` (prepare source for wiki processing), `doc_write` (auto-logged), `wiki_lint` (orphans / broken links / stale pages / hub detection), `wiki_log` (5 ops: ingest / update / lint / query / index), `wiki_index` (auto-generated `index.md`).

**What EKRS has.** Audit log + replay (`/v1/constraints/replay`, `/v1/ingestion/replay`). No wiki, no link graph, no lint.

**Why it's only medium fit.**
- The link graph concept maps loosely to EKRS's `scope_path` (constraint cross-references by scope). But EKRS's scope is a *hierarchy*, not a *graph* — R5 forbids graph DB, and wiki lint's link graph would need a flat table representation, losing the priority semantics.
- `wiki_lint`'s orphan detection has no analogue in EKRS — every constraint has a `source_span` (R1), so orphans are impossible by construction.
- `wiki_index`'s auto-generation would need a new entity (a "compiled standard" view) that doesn't exist in EKRS today.

**Recommended Phase 9 candidate.** Defer — maps to a "compiled engineering standard" feature that's out of scope for Phase 9 unless there's a specific user need. The audit log + replay already covers observability.

### 8. Content-addressable storage (QMD schema: content / documents)

**What QMD does.** `content(hash) → text` table + `documents(path) → hash` table. Idempotent indexing: same content re-indexed → same hash → no-op.

**What EKRS has.** SHA256-based deduplication in the ingestion pipeline (Phase 7 T3's `reparse()` added `"duplicate"` status). No content-addressable storage layer.

**Why it's only medium fit.** Phase 7 already gets the dedup benefit via SHA256 check. A content-addressable layer would deduplicate *across documents* (e.g., same boilerplate spec text in many PDFs), but engineering docs are mostly unique content, so the savings are minor.

**Recommended Phase 9 candidate.** Defer — not enough benefit to justify a refactor.

### 9. Per-page / per-section format caches (QMD: pages_cache, toc_cache, section_map, slide_cache)

**What QMD does.** Caches extracted text per page / TOC structure / DOCX section / PPTX slide, keyed by content hash. Avoids re-extraction on re-index.

**What EKRS has.** Single `block_id` keyed by doc SHA + chunk seq. No sub-document caching.

**Why it's only medium fit.** EKRS extracts one block at a time during ingestion, not per-page. The cache would benefit a future "re-extract after parser schema change" workflow, which doesn't exist yet.

**Recommended Phase 9 candidate.** Defer — no concrete workflow justifies it.

---

## Does NOT fit EKRS (explicit exclusions)

### 10. Multi-format backend framework (QMD backends/pdf.ts, docx.ts, pptx.ts, markdown.ts)

**What QMD does.** Per-format `DocumentBackend` interface (`getToc`, `readContent`, `grep`, `query`, `extractElements`). Format detection via `registry.ts`. Python subprocess for non-markdown formats.

**Why it doesn't fit.** EKRS receives **already-parsed JSONL** from an external Parser service (Phase 4, callback idempotency). The parsing responsibility lives outside the RAG service. Adding backends would duplicate the Parser's work and violate the "Parser writes JSONL, RAG reads it" architecture.

### 11. Web search / external content (QMD SYNTAX.md mentions web sources)

**What QMD does.** Some wiki collections include external URLs as sources, tracked via `wiki_sources` table.

**Why it doesn't fit.** EKRS's inputs are curated engineering documents from the internal Parser. External web sources are explicitly out of scope (R4's "User > Explicit_Doc > Inferred_Doc > Default" — User query doesn't elevate to Explicit_Doc unless ingested).

### 12. Document-level editing (QMD doc_write)

**What QMD does.** `doc_write` writes markdown documents, auto-logged for wiki collections.

**Why it doesn't fit.** EKRS is read-only on the constraint side (R2: solver is pure). Writes go through the Parser's notification path, not direct doc_write.

### 13. LLM-generated wiki content (QMD wiki ingest flow)

**What QMD does.** `wiki_ingest` prepares a source for wiki processing — typically involves LLM summarization/extraction.

**Why it doesn't fit.** EKRS's hint extractor (Phase 2) is rule-based, not LLM-based. LLM in the critical path would violate R2 (deterministic solver) and add non-determinism to the constraint pipeline. The handbook §6 timeline explicitly defers "LLM in retrieval" to a future phase.

### 14. Multi-modal PDF figure extraction (QMD doc_elements)

**What QMD does.** `doc_elements` extracts tables, figures, equations from PDFs.

**Why it doesn't fit.** EKRS's inputs are text (PDF/Word/DWG already converted to JSONL by the Parser). Figure-level data is not in the EKRS IR. Adding it would require IR V3 and Parser coordination — multi-quarter effort.

### 15. GGUF model management (QMD llm.ts + 3 GGUF models)

**What QMD does.** Auto-downloads GGUF models from HuggingFace on first use, cached in `~/.cache/qmd/models/`. Manages 3 separate models with different lifecycle (embedding, reranker, expansion).

**Why it doesn't fit (mostly).** EKRS already uses ONNX bge-m3 (Phase 8 T8-3a vendored into Docker image). GGUF vs ONNX is a tooling choice, not a feature. The vendored-model pattern is what EKRS already adopted. The *auto-download* pattern doesn't fit because EKRS can't reach HuggingFace at runtime in restricted networks.

**Partial fit.** If EKRS adds a reranker (item 2), the GGUF management code from QMD could be a reference for the model lifecycle (cache invalidation on version change, idle-context disposal after 5 min).

---

## Recommended Phase 9 scope candidates (prioritized)

| # | Candidate | Effort | Risk | Iron-Rule compatibility | Notes |
|---|-----------|--------|------|-------------------------|-------|
| 1 | FTS5 BM25 alongside Qdrant vector | medium | low | R5 ok (not graph DB), R8 ok (filter illegal status) | Smallest, highest ROI. Solves exact-token recall. |
| 2 | Markdown-aware chunking (boundary scoring) | small | low | R1 ok (still produces source_span/block_id) | Additive to existing scope-aware chunker. |
| 3 | MCP tool adapter (read-only first) | medium | medium | All R1-R8 ok (read path) | High visibility. Write tools require more auth thought. |
| 4 | Schema versioned migrations (aiosqlite) | small | low | All ok | Prevents drift. ~100 LOC. |
| 5 | Cross-encoder reranker | large | medium | R2 needs review (reranker is in retrieval, not solver) | Gated on latency budget measurement. |

Items 1-4 fit comfortably into a Phase 9 scope. Item 5 is a separate, later phase.

### Out-of-scope reminders

- **QMD's web search, wiki LLM generation, doc_write, multi-modal PDF extraction, multi-format backends** — explicitly NOT portable to EKRS. Listed in §"Does NOT fit" above.
- **PD-1 through PD-6** (Qdrant optimization, multi-region, large-scale batch, mTLS, audit.log remote archival, bge-m3 vendor distribution) remain frozen per §6.2 until production deployment.

---

## Closing notes

### Why research-only at this stage

Phase 8 closed 2026-07-24 (tag force-moved per Decision §3). Phase 9 has no scope yet. This document is the *input* to a Phase 9 planning conversation, not the planning output. Concrete tasks (T9-1, T9-2, …) require:
- A `phase9-scope.md` plan doc with locked decisions (per the Phase 7 / Phase 8 precedent).
- `ekrs-handbook.md §6.3` for Phase 9+ deferrals.
- An updated CHANGELOG + `phase9` tag at closure (Decision §3 pattern).

### Open questions for Phase 9 planning

1. **FTS5 sync mechanism — [已裁决见 ADR] 同步双写。** Qdrant upsert + FTS5 insert
   在同一摄取事务内完成（同步双写）。FTS 写入失败通过 `fts_sync_failed` 审计事件
   记录，不阻断 Qdrant 写入。异步事件驱动方案推迟至 Phase 10+（如需更高吞吐再评估）。
   裁决依据：[`2026-07-24-phase9-cross-doc-adjudication.md`](2026-07-24-phase9-cross-doc-adjudication.md) 冲突 2。
2. **MCP transport choice.** stdio (per-client process — model reload each time, expensive) vs Streamable HTTP (shared daemon — Phase 8 T8-3a's image baseline pinning already requires daemon patterns). QMD's docs show both; what's right for EKRS?
3. **Reranker model selection.** qwen3-reranker-0.6b is QMD's choice. Engineering documents have CJK content; should EKRS evaluate multilingual rerankers? Phase 7 T3a baseline pin pattern applies.
4. **Schema migration framework choice.** Hand-rolled (~100 LOC) vs Alembic / yoyo-migrations (more deps, more learning curve). EKRS's aiosqlite is small enough that hand-rolled is probably sufficient.
5. **Boundary scoring integration point.** Does the existing `chunk_blocks()` get a new parameter, or does a separate `chunk_blocks_v2()` ship? Phase 7 T7's embedding cache pattern (TDD-red-green with explicit feature flag) suggests the former.

---

## Cross-references

- Phase 8 plan doc (closed 2026-07-24): `docs/superpowers/plans/2026-07-23-phase8-scope.md`
- Iron Rules: `ekrs-handbook.md` §1 (R1-R8)
- Phase 6+ deferral freeze: `ekrs-handbook.md` §6.1
- Post-deploy tech debt: `ekrs-handbook.md` §6.2 (PD-1 through PD-6)
- Decision §3 (delivered-state tag pattern): `docs/superpowers/plans/2026-07-23-phase7-scope.md` row #3
- MinerU-Document-Explorer source: `/tmp/mineru-explorer/` (research clone, not in EKRS repo)
- MinerU architecture: <https://github.com/opendatalab/MinerU-Document-Explorer/blob/main/docs/architecture.md>
- MinerU MCP: <https://github.com/opendatalab/MinerU-Document-Explorer/blob/main/docs/mcp.md>