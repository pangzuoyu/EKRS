# Task 8 Report: Final verify + tag (Phase 5.5 F)

## Status: DONE

## Tag
- Name: `phase5.5-f-audit-rotation`
- SHA: HEAD
- Commits: 7 (T1–T7) since Phase 5.5 E tag

## Step 1: Full suite final verify
```
340 passed, 0 failed
```
(vs. 325 in Phase 5.5 E → +15 net new tests across audit_handler/audit_rotation/skip_audit/observability_skip_audit)

## Step 2: Wiring grep

### T2 AuditWriter rotation
```
15: RebuildingRotatingFileHandler
16: gzip_namer
17: gzip_rotator
36: handler = RebuildingRotatingFileHandler(
38: maxBytes=100 * 1024 * 1024,
43: handler.namer = gzip_namer
44: handler.rotator = gzip_rotator
```

### T3 trace.py skip_audit
```
30: def set_skip_audit(skip: bool) -> Token:
35: def reset_skip_audit(token: Token) -> None:
39: def get_skip_audit() -> bool:
```

### T5 middleware
```
19: set_skip_audit, reset_skip_audit
41: skip_token = set_skip_audit(request.url.path == "/healthz")
72: reset_skip_audit(skip_token)
```

### T6 main.py callback
```
195: def _on_audit_rollover() -> None:
210: _audit_writer = AuditWriter(audit_path, on_rollover=_on_audit_rollover)
```

### T7 CLAUDE.md
```
67: Audit log (`audit.log`): permanent, size-bounded by rotation (100MB × 5 gzip), records every solve with evidence
```

## Step 3: Iron Rules
- ✅ R1: NumericHint schema unchanged
- ✅ R2: Solver still pure
- ✅ R3: 3-gate pipeline unchanged
- ✅ R4: Context priority unchanged
- ✅ R5: Entity-overlap scoring unchanged
- ✅ R6: strict mode unchanged
- ✅ R7: scope_path filter unchanged

## Step 4: Audit event schemas
- ✅ All 15 event names + required fields unchanged

## Final commit history (7 commits since Phase 5.5 E tag)
```
3a582d9 T7: CLAUDE.md — audit log spec reflects rotation
532f060 T6: main.py lifespan wires on_rollover callback
a7e2acb T5: middleware sets skip_audit for /healthz requests
1a30b8a T4: AuditWriter.write honors skip_audit flag
56436e4 T3: add _skip_audit ContextVar in trace.py
8019f22 T2: AuditWriter uses RotatingFileHandler with gzip rotator
c05a98c T1: add RebuildingRotatingFileHandler with gzip rotator
```

## Issues encountered & resolved
1. **`namer`/`rotator` not __init__ kwargs** (T1): Python's RotatingFileHandler doesn't accept them in __init__; must be set as attributes after construction. Fixed test fixture.
2. **`audit_writer_does_not_rotate` test deleted** (T2): Per D3 hard-delete; that test asserted the OLD (no-rotation) behavior which we explicitly changed.
3. **`AuditWriter.close()` doesn't exist** (T2 tests): Tests close the handler directly via `w._file_handler.close()`.
4. **`_CountingWriter` test mock insufficient** (T5): Rewrote to use real AuditWriter + inspect the log file — proves the actual skip path through AuditWriter.write.

## Open questions
None — user decisions locked during brainstorming.

## Concerns (non-blocking)
1. **Rebuild blocks request thread**: ~1s for 100MB file. Acceptable per user decision; future optimization possible.
2. **Gzip rotator idempotency**: `doRollover` invokes `namer` then `rotator`. If process dies between, `.1` may exist un-gzipped. Wrapped in try/except — won't crash.
3. **Old trace_ids invalid post-rotation**: Per user decision, only current file scanned. Acceptable trade-off — long-tail traces lost, current traces preserved.