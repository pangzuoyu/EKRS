"""Regression coverage for Phase 7 T2 audit-event emit additions.

Phase 6C 关闭后审计事件完整性审查发现 6 个 schema 完全无 emit：
`ingestion_received` / `ingestion_completed` / `ingestion_failed` /
`lock_acquire_failed` / `constraint_solve_started` /
`constraint_solved` / `constraint_solve_failed` /
`compensation_retry`。本文件用真实 `AuditWriter + AuditIndex`
覆盖每条新增 emit：assert 事件名落在 audit.log 内且 schema 字段
齐全。不依赖 mock — Phase 6C T1 集成测试已暴露 mock 漏掉
write-method 真实契约的风险。

每个测试创建独立 tmp_path + AuditWriter，避免共享索引状态。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ekrs_rag.concurrency.compensation import CompensationScanner
from ekrs_rag.ingestion.outcome import IngestionOutcome
from ekrs_rag.observability.audit import AuditWriter, set_writer
from ekrs_rag.storage.task_repo import TaskRepo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_events(audit_path: Path) -> list[dict]:
    """Parse every JSONL line in audit.log into a list of dicts."""
    if not audit_path.exists():
        return []
    out: list[dict] = []
    for line in audit_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _install_writer(tmp_path: Path) -> AuditWriter:
    """Install a real AuditWriter for this test; clean up afterwards."""
    audit_path = tmp_path / "audit.log"
    writer = AuditWriter(str(audit_path))
    # Register schemas for the events we'll emit (mirrors main.py lifespan).
    writer.register_event_schema("compensation_retry", {"request_id"})
    writer.register_event_schema("lock_acquire_failed", {"lock_key"})
    writer.register_event_schema("ingestion_received", {"request_id", "doc_id"})
    writer.register_event_schema("ingestion_completed", {"request_id", "doc_id"})
    writer.register_event_schema("ingestion_failed", {"request_id", "doc_id"})
    writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    writer.register_event_schema("constraint_solve_failed", {"trace_id", "error_type"})
    set_writer(writer)
    return writer


@pytest.fixture
def audit_writer():
    """Per-test: install writer pointed at tmp_path/audit.log."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        writer = _install_writer(tmp)
        try:
            yield writer, tmp / "audit.log"
        finally:
            # Reset module-level writer to avoid leakage across tests.
            set_writer(None)


# ---------------------------------------------------------------------------
# compensation_retry
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        r = TaskRepo(db_path=os.path.join(d, "test.db"))
        r.init()
        yield r


@pytest.mark.asyncio
async def test_compensation_retry_emitted_on_handler_invocation(repo, audit_writer):
    """When handler is invoked, compensation_retry event lands in audit log
    with attempt + reason=retry_invoked."""
    writer, audit_path = audit_writer
    repo.try_insert("req-1", "doc-a")
    old = time.time() - 3600
    repo._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='req-1'", (old,)
    )
    repo._conn.commit()

    async def handler(task: dict) -> None:
        pass

    scanner = CompensationScanner(task_repo=repo, handler=handler, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 1

    events = _read_events(audit_path)
    retry_events = [e for e in events if e["event"] == "compensation_retry"]
    assert len(retry_events) >= 1
    last = retry_events[-1]
    assert last["request_id"] == "req-1"
    assert last.get("reason") == "retry_invoked"
    assert last.get("attempt") == 1


@pytest.mark.asyncio
async def test_compensation_retry_emitted_on_unwired_handler(repo, audit_writer):
    """When handler_is_wired=False, compensation_retry still emits with
    reason=handler_not_wired so operators see the scan finding the row."""
    _, audit_path = audit_writer
    repo.try_insert("req-2", "doc-b")
    old = time.time() - 3600
    repo._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='req-2'", (old,)
    )
    repo._conn.commit()

    async def handler(task: dict) -> None:  # pragma: no cover — never called
        raise AssertionError("handler must not be invoked when unwired")

    scanner = CompensationScanner(
        task_repo=repo, handler=handler, threshold_sec=60.0,
        handler_is_wired=False,
    )
    n = await scanner.scan()
    assert n == 0

    events = _read_events(audit_path)
    retry_events = [e for e in events if e["event"] == "compensation_retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["request_id"] == "req-2"
    assert retry_events[0].get("reason") == "handler_not_wired"


@pytest.mark.asyncio
async def test_compensation_retry_emitted_on_handler_failure(repo, audit_writer):
    """When handler raises, compensation_retry emits with
    reason=handler_failed:<ExcClass> so the failure surfaces in the log."""
    _, audit_path = audit_writer
    repo.try_insert("req-3", "doc-c")
    old = time.time() - 3600
    repo._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='req-3'", (old,)
    )
    repo._conn.commit()

    async def boom(task: dict) -> None:
        raise RuntimeError("retry handler blew up")

    scanner = CompensationScanner(
        task_repo=repo, handler=boom, threshold_sec=60.0
    )
    n = await scanner.scan()
    assert n == 0

    events = _read_events(audit_path)
    retry_events = [e for e in events if e["event"] == "compensation_retry"]
    # Two emissions: retry_invoked on entry, handler_failed on raise.
    assert len(retry_events) == 2
    assert retry_events[0].get("reason") == "retry_invoked"
    assert retry_events[1].get("reason") == "handler_failed:RuntimeError"
    assert all(e["request_id"] == "req-3" for e in retry_events)


# ---------------------------------------------------------------------------
# _run_locked_ingest: ingestion_received / ingestion_completed / ingestion_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingestion_received_and_completed_emit_on_success(audit_writer):
    """Success path: ingestion_received + ingestion_completed both fire."""
    from ekrs_rag.api.routes.ingestion import _run_locked_ingest

    _, audit_path = audit_writer

    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(
        return_value=IngestionOutcome(rag_status="success", chunks_indexed=7),
    )
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()
    notification.doc_hash = "doc-success"

    await _run_locked_ingest(
        pipeline=pipeline, repo=repo, lock=lock,
        lock_key="k", lock_token="t",
        notification=notification, request_id="req-success",
    )

    events = _read_events(audit_path)
    by_name = {e["event"]: e for e in events}
    assert "ingestion_received" in by_name
    assert "ingestion_completed" in by_name
    assert "ingestion_failed" not in by_name
    assert by_name["ingestion_received"]["doc_id"] == "doc-success"
    assert by_name["ingestion_received"]["request_id"] == "req-success"
    assert by_name["ingestion_completed"]["chunks_indexed"] == 7


@pytest.mark.asyncio
async def test_ingestion_failed_emit_on_failed_outcome(audit_writer):
    """Failed outcome: ingestion_received + ingestion_failed both fire."""
    from ekrs_rag.api.routes.ingestion import _run_locked_ingest

    _, audit_path = audit_writer

    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(
        return_value=IngestionOutcome(
            rag_status="failed",
            error="jsonl missing",
            error_code="jsonl_missing",
        ),
    )
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()
    notification.doc_hash = "doc-fail"

    await _run_locked_ingest(
        pipeline=pipeline, repo=repo, lock=lock,
        lock_key="k", lock_token="t",
        notification=notification, request_id="req-fail",
    )

    events = _read_events(audit_path)
    by_name = {e["event"]: e for e in events}
    assert "ingestion_received" in by_name
    assert "ingestion_failed" in by_name
    assert "ingestion_completed" not in by_name
    assert by_name["ingestion_failed"]["error_code"] == "jsonl_missing"
    assert by_name["ingestion_failed"]["error"] == "jsonl missing"


@pytest.mark.asyncio
async def test_ingestion_failed_emit_on_unhandled_exception(audit_writer):
    """Unhandled exception: ingestion_failed fires with error_code=unhandled_exception."""
    from ekrs_rag.api.routes.ingestion import _run_locked_ingest

    _, audit_path = audit_writer

    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(side_effect=RuntimeError("kaboom"))
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()
    notification.doc_hash = "doc-boom"

    with pytest.raises(RuntimeError):
        await _run_locked_ingest(
            pipeline=pipeline, repo=repo, lock=lock,
            lock_key="k", lock_token="t",
            notification=notification, request_id="req-boom",
        )

    events = _read_events(audit_path)
    by_name = {e["event"]: e for e in events}
    assert "ingestion_failed" in by_name
    assert by_name["ingestion_failed"]["error_code"] == "unhandled_exception"
    assert "kaboom" in by_name["ingestion_failed"]["error"]


# ---------------------------------------------------------------------------
# constraints.py: constraint_solve_started / constraint_solved /
# constraint_solve_failed (gate 1/2/3 paths)
# ---------------------------------------------------------------------------


def _make_retriever(chunks: list | None = None) -> MagicMock:
    """Return a stub EKRSRetriever returning the given chunks (or empty)."""
    r = MagicMock()
    retrieval = MagicMock()
    retrieval.chunks = chunks or []
    r.retrieve = MagicMock(return_value=retrieval)
    return r


@pytest.mark.asyncio
async def test_constraint_solve_started_and_solved_emit_on_happy_path(
    audit_writer, monkeypatch
):
    """Happy path emits constraint_solve_started + constraint_solved."""
    from ekrs_rag.api.routes.constraints import ConstraintQuery, query_constraints

    _, audit_path = audit_writer

    # Force retrieval to succeed with at least one chunk so Gate 1 passes.
    # EvidenceBuilder will then produce constraints for the solver. To
    # keep this test deterministic, stub EvidenceBuilder to return one
    # non-inferred constraint.
    from ekrs_rag.api.routes import constraints as constraints_mod

    class _FakeConstraint:
        inferred = False
        parameter = "T_max"
        operator = "<="
        value = 400.0
        unit = "C"
        scope_path = []

    monkeypatch.setattr(
        constraints_mod.EvidenceBuilder,
        "build",
        lambda chunks: [_FakeConstraint()],
    )
    monkeypatch.setattr(
        constraints_mod.IntervalSolver,
        "solve",
        lambda constraints, active_scope=None: {
            "status": "OK",
            "branches": {"general": {"T_max": "<= 400 C"}},
            "primary_branch": "general",
            "trace": [],
            "conflicts": [],
        },
    )

    retriever = _make_retriever(chunks=[MagicMock()])
    audit_index = None  # main flow doesn't touch audit_index

    # Build request directly (we don't go through FastAPI here; the route
    # function signature is `query_constraints(query, retriever, audit_index,
    # _auth)`.
    cq = ConstraintQuery(query="高温环境温度上限", context={}, strict=False)
    # _auth is the Depends(require_parser_token); bypass by passing None
    # (the route does not use _auth in the body other than Depends wiring).
    response = await query_constraints(
        query=cq, retriever=retriever, audit_index=audit_index, _auth=None
    )
    assert response.mode == "single"

    events = _read_events(audit_path)
    by_name = {e["event"]: e for e in events}
    assert "constraint_solve_started" in by_name
    assert "constraint_solved" in by_name
    assert "constraint_solve_failed" not in by_name
    assert by_name["constraint_solve_started"]["query"] == "高温环境温度上限"
    assert by_name["constraint_solved"]["branches_count"] == 1


@pytest.mark.asyncio
async def test_constraint_solve_failed_emit_on_gate1_insufficient_recall(
    audit_writer, monkeypatch
):
    """Gate 1 (insufficient recall) emits constraint_solve_failed with
    error_type=insufficient_recall, status_code=404."""
    from ekrs_rag.api.routes import constraints as constraints_mod
    from ekrs_rag.api.routes.constraints import ConstraintQuery
    from fastapi import HTTPException

    _, audit_path = audit_writer
    retriever = _make_retriever(chunks=[])  # Gate 1 triggers

    cq = ConstraintQuery(query="x", context={}, strict=False)
    with pytest.raises(HTTPException):
        await constraints_mod.query_constraints(
            query=cq, retriever=retriever, audit_index=None, _auth=None
        )

    events = _read_events(audit_path)
    failed = [e for e in events if e["event"] == "constraint_solve_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "insufficient_recall"
    assert failed[0]["status_code"] == 404


@pytest.mark.asyncio
async def test_constraint_solve_failed_emit_on_gate2_no_constraints(
    audit_writer, monkeypatch
):
    """Gate 2 (no constraints extracted) emits constraint_solve_failed with
    error_type=no_constraints_extracted, status_code=404."""
    from ekrs_rag.api.routes import constraints as constraints_mod
    from ekrs_rag.api.routes.constraints import ConstraintQuery
    from fastapi import HTTPException

    _, audit_path = audit_writer
    retriever = _make_retriever(chunks=[MagicMock()])  # Gate 1 passes

    # EvidenceBuilder.build returns [] → Gate 2 triggers.
    monkeypatch.setattr(
        constraints_mod.EvidenceBuilder, "build", lambda chunks: []
    )

    cq = ConstraintQuery(query="x", context={}, strict=False)
    with pytest.raises(HTTPException):
        await constraints_mod.query_constraints(
            query=cq, retriever=retriever, audit_index=None, _auth=None
        )

    events = _read_events(audit_path)
    failed = [e for e in events if e["event"] == "constraint_solve_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "no_constraints_extracted"
    assert failed[0]["status_code"] == 404


@pytest.mark.asyncio
async def test_constraint_solve_failed_emit_on_gate3_conflict(
    audit_writer, monkeypatch
):
    """Gate 3 (CONFLICT) emits constraint_solve_failed with error_type=conflict,
    status_code=409."""
    from ekrs_rag.api.routes import constraints as constraints_mod
    from ekrs_rag.api.routes.constraints import ConstraintQuery
    from fastapi import HTTPException

    _, audit_path = audit_writer
    retriever = _make_retriever(chunks=[MagicMock()])  # Gate 1 passes

    class _FakeConstraint:
        inferred = False

    monkeypatch.setattr(
        constraints_mod.EvidenceBuilder,
        "build",
        lambda chunks: [_FakeConstraint()],
    )
    monkeypatch.setattr(
        constraints_mod.IntervalSolver,
        "solve",
        lambda constraints, active_scope=None: {
            "status": "CONFLICT",
            "branches": {},
            "primary_branch": None,
            "trace": [],
            "conflicts": [{"a": "b"}],
        },
    )

    cq = ConstraintQuery(query="x", context={}, strict=False)
    with pytest.raises(HTTPException):
        await constraints_mod.query_constraints(
            query=cq, retriever=retriever, audit_index=None, _auth=None
        )

    events = _read_events(audit_path)
    failed = [e for e in events if e["event"] == "constraint_solve_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "conflict"
    assert failed[0]["status_code"] == 409


@pytest.mark.asyncio
async def test_constraint_solve_failed_emit_on_strict_mode_inferred(
    audit_writer, monkeypatch
):
    """Strict mode rejects inferred constraints with status_code=400."""
    from ekrs_rag.api.routes import constraints as constraints_mod
    from ekrs_rag.api.routes.constraints import ConstraintQuery
    from fastapi import HTTPException

    _, audit_path = audit_writer
    retriever = _make_retriever(chunks=[MagicMock()])  # Gate 1 passes

    class _FakeInferredConstraint:
        inferred = True

    monkeypatch.setattr(
        constraints_mod.EvidenceBuilder,
        "build",
        lambda chunks: [_FakeInferredConstraint()],
    )

    cq = ConstraintQuery(query="x", context={}, strict=True)
    with pytest.raises(HTTPException):
        await constraints_mod.query_constraints(
            query=cq, retriever=retriever, audit_index=None, _auth=None
        )

    events = _read_events(audit_path)
    failed = [e for e in events if e["event"] == "constraint_solve_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "strict_inferred"
    assert failed[0]["status_code"] == 400


# ---------------------------------------------------------------------------
# ingestion.py /notify: lock_acquire_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_acquire_failed_emit_on_lock_conflict(
    audit_writer, monkeypatch, tmp_path
):
    """When RedisLock.acquire returns None (already held), lock_acquire_failed
    must be emitted so operators see the contention."""
    from ekrs_rag.api.routes.ingestion import notify
    from ekrs_rag.observability.trace import set_trace_id

    _, audit_path = audit_writer

    shared = tmp_path / "shared"
    shared.mkdir()

    # Stub pipeline/repo/lock on app.state. Lock always conflicts.
    pipeline_stub = MagicMock()
    repo_stub = MagicMock()
    lock_stub = MagicMock()
    lock_stub.acquire = AsyncMock(return_value=None)  # contention!

    token = "x" * 32
    monkeypatch.setenv("PARSER_TOKEN", token)
    set_trace_id("trace-locktest")

    # Build a minimal Request-like object so notify can read app.state and
    # request.state.request_id. We avoid TestClient here to keep the test
    # focused on the audit emit (TestClient requires app state pre-warmed
    # for several FastAPI internals).
    class _FakeState:
        def __init__(self):
            self.shared_storage_root = shared.resolve()
            self.pipeline = pipeline_stub
            self.task_repo = repo_stub
            self.redis_lock = lock_stub
            self.document_repo = None

    class _FakeRequest:
        def __init__(self):
            self.app = MagicMock()
            self.app.state = _FakeState()
            self.state = MagicMock()
            self.state.request_id = "req-locktest"

    fake_request = _FakeRequest()

    # Build a minimal IngestionNotification via the Pydantic model directly.
    from ekrs_shared.models import IngestionNotification
    notification = IngestionNotification(
        doc_hash="doc-lock",
        version=1,
        trace_id="trace-locktest",
        output_path=str(shared / "doc-lock.jsonl"),
    )

    bg = MagicMock()  # BackgroundTasks is unused when lock conflicts
    result = await notify(
        notification=notification,
        background_tasks=bg,
        request=fake_request,
        pipeline=pipeline_stub,
        lock=lock_stub,
        repo=repo_stub,
        _auth=None,
    )
    assert result["status"] == "in_flight"

    events = _read_events(audit_path)
    lock_events = [e for e in events if e["event"] == "lock_acquire_failed"]
    assert len(lock_events) == 1
    assert lock_events[0]["lock_key"] == "lock:ingest:doc-lock"
    assert lock_events[0]["doc_id"] == "doc-lock"
    assert lock_events[0]["request_id"]