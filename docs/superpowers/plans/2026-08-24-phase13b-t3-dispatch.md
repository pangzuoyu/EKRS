# Phase 13b T3 — EncodingRouter dispatch + channel_switched audit

## Context

Phase 13b ships torch FP16 GPU encoding. T1 (commit `7c7377c`) + T2+T4 (commit `b8a03b1`) shipped `encode_gpu` + `EncodingRouter` + GPU metrics. EncodingRouter is **registered in `_init_child` but never invoked on the hot path** — the actual encode still happens inside `QdrantManager.upsert_chunks` via `EmbeddingService.encode()`. T3 wires the router into the production path.

Review 🔴 #2 mandates: `precomputed_encodings` kwarg on `upsert_chunks` (NOT rebinding T9 `_encode_backend` seam — that's dense-only and would lose dual-head `EncodedVector`). T9 seam stays untouched.

Review 🟢 #6 mandates: transition-only audit emit (already implemented in `encoding_router._transition`). T3 closes the 4-step discipline for `channel_switched`: schema registration + emit site (already in b8a03b1) + ekrs-handbook §16 inventory + real-AuditWriter regression.

## TDD Order (v1.1 §7)

T1 ✅ → T2 ✅ + T4 ✅ → **T3 (this plan)** → T5 → T6

## Pre-flight fix-up (eng-review UQ-5 + UQ-6)

Resolve BEFORE T3 commits:

1. **`BGE_M3_GPU_ENABLED` default → `False`** in `rag/ekrs_rag/core/config.py:144` (Phase 13a 灰度节奏). Env var override preserved. Test `test_init_child_reregisters_gpu_on_startup` already monkeypatches True — no breakage.

2. **`torch` as optional dep** in `pyproject.toml`: move `torch>=2.1,<3` from `[project.dependencies]` to `[project.optional-dependencies] gpu = ["torch>=2.1,<3"]`. CPU-only install: `pip install ekrs-rag`. GPU install: `pip install ekrs-rag[gpu]`. Runtime: lazy import in `torch_bge_m3.py` already handles missing-torch → router falls back to cpu.

## Approach

12 RED tests → GREEN implementation in 5 files → commit.

### Step 1 — RED tests (T3.5)

| File | Test | Intent |
|------|------|--------|
| `tests/unit/test_qdrant_client.py` | `test_upsert_chunks_with_precomputed_skips_is_dummy_guard` | precomputed + `_is_dummy=True`; 0 raise, 0 encode call |
| `tests/unit/test_qdrant_client.py` | `test_upsert_chunks_with_precomputed_validates_length` | chunks=3, precomputed=2 → `ValueError` |
| `tests/unit/test_qdrant_client.py` | `test_upsert_chunks_with_precomputed_uses_supplied_vectors_directly` | spy encode; not called when precomputed provided |
| `tests/unit/test_step5_helpers.py` | `test_run_step5_uses_encoding_router_precomputed_kwarg` | monkeypatch router; assert upsert_chunks called with kwarg |
| `tests/unit/test_step5_helpers.py` | `test_run_step5_router_cpu_fallback_propagates` | router.route returns CPU vectors; precomputed kwarg populated |
| `tests/unit/test_audit_event_coverage_t10a7.py` | `test_main_event_schemas_contains_channel_switched` | schema presence + field match |
| `tests/unit/test_audit_event_coverage_t10a7.py` | UPDATE `test_main_event_schemas_count_24` → 25 | count |
| `tests/unit/test_audit_phase13_events.py` | `test_channel_switched_emitted_on_real_audit_writer` | full JSONL round-trip |
| `tests/unit/test_audit_phase13_events.py` | `test_channel_switched_payload_matches_registered_schema` | extra kwarg → False |
| `tests/unit/test_phase13b_t3_probe.py` (NEW) | `test_init_child_spawns_health_probe_daemon_thread` | daemon thread `ekrs_gpu_probe` exists |
| `tests/unit/test_phase13b_t3_probe.py` (NEW) | `test_health_probe_calls_force_re_register_gpu_periodically` | monkeypatch sleep; ≥3 calls |
| `tests/unit/test_phase13b_t3_probe.py` (NEW) | `test_health_probe_swallows_exceptions` | force_re_register raises → loop survives |

### Step 2 — GREEN implementation

5 files:

**`rag/ekrs_rag/retrieval/qdrant_client.py:182`**

```python
def upsert_chunks(
    self,
    chunks: list[Chunk],
    *,
    precomputed_encodings: list[EncodedVector] | None = None,
) -> int:
```

Inside body (line 188, BEFORE `is_dummy` guard):
```python
if not chunks:
    return 0
if precomputed_encodings is not None:
    if len(precomputed_encodings) != len(chunks):
        raise ValueError(...)
    encoded = precomputed_encodings
else:
    if self._embedding_service.is_dummy:
        raise EmbeddingUnavailableError(...)
    encoded = self._embedding_service.encode(texts)
```

Rest of body unchanged.

**`rag/ekrs_rag/services/step5_helpers.py:57-67`** (Protocol)

Update `QdrantLike.upsert_chunks` signature with kwarg.

**`rag/ekrs_rag/services/step5_helpers.py:261`**

```python
from . import encoding_router as _er  # lazy
texts = [c.text for c in chunks]
precomputed = _er.get_router().route(texts)
count = qdrant.upsert_chunks(chunks, precomputed_encodings=precomputed)
```

**`rag/ekrs_rag/main.py:129`** (in `_EVENT_SCHEMAS`)

Add after `task_timeout_killed`:
```python
"channel_switched": {"from_channel", "to_channel", "reason"},
```

Count 24 → 25.

**`rag/ekrs_rag/services/encoding_pool.py`** (after line 140 in `_init_child`)

Add Settings knobs in `core/config.py`:
```python
BGE_M3_GPU_PROBE_ENABLED: bool = True
BGE_M3_GPU_PROBE_INTERVAL_S: int = 30
```

Spawn probe loop (before sys.excepthook):
```python
if settings.BGE_M3_GPU_PROBE_ENABLED and settings.BGE_M3_GPU_ENABLED:
    try:
        import threading as _t
        _stop_event = _t.Event()
        def _probe_loop() -> None:
            while not _stop_event.is_set():
                _stop_event.wait(settings.BGE_M3_GPU_PROBE_INTERVAL_S)
                if _stop_event.is_set():
                    break
                try:
                    _er.get_router().force_re_register_gpu()
                except Exception as _e:
                    logger.debug("gpu_probe: force_re_register failed: %s", _e)
        _t.Thread(
            target=_probe_loop, name="ekrs_gpu_probe", daemon=True,
        ).start()
    except Exception as e:
        logger.warning("init_child: GPU health probe spawn failed: %s", e)
```

AuditWriter is process-local. Pebble workers are fresh subprocesses — `get_writer()` returns None in workers. Probe emits silently drop in workers (acceptable — operators see state via T4 `ekrs_gpu_memory_*` Gauges).

### Step 3 — handbook (4-step step #3)

Update `ekrs-handbook.md §16 audit inventory` — add `channel_switched {from_channel, to_channel, reason}` line.

## Critical Files

- `rag/ekrs_rag/core/config.py` (line 144 GPU default False + 2 new knobs)
- `rag/ekrs_rag/retrieval/qdrant_client.py` (kwarg + dummy-guard bypass)
- `rag/ekrs_rag/services/step5_helpers.py` (Protocol + line 261 dispatch)
- `rag/ekrs_rag/main.py` (`_EVENT_SCHEMAS` line 129)
- `rag/ekrs_rag/services/encoding_pool.py` (30s probe daemon)
- `pyproject.toml` (torch → optional)
- `ekrs-handbook.md` (§16 line)
- 6 test files (5 MODIFIED + 1 NEW)

## Verification

1. `cd rag && pytest tests/unit/test_qdrant_client.py tests/unit/test_step5_helpers.py tests/unit/test_audit_event_coverage_t10a7.py tests/unit/test_audit_phase13_events.py tests/unit/test_phase13b_t3_probe.py -v` — 12 new pass
2. `cd rag && pytest tests/unit tests/golden_set -q` — 0 NEW regression
3. `cd rag && mypy ekrs_rag/ --config-file mypy.ini` — clean on touched files
5. 4-step discipline: schema ✓ + emit ✓ + handbook ✓ + regression ✓
6. T9 seam unchanged: `tests/unit/test_phase13a_t9.py` untouched

## Commit

```
feat(encoder): Phase 13b T3 — wire EncodingRouter on hot path + 30s GPU probe + channel_switched audit
```

No new tag — `phase13a` locked `e5c8f39`; `phase13b` reserved for T6.