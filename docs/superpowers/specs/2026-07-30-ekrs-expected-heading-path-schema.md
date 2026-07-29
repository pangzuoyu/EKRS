# EKRS 期望 schema: `DocumentBlockIR.metadata.heading_path`

**Date**: 2026-07-30
**Author**: EKRS Phase 10 T10b-2 trigger test
**Status**: DRAFT — pending doc-to-md sign-off
**Parent**: `2026-07-30-doc-to-md-heading-path-coordination.md`

## Purpose

Pin down what EKRS consumes from `metadata.heading_path` so doc-to-md can populate it correctly. This is the EKRS-side half of the contract; the other half (mapping algorithm from `outline.json`) is doc-to-md's responsibility per the coordination report.

## 1. Field location

```
DocumentBlockIR (shared/ekrs_shared/models.py:36)
└── metadata (shared/ekrs_shared/models.py:25)
    └── heading_path: Optional[List[str]] = None
```

The field already exists in the EKRS schema — no model change needed. doc-to-md just needs to populate it.

## 2. Semantics: heading hierarchy ONLY

`heading_path` is the **heading hierarchy** — the ordered list of heading titles from root to the leaf that contains this block.

**It is NOT** the doc-type classifier (R4 scope_priority). That is a separate concept (see §6 "Out of scope").

### Type contract

```python
heading_path: Optional[List[str]]
# - None         : doc has no outline tree (current behavior for ~10% docs)
# - []           : doc HAS outline tree, but this block is not enclosed by any heading
# - ["A", "B"]   : this block is enclosed by root heading "A" then child "B"
# - ["A", "B", "C"]: block enclosed by A > B > C (full chain)
```

The chunker (`chunker.py:79`) does `block.metadata.heading_path or []`, so `None` and `[]` are equivalent at the chunker boundary.

## 3. Ordering convention

**Root first, leaf last.**

Example for a 3-level heading tree:
```markdown
# Top Heading             ← scope_path[0]
## Mid Heading            ← scope_path[1]
### Leaf Heading          ← scope_path[2]
content text...
```

Expected `heading_path` for the content block: `["Top Heading", "Mid Heading", "Leaf Heading"]`.

## 4. Depth semantics

- **Minimum depth**: 1 (when block is directly under a top-level heading).
- **Maximum depth**: unbounded (mirrors `outline.json` depth). Depth-1 = `#` heading, depth-2 = `##`, etc.
- **Nested headings**: when a block is in the region where two headings overlap (e.g., block sits under both "Mid" at level 2 and "Leaf" at level 3), use the **deepest enclosing heading path** — i.e., include the leaf. This gives chunker Boundary 2 the finest-grained scope-change signal.

## 5. Boundary cases (testable)

| Case | Expected `heading_path` | Reason |
|------|------------------------|--------|
| Block before any heading (e.g., title page) | `[]` or `None` | Not enclosed by any heading |
| Block directly under top-level heading | `["Top Heading"]` | Single-element path |
| Block in 5-level nested section | `["A", "B", "C", "D", "E"]` | Full chain to leaf |
| Block in nested region with multiple overlapping headings | Use deepest (most specific) | Best scope signal |
| Doc has no `outline.json` tree | All blocks: `None` | Current behavior; no action |
| Block IS the heading itself (`type: "header"`) | The heading's own path (empty list for root headings, parent's path for child) | Headings are their own anchor |

## 6. Out of scope for THIS schema

**Doc-type classifier (R4 scope_priority)** is **NOT** represented in `heading_path`. The retriever's `_SCOPE_PRIORITY_MAP = {national: 100, industry: 80, enterprise: 60, project: 40, reference: 20}` maps `chunk.scope_path[0]` against these five labels. That mapping is currently a no-op (default 40) because headings like "ARTICLE 27" don't match.

This is a **separate doc-to-md gap** — doc-type must come from a different signal (e.g., `metadata.scope_classifier` or document-level field). Coordination item #1 in the report flags this. **Do NOT** prepend "national/" / "industry/" / etc. to `heading_path` as a workaround — that conflates two semantics and breaks `_extract_provision_id` (which scans heading text for clause numbers).

## 7. Worked example

**Input outline** (from `outline.json`):
```json
{
  "tree": [
    {"id": "h1", "title": "ARTICLE 27", "level": 1, "parent_id": null,
     "start_block_id": "blk_0010", "end_block_id": "blk_0099"},
    {"id": "h2", "title": "LEAK TESTING STANDARDS", "level": 2, "parent_id": "h1",
     "start_block_id": "blk_0020", "end_block_id": "blk_0050"},
    {"id": "h3", "title": "Selection of System", "level": 3, "parent_id": "h2",
     "start_block_id": "blk_0030", "end_block_id": "blk_0040"}
  ]
}
```

**Expected `data.jsonl`**:
```jsonl
{"doc_id": "...", "block_id": "blk_0015", "type": "text", "metadata": {"heading_path": ["ARTICLE 27"]}, ...}
{"doc_id": "...", "block_id": "blk_0025", "type": "text", "metadata": {"heading_path": ["ARTICLE 27", "LEAK TESTING STANDARDS"]}, ...}
{"doc_id": "...", "block_id": "blk_0035", "type": "text", "metadata": {"heading_path": ["ARTICLE 27", "LEAK TESTING STANDARDS", "Selection of System"]}, ...}
{"doc_id": "...", "block_id": "blk_0005", "type": "text", "metadata": {"heading_path": []}, ...}
```

## 8. EKRS consumer code references

| Consumer | File:Line | Behavior when `heading_path` is non-empty |
|----------|-----------|-------------------------------------------|
| Chunker Boundary 2 (scope change flush) | `rag/ekrs_rag/ingestion/chunker.py:660-690` | Triggers `_route_accumulated_group` when path differs |
| `chunker._get_scope_path` | `rag/ekrs_rag/ingestion/chunker.py:77-79` | Returns `heading_path or []`; this is the single point of fallback |
| `evidence_builder._extract_provision_id` | `rag/ekrs_rag/constraint_engine/evidence_builder.py:118-130` | Scans for `\d+\.\d+(?:\.\d+)?` clause number pattern |
| `retriever._scope_priority` | `rag/ekrs_rag/retrieval/retriever.py:237-243` | Maps `scope_path[0]` against doc-type map (currently no-op — see §6) |
| `FTSManager` indexed column | `rag/ekrs_rag/retrieval/fts_manager.py:64, 249` | `" ".join(scope_path)` for column-restricted MATCH |
| Qdrant payload | `rag/ekrs_rag/retrieval/qdrant_client.py:219` | Stored as `scope_path` array |

## 9. Verification expectations (post-fix)

EKRS-side validation once doc-to-md ships the fix:

1. **Coverage check**: 30-doc random sample — expect ≥ 80% of blocks with `heading_path` non-empty (aligns with outline coverage). Currently 0.1%.
2. **Chunk scope_path non-empty rate**: chunker output should have `scope_path != []` for ≥ 50% of chunks. Currently ~0%.
3. **Golden regression**: existing 50-case golden set must still pass (scope_path is supplemental signal, not required).
4. **Boundary 2 trigger frequency**: log how often scope-change fires in a stress run — expect non-zero (currently always 0).
5. **Re-run T10b-2 trigger test**: expect `heading_less %` to drop from 100% to < 50%. cond#2 (avg tokens > 614.4) may now drive the decision.

## 10. Title normalization rules

doc-to-md retains the raw heading title as it appears in `outline.json`. EKRS does NOT normalize — chunks store the exact title string.

Rationale: `_extract_provision_id` needs the clause number visible in the title text. Stripping "ARTICLE 27" → "27" would lose the clause context. Title normalization (e.g., "5.2.3 Scope" → "5.2.3") is the consumer's job, not the producer's.

Coordination item #4 in the parent report flags "title normalize prefix" — this spec asserts: **doc-to-md does NOT normalize. EKRS consumers handle their own normalization if needed.**

## 11. Change policy

- This spec is the EKRS contract. Changing it requires coordination, not unilateral decision.
- New field additions to `metadata` go through `shared/ekrs_shared/models.py` Pydantic model + R1 Iron Rule review (source_span / block_id / context_window).
- The chunker's `or []` fallback is intentionally permissive (`None` and `[]` both work) — do not tighten the schema to require `[]`.

## 12. Open questions for doc-to-md

1. **Algorithm choice**: range-based (block_id in `[start, end]`) vs tree-walk. Range-based is O(n) per block; tree-walk is O(depth) per node. For 1000-block docs, range-based wins.
2. **Heading region overlap**: when two headings at different levels share a block (e.g., parent heading spans whole section, child spans subsection), both ranges contain the block. Use the deepest — see §4.
3. **Partial overlap on start/end**: if a heading's `end_block_id` is the same as a sibling heading's `start_block_id`, no ambiguity. But if ranges overlap non-nested (e.g., heading A spans 10-50, heading B spans 30-40), this is a doc-to-md data quality issue — flag for doc-to-md to fix, not for EKRS to disambiguate.