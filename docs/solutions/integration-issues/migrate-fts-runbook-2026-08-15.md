# FTS5 v1 → v2 Migration Runbook — Phase 12 F3

**Date**: 2026-08-15
**Status**: script shipped + tests pass; production run gated on Q5 trigger conditions

## Purpose

One-time rebuild of the FTS5 index from Qdrant payload. Required because:

- Phase 12 T3 added `form_fields_text` / `column_headers_text` columns to FTS5 schema (v2).
- FTS5 `ALTER TABLE ADD COLUMN` is NOT supported (SQLite limitation) — full rebuild required.
- 745 historical docs were ingested before T3 shipped, so their FTS5 rows are v1-only.

## Trigger Conditions (Q5)

Run this script AFTER all of:

- [ ] verify_reingest.py 4-step P2 fix shipped (commit `ccd5726`)
- [ ] Phase 12 T1-T5 shipped (commits `090d74f` + `6b726bd`)
- [ ] 7-day soak period observed (no FTS5 / Qdrant regressions in production)
- [ ] User explicit approval (Q5 final gate)
- [ ] Low-traffic window scheduled (off-peak hours for the corpus)

## Prerequisites

- Qdrant running with all 745 historical docs indexed
- RAG service stopped (or paused) so ConsistencyChecker is not actively writing
- FTS5 SQLite file at `/app/rag/fts.sqlite` (default; override via `--fts-db`)
- `QDRANT_HOST` / `QDRANT_GRPC_PORT` env vars set

## Procedure

### 1. Dry-Run (mandatory first step)

```bash
cd /home/pangzy/code_project/EKRS
python scripts/migrate_fts_v1_to_v2.py --dry-run
```

Expected output:

```
migration_start: docs=745 apply=False limit=None schema_version=2
doc_dry_run: doc-001 (1/745) chunks=42
doc_dry_run: doc-002 (2/745) chunks=58
...
migration_done: success=745 failure=0 apply=False
```

Verify `success=745 failure=0` before proceeding. If failures appear:

- Check Qdrant connectivity (`curl http://localhost:6333/collections`)
- Check audit.log for `qdrant_read_failed` events
- Re-run dry-run (idempotent — same docs, same payload)

### 2. Apply (production migration)

```bash
python scripts/migrate_fts_v1_to_v2.py --apply
```

Script behavior during apply:

- Sets `is_migration_in_progress()=True` → ConsistencyChecker 5-min drift checks skipped (F2)
- `replace_doc` retries 3× with 100/200/400ms backoff on `sqlite3.OperationalError: database is locked` (D3)
- Per-doc progress logged to stdout
- Per-doc failures logged but do not abort the run; partial-failure convergence via re-run

Expected output:

```
migration_start: docs=745 apply=True limit=None schema_version=2
doc_migrated: doc-001 (1/745) chunks_written=42
doc_migrated: doc-002 (2/745) chunks_written=58
...
migration_done: success=745 failure=0 apply=True
```

Exit codes:

- `0` = all docs migrated
- `1` = unrecoverable error (Qdrant unreachable, FTS5 init failure)
- `2` = partial success — re-run script to retry the failed docs

### 3. Verification

```bash
# Verify FTS5 v2 columns populated
sqlite3 /app/rag/fts.sqlite \
  "SELECT COUNT(*) FROM blocks_fts WHERE form_fields_text != '[]';"
# expected: 7-12% of total rows (form-aware docs only)

# Verify Qdrant ↔ FTS5 count parity
sqlite3 /app/rag/fts.sqlite "SELECT COUNT(*) FROM blocks_fts WHERE status='active';"
# compare against Qdrant total (rag/ekrs_rag/retrieval/qdrant_client.py:count_points)
```

If counts diverge > 0 after migration:

- Re-run `python scripts/migrate_fts_v1_to_v2.py --apply` (idempotent — converges)
- Check audit.log for `fts_consistency_drift` events

### 4. Post-Migration

1. Restart RAG service (it was paused during migration)
2. Spot-check a form-aware doc query:
   ```bash
   curl -X POST http://localhost:8000/v1/constraints \
     -H "X-Parser-Token: $PARSER_TOKEN" \
     -d '{"query": "LOT 49 SYSTEM NO", ...}'
   ```
   Expected: boosted scope score (FORM_FIELD_WEIGHT=0.9) for the LOT 49 chunk
3. Update CHANGELOG.md `[phase12]` section with migration completion timestamp

## Rollback

If migration produces bad FTS5 state:

1. Stop RAG service
2. Restore v1 backup: `cp /app/rag/fts.sqlite.v1-backup /app/rag/fts.sqlite`
3. Restart RAG service — fallback to v1 (T1-T2 still work; form_fields payload is preserved)

Backups are taken automatically by `ConsistencyChecker`'s audit rotation logic, but
explicit pre-migration backup is recommended:

```bash
cp /app/rag/fts.sqlite /app/rag/fts.sqlite.v1-backup-$(date +%Y%m%d)
```

## See Also

- Phase 12 plan: `docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md`
- T3 FTS5 schema: `rag/ekrs_rag/retrieval/fts_manager.py`
- F2 migration state: `rag/ekrs_rag/concurrency/migration_state.py`
- D3 retry decorator: `scripts/migrate_fts_v1_to_v2.py:retry_on_sqlite_busy`
- T4 retriever boost: `rag/ekrs_rag/retrieval/retriever.py:_scope_priority`