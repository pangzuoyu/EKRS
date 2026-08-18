# Phase 12 Task C — Doc-Type Classifier at Ingest (Design Spec)

**Date**: 2026-08-18
**Phase**: 12 Task C (per [[phase12-doc-to-md-followup-plan]])
**Status**: Design approved (chat 2026-08-18 21:30 GMT+8). Awaiting user review of written spec.
**Tag**: absorbs under `phase12` closure at `d9a602c` per post-closure incremental pattern

## Goal

Surface R4 scope-priority signal at ingest time via a filename-derived `doc_type` classifier, so `_scope_priority` can use a stronger, decoupled signal than the currently-default project=0.4 (which 99% of chunks inherit because `scope_path[0]` is empty for non-heading docs).

## Why now

Phase 12 Task B (heading_path verification) closed today (doc-to-md commit `02f5caa`, EKRS commits `d66f8a3` / `2f51cdc` / `85b1f04` / `6b726bd` / `090d74f`). That fix unblocks Q4 priority ordering for **future** ingestions. **Legacy** chunks (pre-heading_path fix) still have empty `scope_path` and need a separate signal — `doc_type` from filename is that signal.

The classifier is **EKRS-internal** (Decision 1 in [[phase12-doc-to-md-followup-plan]]): no doc-to-md coordination, no schema coupling. Rules are tweakable via JSON config without re-deploying doc-to-md.

## Locked design decisions (2026-08-18, in chat)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Source of `source_filename` | Read from `{output_path}/index.json` `file_name` field (zero parser protocol change) |
| 2 | Rule storage + default | JSON config `doc_classifier_rules.json`, loaded at startup; default doc_type="unknown" maps to project=40 (preserves R4 default) |
| 3 | Backward compat strategy | `_scope_priority` reads `chunk.doc_type` first; falls back to `chunk.scope_path[0]` for legacy chunks (pre-Task-C) |
| 4 | Initial regex set | 5 rules: national_standard, industry_standard, enterprise_spec, project_spec, lot_checklist + default unknown |

## Initial regex rule set

Stored in `rag/ekrs_rag/ingestion/doc_classifier_rules.json`, loaded as a Pydantic model `DocClassifierRules`:

```python
class DocClassifierRule(BaseModel):
    pattern: str       # regex source; compiled via `re.compile(pattern, re.IGNORECASE)`
    doc_type: str      # tag emitted on match
    priority: int      # R4 priority (0–100)

class DocClassifierRules(BaseModel):
    rules: List[DocClassifierRule]
    default: DocClassifierRule
```

JSON example:

```json
{
  "rules": [
    {"pattern": "^GB[/_-T]?\\d",         "doc_type": "national_standard",  "priority": 100},
    {"pattern": "^HG[/_-T]?\\d",         "doc_type": "industry_standard",  "priority": 80},
    {"pattern": "^Q[/_-]?\\d",           "doc_type": "enterprise_spec",    "priority": 60},
    {"pattern": "^SA[-_]",               "doc_type": "project_spec",       "priority": 40},
    {"pattern": "Lot\\s*\\d+|NCR|DCN|Check[- ]?list|Exception\\s*List",
                                          "doc_type": "lot_checklist",      "priority": 60}
  ],
  "default": {"doc_type": "unknown", "priority": 40}
}
```

**First-match wins** (rules evaluated top-to-bottom; document assigned the first matching rule's doc_type).

**Config path**: default = `rag/ekrs_rag/ingestion/doc_classifier_rules.json` (sibling to `doc_classifier.py`). Override via env var `EKRS_DOC_CLASSIFIER_RULES_PATH` (consistent with Phase 5.5 `FTS_DB_PATH` env-var pattern).

## Architecture

```
NEW   rag/ekrs_rag/ingestion/doc_classifier.py         (~150 LOC)
NEW   rag/ekrs_rag/ingestion/doc_classifier_rules.json (~20 LOC)
NEW   rag/tests/unit/test_doc_classifier.py            (~200 LOC, ~12 tests)
EDIT  shared/ekrs_shared/models.py                    (+1 line: Chunk.doc_type)
EDIT  rag/ekrs_rag/ingestion/chunker.py                (+5 lines: chunk_blocks kwarg + propagation)
EDIT  rag/ekrs_rag/ingestion/pipeline.py               (+10 lines: read index.json → classify)
EDIT  rag/ekrs_rag/retrieval/retriever.py              (+10 lines: _scope_priority + _payload_to_chunk)
```

## Data flow

```
Parser ─POST /v1/ingestion/notify──▶ {output_path, doc_hash, version}
                                          │
Pipeline.ingest() ─read output_path/index.json──▶ file_name
                                          │
                                  classify(file_name) → doc_type
                                          │
chunk_blocks(blocks, doc_hash, version, doc_type=...)  ◀── stamp each Chunk.doc_type
                                          │
QdrantManager.upsert_chunks(chunks)  ◀── serializes all Chunk fields incl. doc_type
                                          │
Qdrant payload + FTS5 row  ◀── doc_type persisted
                                          │
[future retrieve] ─_payload_to_chunk──▶ Chunk(doc_type="national_standard", ...)
                                          │
                                  _scope_priority:
                                    chunk.doc_type → priority_map[doc_type]
                                    if None: chunk.scope_path[0] → _SCOPE_PRIORITY_MAP
```

## Error handling

| Condition | Behavior |
|-----------|----------|
| `index.json` missing | WARNING + `doc_type="unknown"` (pipeline does NOT fail) |
| `file_name` missing from `index.json` | WARNING + `doc_type="unknown"` (pipeline does NOT fail) |
| Invalid regex in JSON config | **fail-fast at module import** (Pydantic-settings validator); misconfig caught in CI |
| Classifier raises at ingestion | Caught at pipeline boundary, WARNING, default `"unknown"`; ingestion does NOT fail |
| Legacy chunk (pre-Task-C, no `doc_type` in payload) | `_payload_to_chunk` populates `doc_type=None`; `_scope_priority` falls back to `scope_path[0]` (current Phase 6B behavior preserved) |

## R4 mapping (doc_type → priority)

```python
_DOC_TYPE_PRIORITY: Dict[str, int] = {
    "national_standard":  100,  # 国标
    "industry_standard":   80,  # 行标
    "enterprise_spec":     60,  # 企标
    "lot_checklist":       60,  # 项目现场清单 (项目内高优先级)
    "project_spec":        40,  # 项目/合同 spec
    "unknown":             40,  # 默认 — 保留 Phase 6B baseline
}
```

`_scope_priority` reads `chunk.doc_type` → maps to `{0.0–1.0}` (divided by 100). Falls back to `chunk.scope_path[0]` lookup when `doc_type is None`.

## Testing strategy (TDD)

### Unit tests (12 new in `test_doc_classifier.py`)

1. Each of 5 rules matches a representative filename
2. No-match → default `unknown` (priority 40)
3. Case-insensitive regex (`gb150.pdf` matches `^GB`)
4. JSON config loads with Pydantic-settings (env-var override supported)
5. Invalid regex → Pydantic `ValidationError` at module import (fail-fast)
6. Empty filename → `unknown`
7. Missing `index.json` → `unknown` (pipeline integration point)
8. `doc_type → priority` mapping (5 rules + default, 6 assertions)
9. First-match wins (rule order matters)
10. Rule with `priority=60` for `lot_checklist` (overrides default 40)
11. JSON config env-var override: `EKRS_DOC_CLASSIFIER_RULES_PATH=/custom/path`
12. Multiple patterns in one rule (e.g. `NCR` and `DCN` both match)

### Chunker integration (2 new)

1. `chunk_blocks(..., doc_type="national_standard")` stamps each produced `Chunk.doc_type`
2. Default `doc_type=None` preserves pre-Task-C behavior (golden set parity)

### Retriever integration (2 new)

1. `_scope_priority(chunk(doc_type="national_standard"))` → 1.0 (highest)
2. `_scope_priority(chunk(doc_type=None))` falls back to `chunk.scope_path[0]` lookup (legacy chunk path)

### Regression

- **Golden set**: 208 pass baseline → MUST stay 0 regression
- **mypy**: no new errors
- **Q4 priority ordering** (stretch — only meaningful post-Task-D re-ingest): lot_checklist should outrank project_spec

## Out of scope (deferred)

- **Phase 12 Task D** (745-doc re-ingest): not part of this task. Task C enables Q4 priority ordering for future ingestions; legacy chunks remain at `doc_type=None` until Task D re-ingest runs.
- **Real-infra Q4 recall@10 measurement**: deferred to Task D + 8/20 联调 (need 15 bundles in RAG).
- **ground_truth.pick_heading_chunk un-defer**: Task B closed today (heading_path now in Qdrant payload), but un-deferring the heuristic is a separate concern, not in Task C scope.
- **doc_classifier_rules.json auto-reload**: rules load once at module init; hot-reload would require file-watcher or re-import pattern. Out of scope until needed.

## Tag discipline

No new tag — absorbs under `phase12` closure at `d9a602c` per prior post-closure incremental pattern (T10b-3, T10d Td.1+2, T11-3, T11-4, T12-A, FTS_DB_PATH, ground-truth).

## Open questions

None. All design decisions locked in chat 2026-08-18 21:30 GMT+8.

## Implementation plan

After user spec review approval, invoke `superpowers:writing-plans` to generate the implementation plan with TDD step-by-step.

## Related

- [[phase12-doc-to-md-followup-plan]] — parent plan (Task C step list)
- [[phase12-recall-baseline-ground-truth]] — Task B output (8/20 verification pending)
- [[phase12-closure]] — `phase12` anchor at `d9a602c`
- [[phase12-recall-baseline-prep]] — FTS_DB_PATH (Task A), closed at `1e5b66a`
- `rag/ekrs_rag/ingestion/pipeline.py:167-171` — chunk_blocks call site (will gain `doc_type` arg)
- `rag/ekrs_rag/ingestion/chunker.py` — chunk_blocks signature (will gain `doc_type` kwarg)
- `rag/ekrs_rag/retrieval/retriever.py:268-294` — `_scope_priority` (will read `doc_type` first)
- `shared/ekrs_shared/models.py:186-216` — `Chunk` model (will gain `doc_type` field)
- `rag/ekrs_rag/retrieval/qdrant_client.py` — `upsert_chunks` (auto-serializes `doc_type` from Chunk; no edit)