# MinerU/QMD × Karpathy LLM Wiki → EKRS Deep-Dive Extensions

> Research artifact — **research only, no implementation**.
> Date: 2026-07-24
> Companion to: [`2026-07-24-mineru-explorer-feature-mapping.md`](2026-07-24-mineru-explorer-feature-mapping.md)
> Purpose: Deep-dive on the three functional areas — **Retrieve / Deep Read / Ingest** — mapping Karpathy's LLM Wiki pattern + QMD's concrete implementation onto EKRS's Iron Rules R1-R8 + existing Phase 1-8 architecture.
> Source materials:
> - Karpathy gist: <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f> (75 lines, fetched 2026-07-24)
> - QMD code: `/tmp/mineru-explorer/src/{hybrid-search,search}.ts`, `backends/{types,query-utils}.ts`, `wiki/{index-gen,lint,log}.ts`

---

## Part 1 — 检索 (Retrieve)

### 1.1 Karpathy's view

Karpathy's gist treats the search engine as an **optional CLI/MCP tool** layered on top of the wiki:

> "At some point you may want to build small tools that help the LLM operate on the wiki more efficiently. A search engine over the wiki pages is the most obvious one — at small scale the index file is enough, but as the wiki grows you want proper search. **qmd** is a good option: it's a local search engine for markdown files with hybrid BM25/vector search and LLM re-ranking, all on-device. It has both a CLI (so the LLM can shell out to it) and an MCP server (so the LLM can use it as a native tool)."

In Karpathy's pattern, search is **one of several tools the LLM agent uses** while operating on the wiki. The wiki is the source of truth; search is a retrieval helper.

### 1.2 QMD's three search modes

From `hybrid-search.ts`:

| Mode | Function | Pipeline |
|------|----------|----------|
| Hybrid | `hybridQuery(store, query, opts)` | BM25 probe → strong-signal check → query expansion (lex/vec/hyde) → parallel FTS+vector → RRF fusion (k=60, original×2 weight, top-rank bonus) → top 40 candidates → chunk + best-chunk selection → cross-encoder rerank → position-aware blending (75%/60%/40%) |
| Vector-only | `vectorSearchQuery(store, query, opts)` | expandQuery (filter to vec/hyde) → searchVec for each variant → dedup by filepath → sort |
| Structured | `structuredSearch(store, searches, opts)` | caller pre-expands (lex/vec/hyde list) → type-routed FTS/vector → RRF → rerank → blend |

Three notable patterns:

1. **Strong-signal short-circuit**: `topScore ≥ 0.85 && gap ≥ 0.15` → skip expansion entirely. Saves 1-2 LLM calls when the BM25 top is clear.
2. **Chunk-first reranking** (`hybrid-search.ts:289-313`): chunk each candidate, score chunks by term overlap with the query, pick best chunk per doc, rerank *chunks* not full bodies. Avoids the O(tokens) trap.
3. **Grep fallback** (`backends/query-utils.ts:15-33`): when embeddings are unavailable, fall back to grep on the first 3 query terms. The query never fails entirely.

### 1.3 EKRS extension candidates — Retrieve

EKRS's current retrieval: Qdrant vector search with composite scope-aware scoring (`vector_score * (1 + scope_priority)`). No keyword index, no reranker, no expansion.

#### 1.3.1 FTS5 BM25 alongside Qdrant vector (Phase 9 priority 1)

**Direct mapping from QMD.** What QMD contributes that EKRS doesn't have:

- A **parallel retrieval path** with different recall characteristics (lexical vs semantic).
- A **strong-signal probe** (BM25 first; skip expansion if top hit is clear).
- **Engineering-specific term patterns**: QMD's BM25 handles general text; EKRS needs to handle `1.6MPa`, `A312-TP316`, `GB/T 12459`, `20#` — these are *exact* identifiers that benefit from lexical matching.

**Implementation sketch.**

```sql
-- New table, mirrored from Qdrant payload
CREATE VIRTUAL TABLE blocks_fts USING fts5(
    block_id UNINDEXED,
    text,
    scope_path,
    status UNINDEXED,  -- R8: filter illegal status
    tokenize = 'unicode61 remove_diacritics 2'  -- [已裁决见 ADR] 见下方分词器说明
);
```

**[已裁决见 ADR] 中文分词器选择**：Phase 9 FTS5 表使用 `tokenize='unicode61
remove_diacritics 2'`（不使用 porter 词干提取，因为 porter 对中文无效且可能破坏
CJK token）。`unicode61` 对 CJK 字符做逐字切分，对英文做空格+标点切分。
可选启用 jieba 前置分词（`config.yaml: retrieval.fts_tokenizer: jieba`），
在摄取时对 chunk.text 做词级预分词再写入 FTS5，提升中文工程术语召回率。
Phase 9a 先用 unicode61 验证基线，Phase 9b 评估是否启用 jieba。
裁决依据：[`2026-07-24-phase9-cross-doc-adjudication.md`](2026-07-24-phase9-cross-doc-adjudication.md) 不一致 2。

Sync mechanism (the main design question): sync via the existing `AuditWriter` pipeline. Each Qdrant upsert emits a paired FTS insert in the same atomic write. Phase 7 T1's `qdrant_write_failed` integration test pattern extends to `fts_write_failed`.

**Iron Rule check.** R5 ok (FTS5 is a SQLite virtual table, not a graph DB). R8 ok (filter `status != "illegal"` at the index layer, never trim authority). R3 ok (BM25 just enriches recall; the three-gate pipeline still runs). R2 ok (BM25 is a lookup, not part of the solver).

**Strong-signal short-circuit NOT applicable to EKRS.** Unlike QMD (which can short-circuit and skip LLM expansion), EKRS must always run the full three-gate pipeline in non-strict mode, and in strict mode (R6) must always execute the gate. The probe is fine, but the short-circuit is forbidden.

**Effort.** Medium (3-5 tasks). New table, sync in pipeline, score fusion via RRF k=60, integration test against Qdrant+SQLite, golden set regression.

#### 1.3.2 Cross-encoder reranker (Phase 9 candidate, gated on latency)

**Direct mapping from QMD.** The `queryWithEmbeddings` function in `backends/query-utils.ts:51-172` shows the *exact* pattern: cosine score → top-K*3 candidates → rerank → reranker score wins. Fallback: when reranker unavailable, use cosine scores.

**The O(tokens) trap is solved by chunk-first reranking** (QMD's pattern). For EKRS, this means:
1. Qdrant returns top-K=40 candidates (vector-only path, no scope filter — that comes later).
2. For each candidate, get the best-matching `block_id` (already in Qdrant payload).
3. Rerank the 40 blocks (not full documents) via cross-encoder.
4. Apply position-aware blending: top 1-3 blocks = 75% reranker / 25% composite score (preserve scope matches); top 11+ = 40% reranker / 60% composite score (trust structural modifier).

**Iron Rule check.** R2 (solver is pure): the reranker is in *retrieval*, not the solver. The solver receives a `RetrievalResult` with reranked blocks; it doesn't know the reranker exists. R2 ok.

**[已裁决见 ADR] strict 模式门控**：`strict=True` 时**强制跳过重排**（不可配置覆盖）。
Cross-encoder 模型推理引入浮点级非确定性，与 R2 的确定性要求直接冲突。strict 模式
是 R2 的硬保证入口，任何允许 strict+rerank 的代码路径都是 R2 违规的潜在入口。
裁决依据：[`2026-07-24-phase9-cross-doc-adjudication.md`](2026-07-24-phase9-cross-doc-adjudication.md) 冲突 1。

**Tradeoff.** T8-5 baseline chunker p99 = 279µs per doc. Reranker adds ~50-200ms per (query, block) pair at K=40 ≈ 2-8s per query. This eats into the 5s solver budget if applied to the constraint pipeline. **Mitigation:** apply reranker only on the top-40 candidate set after BM25 + vector + scope filter; the solver then sees the reranked blocks.

**Effort.** Large (model integration + cache key composition + perf baseline + benchmark validation).

#### 1.3.3 Query expansion (defer — measure first)

QMD's query expansion (`qmd-query-expansion-1.7B-q4_k_m`) generates lex/vec/hyde variants. **Engineering queries are usually precise**, not exploratory. "Design pressure of the reflux drum" doesn't benefit from expansion the way "tell me about heat exchangers" does.

**Where expansion MIGHT fit EKRS:**

- **Ambiguity resolution.** Engineering acronyms (MT = Material Thickness vs Metric Tons) are common. Expansion could disambiguate before retrieval.
- **But:** disambiguation should happen at the *agent* layer (out-of-band), not in EKRS's pipeline (R2).
- The MCP `structuredSearch` API design from QMD exactly fits this: caller expands, EKRS executes deterministically.

**Recommended.** Defer query expansion. If FTS5 + reranker still show recall gaps in the golden set, *then* consider expansion via the MCP `structuredSearch` path (caller-supplied expansions). EKRS doesn't need its own expansion model.

#### 1.3.4 Position-aware blending as a pattern for scope_priority

QMD's position-aware blending (75%/60%/40% for top 1-3/4-10/11+) is a **structural pattern** that could inform EKRS's scope-aware scoring:

```python
# Current EKRS
composite_score = vector_score * (1 + scope_priority)

# Possible Phase 9 evolution (NOT a recommendation yet — design doc needed)
if rank <= 3:
    composite_score = 0.75 * vector_score + 0.25 * scope_modifier
elif rank <= 10:
    composite_score = 0.60 * vector_score + 0.40 * scope_modifier
else:
    composite_score = 0.40 * vector_score + 0.60 * scope_modifier
```

The intuition: when scope_priority brings a doc to the top, trust the structural signal more (it's a strong indicator). When scope_priority is just one of many factors pulling a doc up, trust the vector similarity more.

**Iron Rule check.** R4 (Context priority: User > Explicit_Doc > Inferred_Doc > Default) — the position-aware blending preserves this: high-priority docs rank highly via scope priority; the blending just adjusts *how much* of the final score is structural vs semantic.

**Recommended.** Phase 9 design-doc candidate, gated on a measurement showing the current scoring has scope-priority bias problems. Otherwise the current formula is fine.

#### 1.3.5 Intent hint parameter

QMD's `HybridQueryOptions.intent` is a domain hint that steers expansion + chunk selection. Maps loosely to EKRS's `scope_path`:

| QMD | EKRS |
|-----|------|
| `intent = "web page load times"` | `scope_path = "/national/GB-150"` |
| Steers expansion to relevant synonyms | Steers scope-priority weighting |
| Used to disable strong-signal short-circuit when intent != top BM25 | Used to filter retrieval to scope subtree |

**Recommended.** No new code — just document the analogy in the Phase 9 design doc. The scope_path parameter already serves the same role.

---

## Part 2 — 精读 (Deep Read)

### 2.1 QMD's DocumentBackend interface

From `backends/types.ts:64-77`:

```typescript
export interface DocumentBackend {
  readonly format: "md" | "pdf" | "docx" | "pptx";
  getToc(filepath, docid): Promise<TocSection[]>;          // Tree of headings
  readContent(filepath, docid, addresses, maxTokens?): Promise<ContentSection[]>;
  grep(filepath, docid, pattern, flags?): Promise<GrepMatch[]>;
  query(filepath, docid, queryText, topK?): Promise<QueryChunk[]>;
  extractElements?(filepath, docid, addresses?, query?, elementTypes?): Promise<ContentElement[]>;
}
```

The unifying concept is the **address space** — every read operation works on format-specific addresses:

| Format | Address example |
|--------|-----------------|
| Markdown | `line:45-120` |
| PDF | `pages:12-15` |
| DOCX | `section:3` |
| PPTX | `slide:4` |

The 5 operations form a complete in-document exploration toolkit:
- **getToc**: structural map
- **readContent**: read at addresses from TOC/grep/query
- **grep**: regex find within doc
- **query**: semantic find within doc
- **extractElements**: tables/figures/equations

### 2.2 EKRS's current state

| Capability | QMD | EKRS |
|------------|-----|------|
| `block_id` (per-chunk identifier) | docid + chunk seq | block_id (R1) |
| Read a specific chunk | readContent(addresses) | NOT exposed via API |
| TOC / heading tree | getToc | NOT extracted |
| Intra-document search | doc_query | NOT supported (cross-doc only) |
| Regex within document | doc_grep | NOT supported |
| Element extraction (tables/figures/equations) | doc_elements | NOT in scope (Parser's job) |
| Cross-doc search | hybridQuery | `/v1/constraints` |

EKRS has the *primitive* (block_id, scope_path, source_span) but exposes only cross-document retrieval. The Deep Read tools are missing.

### 2.3 EKRS extension candidates — Deep Read

#### 2.3.1 GET /v1/blocks/{block_id} (small effort, high value)

Read a specific block by ID. Returns: `block_id`, text, source_span, scope_path, context_window, doc_id, block_index.

**Use case.** After a `/v1/constraints` query returns block_ids in its evidence, an agent drills into the actual evidence block to verify the constraint before citing it.

**Iron Rule check.** R1 (returns source_span + block_id + context_window — already mandated). R7 (returns scope_path — preserved). R2 (read path; not solver).

**Effort.** Small (1 task). New route handler + aiosqlite or Qdrant point lookup by block_id.

#### 2.3.2 GET /v1/documents/{doc_id}/toc (medium effort)

Heading/section tree. Returns nested `TocSection` (mirrors QMD's interface).

**Implementation.** During ingestion, the chunker could extract heading metadata (`markdown #/##/###` patterns or PDF bookmarks via the Parser's JSONL). Store in a new `document_toc` table keyed by `doc_id` + heading path.

**Use case.** Agents navigate large engineering specs by section, not by full-doc scan. "Show me section 4.2.3 of PID-001" returns the section's blocks without re-running vector search.

**Iron Rule check.** R1 (sections mapped to source_span). R7 (scope_path propagated at section level). R2 (read path).

**Effort.** Medium. Requires Parser coordination if PDF bookmark metadata is desired; for markdown-style headings, chunker-side extraction suffices.

#### 2.3.3 Intra-document search (small effort)

QMD's `doc_query` semantic-restricted to one document. For EKRS: filter existing `/v1/constraints`-style retrieval by `doc_id`.

**Use case.** "Find all mentions of pressure in this one PID diagram spec." Constrained case of cross-document search.

**Implementation.** Add `doc_id` filter to existing Qdrant retrieval. The constraint pipeline isn't invoked — this is a debug/inspection tool.

**Effort.** Small. ~1 task.

#### 2.3.4 GET /v1/blocks?doc_id=X&pattern=Y (small effort)

QMD's `doc_grep`. Regex search within one document, returns matching blocks.

**Use case.** "Look for 'A312' in this one spec." Exact identifier match, faster than vector search.

**Implementation.** New endpoint that filters blocks by doc_id and matches the pattern against `block_text`. Could use SQLite LIKE or a per-doc regex.

**Effort.** Small. ~1 task. Possibly combined with intra-doc search.

#### 2.3.5 Element extraction (NOT EKRS scope)

QMD's `doc_elements` extracts tables/figures/equations from PDFs. **EKRS receives already-parsed JSONL from the Parser** (Phase 4 architecture). Element extraction is the Parser's job. Adding it to EKRS would violate the Parser↔RAG separation.

**Recommended.** Explicit exclusion. Document in the Deep Read report.

### 2.4 Mapping QMD's address system to EKRS

QMD's format-specific addresses (`line:45-120`, `pages:12-15`, `section:3`, `slide:4`) suggest EKRS could adopt a similar convention:

```
ekrs://doc/{doc_id}/block/{block_id}                    # exact block
ekrs://doc/{doc_id}/section/{section_path}              # section range
ekrs://doc/{doc_id}/source/{source_span}                # source_span (R1)
```

This gives the MCP adapter a uniform address space without forcing EKRS to know about format internals. The `block_id` is the canonical address; `section` and `source_span` are derivable.

---

## Part 3 — 摄取 (Ingest)

### 3.1 Karpathy's LLM Wiki pattern

Three-layer architecture:

```
┌─────────────────────────────────────────┐
│ Raw sources (immutable, source of truth)│  ← EKRS: Parser JSONL
├─────────────────────────────────────────┤
│ Wiki (LLM-maintained, persistent)       │  ← EKRS: audit.log + constraint repo
├─────────────────────────────────────────┤
│ Schema (CLAUDE.md / AGENTS.md)          │  ← EKRS: ekrs-handbook.md (Iron Rules)
└─────────────────────────────────────────┘
```

Three operations:

- **Ingest**: read raw source → extract key info → update entity pages → update index → log entry.
- **Query**: search wiki → read pages → synthesize answer with citations → file good answers back.
- **Lint**: health-check — contradictions, stale claims, orphan pages, missing cross-refs, data gaps.

Two navigability helpers:
- `index.md` — content catalog (categories, one-line summaries)
- `log.md` — chronological activity log

Karpathy's central thesis: **"The wiki is a persistent, compounding artifact. The cross-references are already there. The contradictions have already been flagged. The synthesis already reflects everything you've read."**

### 3.2 QMD's wiki implementation

From `src/wiki/`:

| Module | Function | Purpose |
|--------|----------|---------|
| `index-gen.ts` | `generateWikiIndex(db, opts)` | Auto-generates `index.md` grouped by top-level directory |
| `log.ts` | `appendLog / queryLog / getLogStats / formatLogAsMarkdown` | 5 ops: ingest / update / lint / query / index |
| `lint.ts` | `lintWiki(db, opts)` | Orphan pages, broken wikilinks, missing pages, hub pages, stale pages, source-stale pages |

Plus the link graph in `links.ts` (forward/backward link parsing for wikilinks, markdown, URLs).

### 3.3 EKRS extension candidates — Ingest

This is the most sensitive area. Many Karpathy patterns violate EKRS's Iron Rules; the analysis below calls out where they conflict.

#### 3.3.1 "Wiki" concept → EKRS already has it (no new entity)

Karpathy's wiki is the LLM-maintained artifact. EKRS's equivalent is the **combination of**:
- `audit.log` (chronological, immutable, append-only) — like QMD's `log.md`
- The constraint repository in Qdrant + aiosqlite — like QMD's wiki pages
- The golden set — like QMD's curated entries

EKRS doesn't need a new "wiki" entity. The Iron Rules already enforce the wiki properties Karpathy values (immutability via audit.log, source-of-truth via R1's source_span).

#### 3.3.2 "LLM writes the wiki" → violates R2

Karpathy's central idea: **"You never (or rarely) write the wiki yourself — the LLM writes and maintains all of it."**

This **directly violates EKRS R2** (solver is a pure function — no I/O, no state, no side effects). An LLM in the ingest or synthesis path is non-deterministic.

**Resolution:** EKRS uses LLM as a *human-in-the-loop curator* (via the planned MCP adapter — Claude / Cursor), not as a pipeline component. The LLM agent invokes EKRS APIs; EKRS executes deterministically.

This is a fundamental design divergence:
- **QMD**: human curates + LLM maintains autonomously
- **EKRS**: system is autonomous + deterministic; LLM only acts via explicit MCP/API calls

Both are valid; they serve different purposes. EKRS's choice is deliberate — engineering constraints must be reproducible (the same query returns the same interval every time, even across deploys).

#### 3.3.3 Ingest operation → already implemented (no new code)

QMD's ingest = "read source → update wiki pages → update index → log entry". EKRS's `IngestionPipeline`:
- Reads Parser JSONL
- Extracts numeric hints via `hint_extractor` (Phase 2)
- Solves intervals via `interval_solver` (Phase 2)
- Writes to Qdrant
- Writes to audit.log
- Compensates on failure (Phase 7 T3)

The "update 10-15 wiki pages" pattern is what EKRS does: one source → many constraint entries → audit log entries. No LLM in the path.

#### 3.3.4 Query operation → already implemented (no new code)

QMD's query = "search wiki → synthesize answer → file good answers back". EKRS's `/v1/constraints`:
- Searches via Qdrant vector + scope filter (Phase 3)
- Extracts hints from blocks (Phase 2)
- Solves intervals via `portion` (Phase 2)
- Returns structured constraint intervals per parameter

"File good answers back" maps to the audit log — every solve is recorded. The synthesis step (Karpathy's "natural language answer") is NOT done by EKRS; the structured output is the answer.

#### 3.3.5 Lint operation → NEW Phase 9 candidate (medium effort)

QMD's lint checks: orphans, broken links, missing pages, hub pages, stale pages, source-stale pages. EKRS equivalents:

| QMD concept | EKRS equivalent | Iron Rule |
|-------------|-----------------|-----------|
| Orphan pages (no inbound links) | Orphan hints: numeric_hint in Qdrant with no matching interval in last 1000 solves | R5 ok (no graph DB) |
| Broken wikilinks | Broken scope_path: hint references scope that no longer exists in scope registry | R5 ok |
| Missing pages | Missing context: hint has empty context_window where R1 requires non-empty | R1 |
| Hub pages (high inbound) | Hot scope_path: scope with high constraint density (operational telemetry) | None (telemetry) |
| Stale pages (last_updated > threshold) | Stale constraints: constraint hasn't been re-verified in N days | None |
| Source-stale pages | Constraint-vs-source drift: source_span refers to a block that's been superseded | R1 (source_span must point to current block) |

**API sketch.** `GET /v1/admin/lint?stale_days=30&hub_threshold=50` returns `WikiLintResult`-shaped JSON.

**Iron Rule check.** R5 (uses scope_path hierarchy queries, not a graph DB). R8 (filter illegal status, never trim authority — i.e., report lint findings but don't auto-delete). R2 (lint is read-only, no solver involvement).

**Effort.** Medium. 1-2 tasks. Mostly SQL queries against existing tables.

#### 3.3.6 index.md catalog → NEW Phase 9 candidate (medium effort)

QMD's `index.md`: content catalog with one-line summaries per page.

**EKRS equivalent.** `GET /v1/admin/index` returns a catalog of all ingested documents with metadata (title, scope_path, block_count, last_ingested, constraint_count).

**The summary generation problem.** QMD uses the document's first line as the summary. For EKRS, the Parser JSONL already provides document titles. The summary question is: do we need a longer description?

- If summaries = first 100 chars of the document: trivial, no LLM needed.
- If summaries = LLM-generated: violates R2.

**Recommendation.** Ship catalog without summaries first (just title + metadata). Add summaries only if a concrete user need surfaces. The agent can read the first block via `GET /v1/blocks/{block_id}` for context if needed.

**[已裁决见 ADR] LLM 摘要 Phase 归属**：Phase 9 **不实现** LLM 摘要生成。
catalog 端点仅返回 title + metadata（来自 Parser JSONL），不生成 LLM 摘要。
LLM 摘要/查询扩展推迟至 Phase 10+。Phase 9 保留此接口设计作为 no-op stub
（接口存在但 summary 字段始终返回 null）。
裁决依据：[`2026-07-24-phase9-cross-doc-adjudication.md`](2026-07-24-phase9-cross-doc-adjudication.md) 冲突 3。

**Effort.** Medium. 1-2 tasks. Endpoint + aiosqlite query + golden set regression.

#### 3.3.7 Cross-references → already exists (no new code)

QMD's wikilinks `[[like this]]` → link graph with forward/backward references.

EKRS's `scope_path` already provides hierarchical cross-references:
- `/national/GB-150/Part-1/Section-3.2` is a hierarchical path
- "Show all constraints in this scope subtree" is `WHERE scope_path LIKE '/national/GB-150/%'`

What's missing in EKRS:
- **Bidirectional queries**: "Which scopes reference this constraint?" — currently you have to scan all hints.
- **Scope-to-scope relationships**: e.g., "Which enterprise standard supersedes this industry standard?"

**API sketch.** `GET /v1/scope/{scope_path}/constraints` lists all constraints within a scope subtree.

**Iron Rule check.** R5 (scope_path is a hierarchy, not a graph). R7 (every hint has scope_path).

**Effort.** Small (1 task). New endpoint + existing scope_path LIKE query.

#### 3.3.8 Schema layer → already exists (no new code)

Karpathy: "The schema tells the LLM how the wiki is structured... it's what makes the LLM a disciplined wiki maintainer."

EKRS: `ekrs-handbook.md` IS the schema. The Iron Rules R1-R8 are the operational doctrine. The agent (Claude) reads the handbook to understand how to operate on EKRS.

**Mapping:** Already done. No new code.

#### 3.3.9 "File good answers back into the wiki" → already partially fits

Karpathy: "A comparison you asked for, an analysis, a connection you discovered — these are valuable and shouldn't disappear into chat history."

EKRS: The audit log records every solve. The golden set is curated manually (regression tests). What's missing: a way for the MCP agent to *persist* its discoveries for future sessions.

**API candidate.** `POST /v1/admin/notes` — append a freeform note (X-Admin-Key gated). Stored alongside audit log. Retrievable via `GET /v1/admin/notes?since=...`.

**Iron Rule check.** R2 (notes are not solver output; they're operator annotations). R8 (notes don't trim authority).

**Effort.** Small (1 task). Aiosqlite `notes` table + 2 endpoints.

**Use case.** A Claude agent investigating a constraint can leave notes for the next session: "This constraint frequently conflicts with X — investigate scope priority."

#### 3.3.10 The 5 operations in QMD's wiki, mapped to EKRS

| QMD operation | EKRS equivalent | Status |
|---------------|-----------------|--------|
| `wiki_ingest` | `IngestionPipeline.reparse()` + `audit.log` | ✓ exists |
| `wiki_update` | (same as ingest — QMD distinguishes "first time" vs "update") | ✓ exists |
| `wiki_lint` | `/v1/admin/lint` (new) | Phase 9 candidate |
| `wiki_query` | `/v1/constraints` + `/v1/blocks/{block_id}` (new) | ✓ exists + Phase 9 candidate |
| `wiki_index` | `/v1/admin/index` (new) | Phase 9 candidate |

5 of 5 mapped. 3 are existing; 2 are Phase 9 candidates.

---

## Part 4 — Cross-cutting considerations

### 4.1 MCP integration amplifies all three areas

From the prior report and the QMD MCP design (`docs/mcp.md`): the MCP adapter becomes the *carrier* for Retrieve / Deep Read / Ingest tools. Concrete MCP tool mapping:

| MCP tool | QMD source | EKRS endpoint |
|----------|-----------|---------------|
| `query` | `hybridQuery` | `POST /v1/constraints` |
| `get` | `get(filepath, docid)` | `GET /v1/blocks/{block_id}` (Phase 9) |
| `multi_get` | `multi_get` | `GET /v1/blocks?block_ids=a,b,c` (Phase 9) |
| `status` | `status` | `GET /healthz` + `GET /v1/admin/index` (Phase 9) |
| `doc_toc` | `getToc` | `GET /v1/documents/{doc_id}/toc` (Phase 9) |
| `doc_read` | `readContent` | `GET /v1/blocks?addresses=...` (Phase 9) |
| `doc_grep` | `grep` | `GET /v1/blocks?doc_id=X&pattern=Y` (Phase 9) |
| `doc_query` | `query` | `POST /v1/constraints?doc_id=X` (Phase 9) |
| `wiki_lint` | `lintWiki` | `GET /v1/admin/lint` (Phase 9) |
| `wiki_index` | `generateWikiIndex` | `GET /v1/admin/index` (Phase 9) |
| `wiki_log` | `queryLog` | `GET /v1/admin/audit?since=...` (already exists, refactor) |
| `wiki_ingest` | (collection-level) | `POST /v1/ingestion/notify` (already exists) |

This shows that **Phase 9 is primarily an MCP integration phase**, with each tool exposing an existing or new HTTP endpoint.

### 4.2 R2 (deterministic solver) is the fundamental tension

Karpathy's pattern: LLM in the critical path, writing the wiki.
EKRS: solver is pure; LLM is not allowed in the solve path.

**Resolution.** EKRS uses LLM as a *human-in-the-loop curator* (via MCP), not as a pipeline component. The LLM agent invokes EKRS APIs; EKRS executes deterministically.

This divergence is captured in the handbook §1 R2 rationale:
> "Solver is a pure function — no I/O, no state, no side effects. Determinism is a hard requirement for engineering use cases (the same query returns the same interval every time, even across deploys)."

QMD's LLM-maintained wiki is great for personal knowledge management where determinism is not critical. EKRS's deterministic backend is necessary for engineering constraint validation.

### 4.3 R5 (no graph DB) affects the link graph

QMD uses a real link graph (forward/backward links).
EKRS uses a scope_path hierarchy (not a graph).

Scope priority (national > industry > enterprise > project > reference) is **hierarchical**, not graph-shaped. Cross-references between scopes (e.g., "this enterprise standard supersedes that industry standard") would need a graph DB if implemented naively.

**Resolution.** Scope_path queries simulate cross-references via path-prefix matching:
- "All constraints in scope subtree X": `scope_path LIKE 'X/%'`
- "Constraints with this scope or any ancestor": `scope_path IN (ancestors of X)`
- "Which scopes have higher priority than X": pre-computed list, looked up

No graph DB needed. SQLite + path queries suffice.

---

## Part 5 — Recommended Phase 9 scope (updated with deep-dive)

| # | Candidate | Area | Effort | Iron Rule | Phase 9 fit |
|---|-----------|------|--------|-----------|-------------|
| 1 | FTS5 BM25 alongside Qdrant | Retrieve | medium | R5 ok, R8 ok | ✓ |
| 2 | Boundary-scored chunking | Retrieve (chunker) | small | all | ✓ |
| 3 | MCP tool adapter (read-only first) | Cross-cutting | medium | all (read path) | ✓ |
| 4 | Schema versioned migrations | Ingest (ops) | small | all | ✓ |
| 5 | `GET /v1/blocks/{block_id}` | Deep Read | small | R1, R7 | ✓ |
| 6 | `GET /v1/documents/{doc_id}/toc` | Deep Read | medium | R1, R7 | ✓ |
| 7 | Intra-doc search + grep | Deep Read | small | all | ✓ |
| 8 | `GET /v1/admin/lint` | Ingest (Karpathy Lint) | medium | R5, R8 | ✓ |
| 9 | `GET /v1/admin/index` | Ingest (Karpathy Index) | medium | needs summary strategy | ✓ |
| 10 | `GET /v1/scope/{scope_path}/constraints` | Ingest (cross-refs) | small | R5, R7 | ✓ |
| 11 | `POST /v1/admin/notes` | Ingest (Karpathy "file back") | small | R2, R8 | ✓ |
| 12 | Cross-encoder reranker | Retrieve | large | R2 ok, needs latency | later |

Items 1-11 fit Phase 9 comfortably. Item 12 is a separate, later phase.

**Estimated Phase 9 size.** ~10-11 tasks (some group naturally: 5+6 as one chunking+MCP release; 7 as another small release; 8+9+10 as the "Karpathy Lint/Index/Cross-ref" release; 11 standalone).

---

## Part 6 — Open questions (refined from prior report)

1. **FTS5 sync mechanism** (same as before): co-write vs async event.
2. **MCP transport choice** (same as before): stdio vs Streamable HTTP daemon.
3. **NEW — Deep Read release strategy.** Ship `/v1/blocks/{block_id}` first (smallest), then `/v1/documents/{doc_id}/toc` (needs ingestion change), then intra-doc search + grep as a single "explore" release. Or bundle all into one Phase 9 MCP release?
4. **NEW — Lint semantics.** What does "orphan hint" mean operationally? Hint in Qdrant with no matching interval in the last N solves? Hint with no downstream consumer (no `constraints_query` referencing it)? Need a design doc.
5. **NEW — Index catalog summaries.** Skip summaries (just titles), or first-N-chars from Parser, or operator-curated via `POST /v1/admin/notes`?
6. **NEW — Karpathy divergence documentation.** Should EKRS add a `docs/superpowers/research/2026-07-24-karpathy-divergence.md` explaining why "LLM maintains the wiki" is forbidden by R2, and how MCP + deterministic backend is the EKRS alternative? Useful for future Claude agents reading the handbook.
7. **NEW — Phase 9 task ordering.** Which items unblock others? Items 1, 5, 11 are independent. Item 3 (MCP) unblocks all others being usable from agents. Item 8+9+10 share lint/index SQL queries — could share a task.

---

## Part 7 — Closing notes

### 7.1 Why this deep-dive matters

The prior `2026-07-24-mineru-explorer-feature-mapping.md` gave the high-level map. This document goes deeper into the three functional areas — Retrieve, Deep Read, Ingest — that correspond to Karpathy's three primary LLM Wiki operations.

The deep-dive produces:
- A concrete **Retrieve pipeline design** (FTS5 + Qdrant hybrid, with strong-signal analysis specific to EKRS's R6).
- A **Deep Read API surface** (`/v1/blocks/{block_id}`, `/v1/documents/{doc_id}/toc`, intra-doc search, grep).
- An **Ingest operation mapping** showing that 3 of 5 Karpathy operations are already implemented in EKRS, and 2 new ones (lint, index) are Phase 9 candidates.
- An **MCP integration plan** that exposes all three areas as agent-callable tools.

### 7.2 The fundamental design divergence

The most important insight from this deep-dive:

> **Karpathy's LLM Wiki pattern** puts the LLM in the critical path, autonomously maintaining the wiki.
> **EKRS's design** puts the LLM outside the critical path, as an MCP-driven curator calling deterministic APIs.

Both designs are valid. EKRS's choice is forced by R2 (deterministic solver). QMD's choice is forced by its personal-knowledge-base use case (the user wants the LLM to do the work).

Future EKRS agents reading this document should understand: **EKRS is a deterministic backend, not an LLM-maintained wiki**. The agent's job is to call EKRS APIs; EKRS's job is to return reproducible results.

### 7.3 Cross-references

- Prior report: [`2026-07-24-mineru-explorer-feature-mapping.md`](2026-07-24-mineru-explorer-feature-mapping.md)
- Iron Rules: `ekrs-handbook.md` §1 (R1-R8)
- Phase 6+ deferral freeze: `ekrs-handbook.md` §6.1
- Post-deploy tech debt: `ekrs-handbook.md` §6.2 (PD-1 through PD-6)
- Decision §3 (delivered-state tag pattern): `docs/superpowers/plans/2026-07-23-phase7-scope.md` row #3
- Phase 8 closure (current state): `docs/superpowers/plans/2026-07-23-phase8-scope.md`
- Karpathy gist: <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
- QMD architecture: <https://github.com/opendatalab/MinerU-Document-Explorer/blob/main/docs/architecture.md>
- QMD MCP: <https://github.com/opendatalab/MinerU-Document-Explorer/blob/main/docs/mcp.md>
- QMD source (research clone): `/tmp/mineru-explorer/`