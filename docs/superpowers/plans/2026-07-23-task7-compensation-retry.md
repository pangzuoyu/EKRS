# Task 7 — CompensationHandler real retry (Phase 7 T3 spec)

> Status: planning → ready for TDD
> Date: 2026-07-23
> Author: Claude (Sonnet)
> Parent: [Phase 7 scope](2026-07-23-phase7-scope.md) §Decisions (locked)
> Predecessor: Phase 4 `CompensationScanner` + Phase 6A `compensation_retry` audit schema

---

## Background

`rag/ekrs_rag/main.py:41` ships with `COMPENSATION_HANDLER_IMPLEMENTED = False`.
`main.py:44-47` defines `_stub_compensation_handler` that only `logger.warning`s.

**The `compensation_retry` audit events fire correctly** (Phase 7 T2 at commit `41c2d54`),
but the handler is a no-op → orphan `PENDING`/`RUNNING` tasks accumulate in
`aiosqlite` and never recover without manual intervention (`/v1/ingestion/replay`).

This task wires the handler to **re-trigger ingestion** so orphan tasks actually
self-heal.

---

## Design

### Handler contract change

```python
# Before (current):
Handler = Callable[[dict[str, Any]], Awaitable[None]]

# After:
Handler = Callable[[dict[str, Any]], Awaitable[bool]]  # True=success, False=failed
```

Returning `True` / `False` (instead of just raising) lets the scanner emit
`reingest_outcome` cleanly. Exceptions still propagate to existing
`handler_failed` path.

### `IngestionPipeline.ingest()` entry point

The handler needs a way to re-trigger the original workflow. Per Phase 7
Decisions §1: **universal re-run** via reconstructed notification.

New helper in `IngestionPipeline`:

```python
async def reparse(
    self,
    source_path: str,
    doc_hash: str,
    version: int,
    callback_url: str | None,
    force: bool = False,
) -> IngestionOutcome:
    """Re-parse JSONL at source_path and run the ingest pipeline end-to-end.

    When force=False (default): if content_hash at source_path matches the
    stored payload_sha256, returns early with IngestionOutcome(rag_status="duplicate").
    When force=True: always re-runs.
    """
```

This is essentially what `/v1/ingestion/replay` does today (see
`routes/ingestion.py:282-364`) but **without the http context** — callable from
the compensation scanner directly.

### `_stub_compensation_handler` → real handler

Replace at `main.py:44`:

```python
async def _compensation_reparse_handler(task: dict[str, Any]) -> bool:
    """Re-run ingestion for an orphan task via pipeline.reparse().

    Reads source_path + payload_sha256 + version from the task row and
    invokes IngestionPipeline.reparse(). Returns True on success,
    False if the JSONL is missing or content_hash mismatches (without --force).
    """
    pipeline: IngestionPipeline = ...  # resolved via app.state at lifespan init
    try:
        outcome = await pipeline.reparse(
            source_path=task["source_path"],
            doc_hash=task["doc_id"],
            version=task.get("version", 1),
            callback_url=task.get("callback_url"),
            force=False,  # honor hash check
        )
        return outcome.rag_status in ("success", "duplicate")
    except Exception:
        logger.exception("Compensation reparse failed for %s", task["request_id"])
        return False
```

Then `main.py:41` flips to `True`:

```python
COMPENSATION_HANDLER_IMPLEMENTED = True
```

### Required `compensation_retry` schema change

Two new fields added to `_EVENT_SCHEMAS["compensation_retry"]`:

```python
"compensation_retry": {
    "request_id",          # existing (required)
    "reingest_outcome",    # NEW: "success" | "failed" | "duplicate" | "skipped"
    "reingest_duration_ms",  # NEW: int, wall-clock per re-ingest
},
```

**Backward compatibility**: `AuditIndex` (the read path) is defensive — old audit
log entries without these fields will default to `outcome=None`, `duration_ms=0`
when parsed. Only the write path validates required fields.

`compensation.py:_emit_compensation_event` is updated to take `outcome` +
`duration_ms` kwargs and pass them through.

For the existing `claim_race_lost` / `handler_not_wired` reasons, there is no
re-ingest → emit `outcome="skipped"`, `duration_ms=0`. This keeps the schema
uniformly required across all reasons.

### Audit example

```json
{
  "timestamp": "2026-07-23T14:32:01.123Z",
  "event": "compensation_retry",
  "request_id": "abc123",
  "attempt": 2,
  "reason": "retry_invoked",
  "reingest_outcome": "success",
  "reingest_duration_ms": 1520
}
```

### Operator CLI (Phase 7 Decisions §1 implementation)

```bash
# Re-run ingest for a doc, skipping hash check:
ekrs reparse --doc-id abc123 --force

# Re-run only if hash mismatch detected (default):
ekrs reparse --doc-id abc123
```

This is a thin wrapper around `pipeline.reparse()` — separate from the
compensation scanner (which is automatic, not operator-initiated).

---

## Files touched

| File | Change |
|------|--------|
| `rag/ekrs_rag/main.py` | Flip `COMPENSATION_HANDLER_IMPLEMENTED = True`; replace stub handler with `_compensation_reparse_handler` |
| `rag/ekrs_rag/concurrency/compensation.py` | `_emit_compensation_event` accepts `outcome` + `duration_ms`; `scan()` measures elapsed and reports outcome |
| `rag/ekrs_rag/ingestion/pipeline.py` | New `reparse(source_path, doc_hash, version, callback_url, force=False)` method |
| `rag/ekrs_rag/main.py:_EVENT_SCHEMAS` | Add `reingest_outcome` + `reingest_duration_ms` to `compensation_retry` required fields |
| `rag/ekrs_rag/cli.py` (new) | `ekrs reparse --doc-id X [--force]` CLI entry point |
| `rag/tests/unit/test_compensation.py` | Updated for new signature + outcome/duration emission |
| `rag/tests/integration/test_compensation_reparse.py` (new) | End-to-end: orphan task → scanner → handler → re-ingest → COMPLETED |
| `docs/CHANGELOG.md` | Phase 7 T3 entry |

---

## TDD test list (RED → GREEN → IMPROVE)

### Unit tests

1. `test_handler_returns_true_on_success`
   - Stub handler that returns True → scan emits `reingest_outcome="success"`.
2. `test_handler_returns_false_on_failure`
   - Stub handler that returns False → scan emits `outcome="failed"`, marks FAILED.
3. `test_handler_raises_propagates_to_existing_handler_failed_path`
   - Stub handler that raises → existing handler_failed emit still fires, AND
     new `outcome="failed"` field is also emitted (existing + new in same call).
4. `test_compensation_event_includes_duration_ms`
   - Mock handler with `asyncio.sleep(0.05)` → emitted duration_ms ≥ 40.
5. `test_compensation_event_skipped_emits_zero_duration`
   - claim_race_lost path → `outcome="skipped"`, `duration_ms=0`.

### Integration tests

6. `test_compensation_reparse_recovers_orphan_task` (real Qdrant + aiosqlite)
   - Insert a PENDING task with valid `source_path` JSONL but no recent update.
   - Run `scanner.scan()` → task transitions to COMPLETED, Qdrant has the chunks.
7. `test_compensation_reparse_skips_on_hash_match`
   - Insert PENDING task where `payload_sha256` matches actual file → scanner
     emits `outcome="duplicate"`, does NOT re-upsert to Qdrant.
8. `test_compensation_reparse_force_bypasses_hash`
   - Same as above but `force=True` → `outcome="success"`, Qdrant re-upserted.
9. `test_compensation_reparse_missing_source_path_skips`
   - Task row with `source_path` pointing to deleted file → `outcome="failed"`,
     task marked FAILED with descriptive `last_error`.

### Audit / schema tests

10. `test_compensation_retry_schema_requires_new_fields`
    - Direct call to `writer.write("compensation_retry", request_id="x")` (no
      outcome/duration) → raises ValueError (proves schema enforced).
11. `test_compensation_event_writer_round_trip`
    - Write all 4 outcome variants + read back via `AuditIndex`, confirm
      fields preserved.

---

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Re-ingest double-writes Qdrant during concurrent compensation + manual replay | Per-doc Redis lock already serializes ingestion (Phase 4); compensation handler reuses the same lock via `pipeline.reparse()` |
| Schema update breaks existing audit-log readers | Defensive defaults in `AuditIndex` (`outcome=None`, `duration_ms=0` for missing) |
| `--force` clobbers valid in-flight updates | Documented as operator-only escape hatch; not auto-invoked |
| `reparse()` differs from `replay()` route semantics | Spec them as **the same operation**, just callable from CLI vs HTTP |

---

## Out of Task 7 scope

- Changing the **handler trigger cadence** (still threshold_sec=60 + max_attempts=3).
- Adding **new audit events** for compensation cascade (no failure-event from handler → just enrich existing `compensation_retry`).
- **Backfilling** `reingest_outcome` to historical audit.log entries (one-way — old entries stay schema-lite).