---
title: EKRS TDD fixture convention — production-critical deps use real instances
date: 2026-07-21
category: docs/solutions/best-practices/
module: EKRS/rag
problem_type: best_practice
component: testing
severity: medium
applies_when:
  - Writing tests for code that injects production-critical dependencies
  - Auditing existing fixtures for branch-coverage gaps
  - Reviewing tests added during a bugfix (regression tests for emit/callback paths)
tags: [ekrs, tdd, fixtures, audit-writer, real-instance, mock-policy]
---

# EKRS TDD Fixture Convention — Production-Critical Deps Use Real Instances

## Context

Phase 6A T14 review surfaced a class of bug where test fixtures injected `audit_writer=None` (or `MagicMock` without `spec=`) to skip the audit branch. The implementation had a guard `if self._audit_writer is None: return` that short-circuited every emit call, so tests passed while zero coverage existed for the write paths. A later method-rename (`emit_event` → `write`) silently slipped through because the annotation was `object | None` and the call site was never exercised.

The root cause was not the implementation but the **fixture convention**: tests for code paths that emit, persist, or call external services must use **real instances** (or `MagicMock(spec=ConcreteClass)` with strict assertions), not bare `None`/`MagicMock()`. This doc codifies the rule and lists per-component guidance.

## Rule

> **For any injected dependency that is exercised along a production code path being tested, use a real instance — not `None`, not a bare `MagicMock()`.**

The "production code path being tested" is the test's *subject*. If your test is asserting that `ingest()` writes a `query_received` audit event, then `audit_writer` is on the production code path under test and must be a real instance.

## Decision Tree

When writing a test, ask these questions about each injected dep:

```
Q1: Does the dep's method have side effects the test cares about
    (writes, emits, network, persistence)?
    │
    ├── YES  → Use a real instance (or a fake with the same observable contract).
    │         See "Real-instance injection" patterns below.
    │
    └── NO   → Q2: Does the dep have any method that could plausibly
                misbehave in a way the test cares about?
                │
                ├── YES  → Use MagicMock(spec=DepClass), strict attribute check.
                │         Assertions must verify expected calls.
                │
                └── NO   → Use MagicMock() with no assertions on this dep.
```

The key heuristic: **if you delete the dep entirely, does the test still pass?** If yes, the dep is on a non-tested path and may be `None` or a bare mock. If no, the dep is on the tested path and must be real.

## Real-Instance Injection Patterns

### AuditWriter (ekrs_rag.observability.audit.AuditWriter)

`AuditWriter` writes to a rotating file with size-bounded retention. Use the real class in tests:

```python
import tempfile
from pathlib import Path
from ekrs_rag.observability.audit import AuditWriter

@pytest.fixture
def audit_writer(tmp_path: Path) -> AuditWriter:
    return AuditWriter(
        log_path=tmp_path / "audit.log",
        index_path=tmp_path / "audit.index",
    )
```

If a test only needs to assert an event *was* written, read back from `audit_writer.read_recent(limit=N)` and assert on the contents. Do not mock — the act of writing is part of what's being tested.

### EmbeddingService (ekrs_rag.retrieval.embedding_service.EmbeddingService)

`EmbeddingService` falls back to *dummy mode* when ONNX is missing. Use the real class with `_load_flag_model` patched:

```python
from unittest.mock import patch
from ekrs_rag.retrieval.embedding_service import EmbeddingService

@pytest.fixture
def embedding_service(mock_flag_model: MagicMock, tmp_path: Path) -> EmbeddingService:
    (tmp_path / "model.onnx").write_bytes(b"x")
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_flag_model",
        return_value=mock_flag_model,
    ):
        return EmbeddingService(model_dir=tmp_path)
```

This preserves `is_dummy` semantics (`False` for the patched case) and lets tests assert on the real `EncodedVector` shape. Dummy mode itself has its own tests (`test_is_dummy_*`).

### RedisLock (ekrs_rag.concurrency.redis_lock.RedisLock)

For lock-contention tests, use a real Redis (Docker `make dev`). For single-process "lock held" tests, use the real class with a fakeredis instance:

```python
import fakeredis.aioredis  # type: ignore[import-not-found]
from ekrs_rag.concurrency.redis_lock import RedisLock

@pytest.fixture
async def redis_lock() -> RedisLock:
    client = fakeredis.aioredis.FakeRedis()
    return RedisLock(client, key="test:lock", ttl_seconds=10)
```

Do **not** mock `acquire()`/`release()` — the locking semantics *are* what's being tested.

### QdrantManager (ekrs_rag.retrieval.qdrant_client.QdrantManager)

For Qdrant-touching tests, use the real class against a Qdrant testcontainer (integration tests, `tests/integration/`). For pure unit tests that don't touch Qdrant at all, leave the manager out of the test entirely.

### Counter-examples (do NOT do this)

```python
# WRONG: guard short-circuits all writes; test passes while emit path is uncovered
@pytest.fixture
def pipeline() -> IngestionPipeline:
    p = IngestionPipeline(...)
    p._audit_writer = None  # ← this is the bug
    return p

# WRONG: bare MagicMock accepts any attribute name, hides method-rename bugs
@pytest.fixture
def pipeline() -> IngestionPipeline:
    p = IngestionPipeline(...)
    p._audit_writer = MagicMock()  # ← no spec=; .write renamed to .emit_event still passes
    return p

# CORRECT: spec=RealClass catches method-rename at call-site, not type-check time
@pytest.fixture
def pipeline() -> IngestionPipeline:
    p = IngestionPipeline(...)
    p._audit_writer = MagicMock(spec=AuditWriter)
    return p
```

`spec=` is a partial mitigation but does not exercise the *real* write semantics (e.g., rotation, index rebuild, file format). For deps that have observable I/O, prefer real instances.

## Exceptions

When `None` or a bare `MagicMock()` is acceptable:

1. **The dep is not exercised at all on the test path.** E.g., a unit test for `_parse_query()` that doesn't touch the audit writer. The default in `IngestionPipeline.__init__` is `_audit_writer = None` precisely so these tests don't need to construct a writer.
2. **The test is asserting a precondition for a missing dep.** E.g., `assert pipeline._audit_writer is None` after `__init__`. This is a one-line test, not a multi-step flow.
3. **The dep is a third-party network client and the test is gated on "the call would have been made" via mock.** Acceptable for `httpx.AsyncClient` in callback tests when the assertion is `client.post.assert_awaited_once_with(...)`. Document this in the test docstring.

## Reviewer Checklist

When reviewing a new test, ask:

- [ ] For each injected dep, can I trace which production call site it covers?
- [ ] If I delete the dep, does the test still pass? If yes, is the dep on the tested path?
- [ ] Are assertions on the dep's behavior, not just its existence (`assert_called_with`, file content, etc.)?
- [ ] If `MagicMock` is used, does it have `spec=` matching the real class?
- [ ] Is the exception case (the test legitimately doesn't need the dep) documented in the test docstring?

## Related

- Phase 6A T14 review: `docs/superpowers/plans/2026-07-21-ekrs-integration-fixes-review.md` — original finding.
- Phase 6C scope doc: `docs/superpowers/plans/2026-07-21-phase6c-scope.md` — T3 rationale.
- Protocol + AuditEmitter (`def write(self, event_type: str, **kwargs: object) -> bool`): catches method-rename at *type-check* time as a complement to this fixture convention (orthogonal layer of defense).
