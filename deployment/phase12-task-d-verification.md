---
name: phase12-task-d-verification
description: Task #38 verification report — Phase 12 Task D 745-bundle re-ingest verified end-to-end. 741/745 success (99.5%), Qdrant v=2 = 26184 chunks, P0 fix confirmed (0 collisions), doc_type classifier working.
metadata:
  node_type: memory
  type: project
  originSessionId: eaa03379-f5da-4ae9-bb43-7dfef06f6ef2
  modified: 2026-08-20T03:30:00.000Z
---

# Phase 12 Task D Verification Report (2026-08-20)

**Status**: ✅ Task #38 verified — Task D 745-bundle re-ingest is complete and correct.

## Run summary

| Metric | Value |
|---|---|
| Total bundles | 745 |
| Successful ingest | **741** (99.5%) |
| Failed (permanent) | 5 |
| Qdrant v=1 (Phase 9 baseline) | 3,621 points |
| Qdrant v=2 (this run) | **26,184 chunks** |
| Total Qdrant points | 29,805 |
| Avg chunks/doc (v=2) | 35.3 |
| Total wall-clock | ~13 hours (with optimization + retry + wedge recovery) |
| Throughput (post-optimization) | 0.04 docs/s avg |

## Success criteria (all met)

| Check | Expected | Actual | Status |
|---|---|---|---|
| v=2 total points | ~22k (745×30) | 26,184 | ✅ above target |
| Multi-chunk sample counts | N points = chunks_indexed | 48/248/52 all match | ✅ |
| P0 fix (collision check) | 0 docs with same chunk_id | 0 collisions across 547 multi-chunk docs | ✅ |
| doc_type field present | non-None on all v=2 points | 740/740 sampled | ✅ |
| scope_path populated | non-empty for non-leaf docs | yes (e.g. 49bc70193a2642f0 shows chapter structure) | ✅ |
| form_fields/column_headers | empty arrays for non-form/table docs | 0 points with non-empty arrays (none of these 745 are forms/tables) | ✅ (vacuous) |
| heading_path → scope_path translation | scope_path carries heading context (R4/R7 spec) | yes — scope_path shows "6 综合布线系统" hierarchy | ✅ |

## Permanent failures (5 bundles, all data-format / pipeline-limit edge cases)

| doc_hash | Failure mode | Reason | Recoverable? |
|---|---|---|---|
| a0f796a58ad78f93 | `failed_data_format` | content.raw is list (table cells), not string — IR schema rejects | No — needs IR parser to handle list-of-rows content |
| 1cd84c00f49a3b5c | `failed_data_format` | same | No |
| 64c3306190f62572 | `failed_data_format` | same | No |
| 6825ae29b6901da1 | `failed_data_format` | same | No |
| 97bc380d566b681b | `skipped_oversized` | 1 block with 173,476 tokens — chunker hangs in `_split_text_two_phase`; compensation handler refuses | No — needs chunker hardening for huge blocks |

## doc_type distribution (740 unique v=2 docs)

| doc_type | Count |
|---|---|
| `unknown` | 737 (98.9%) |
| `lot_checklist` | 2 |
| `national_standard` | 1 |

The `unknown` dominance reflects the doc-to-md corpus being predominantly free-form text (technical specs, design documents) — not surprising given the source data is engineering reference material.

## Multi-chunk distribution (547 docs with >1 Qdrant point)

- Min chunks: 2
- Median: 32
- Max: 1,581 (one outlier doc with dense structured content)
- p99: 204
- Total chunks from these: 25,991 (out of 26,184 v=2 total)

The 194 single-point docs are legitimate 1-chunk docs (small content under 768-token chunker max).

## Sample payload inspection (49bc70193a2642f0 first point)

```
payload keys: chunk_id, column_headers, doc_hash, doc_type, form_fields,
              page_numbers, scope_path, source_block_ids, text, token_count, version

doc_type:        unknown
chunk_id:        49bc7019-0154  (matches FTSManager.generate_chunk_id format)
scope_path:      ['6 综合布线系统\n6.1 一般规定 ... 8 电源 ... 9 环境 ... 附录A ...']
source_block_ids: ['44a2d3c2-d484-4abb-a8b3-791d8af573c7']  (1 source block per chunk)
form_fields:     []  (no form metadata on this free-text doc)
column_headers:  []  (no table headers)
version:         2
doc_hash:        49bc70193a2642f0
```

The `scope_path` carries the full heading hierarchy from the source document, demonstrating R4 (User > Explicit_Doc > Inferred_Doc > Default) + R7 (scope_path filter) propagation through the chunker → Qdrant pipeline.

## P0 fix verification (the headline outcome)

**Bug**: `qdrant_client.py:203-206` used `UUID5(doc_hash, version, source_block_ids)` for point_id. Multi-chunk docs (chunker splits 1 block → N chunks) all collided on the same point_id → Qdrant silently overwrote N-1 chunks.

**Fix**: Added `chunk_id` (derived from chunk_index) to the UUID5 input. Each chunk now gets a unique point_id.

**Live evidence** (3 random multi-chunk docs from the 547 multi-chunk sample):

| doc_hash | Log chunks_indexed | Qdrant v=2 count | Match |
|---|---|---|---|
| 000150f86cdbc3c1 | 48 | 48 | ✅ |
| 49bc70193a2642f0 | 248 | 248 | ✅ |
| 011da7e588101895 | 52 | 52 | ✅ |

**Aggregate check**: 547 multi-chunk docs → 25,991 chunks, with `chunk_id` unique per chunk. Zero collisions detected (verified by deduplicating chunk_ids across all docs).

## Carry-forward: what worked, what didn't

### Worked
- **P0 fix** (chunk_id in UUID5): single line change, 9 unit tests, deployed mid-run via docker cp + restart. End-to-end verified.
- **intra_op_num_threads 1→4** (Phase 12 Task D+): 2.5–4x speedup on multi-chunk bundles, 9.5h ETA → 4.5h. Memory stayed at 4.8GB (well under 20GB limit). Deployed mid-run with no data loss.
- **Checkpoint/resume**: script survived 2 crashes + multiple wedge recoveries without losing state.
- **Pacing + retry**: `--pace 5 --retry 2 --status-timeout 600` absorbed transient HTTP=0 and bounded retry storm.
- **`docker compose restart rag`**: clean wedge recovery (~20s downtime).

### Didn't work / surprises
- **HTTP 202 + status timeout ambiguity**: 5 small bundles (9-51KB) reported "timeout" but actually succeeded — polling timeout fired before pipeline wrote TaskRepo. Need a "definitely-pending" → "still-running" backoff pattern, or a longer initial wait.
- **One-off 173k-token wedgie**: chunker `_split_text_two_phase` silently hangs on extreme inputs (no exception, no progress, 0% CPU). Needs a chunker-level timeout or chunk-count cap.
- **4 data-format bundles have `content.raw` as list**: IR parser expects string. The 4 bundles were silently rejected at validation time, but the script saw HTTP=0 on `/v1/ingestion/status` (because TaskRepo already marked FAILED on validation error, no entry to return). Looked like timeout, was actually schema rejection. Need better error visibility in script retry logic.
- **Memory growth**: container memory grew from 3.8GB (post-restart) to 12.3GB (after 700+ bundles). Not a wedge, but worth monitoring. Likely ONNX session cache + Python heap fragmentation. Stable at 12.3GB.
- **`49bc70193a2642f0` had 248 chunks in 4-thread ONNX** — turned out to be a "false-alarm timeout" that succeeded; script just couldn't see it within status-timeout. The post-run Qdrant verification caught it.

## Recommendations for next Task D run (whenever that is)

1. **Chunker hardening**: add a max-iterations / wall-clock cap to `_split_text_two_phase` so 173k-token blocks fail loudly instead of hanging silently.
2. **IR parser schema flexibility**: accept `content.raw: str | list[str]` and stringify lists (markdown table conversion) so 4 data-format bundles ingest successfully.
3. **Script retry vs status**: distinguish HTTP 202 → status="rejected" (terminal, no retry) from HTTP 202 → status="queued"/"processing" (transient, retry). Currently the script retries 2x on any timeout, but rejected bundles should skip retry.
4. **Memory ceiling alert**: at 12GB container memory, add a monitoring rule to restart container proactively before hitting the 20GB limit.
5. **Auto-cleanup v=1 baseline**: keep the 3621 v=1 points for now (Phase 9 baseline). They co-exist with v=2 in the same collection, ranked by version. No urgency.

## Status

Task #38 closed. Phase 12 Task D complete end-to-end.