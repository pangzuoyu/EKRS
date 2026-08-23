"""Phase 13a T6 — admission_rejected + task_timeout_killed audit events.

Both events are 4-step entries (cerebrum checklist):
1. register schema in _EVENT_SCHEMAS (main.py)
2. write-site emits on the actual production path
3. ekrs-handbook §16 inventory updated
4. real AuditWriter regression test (this file)

TDD red: this file is added before the schema registration / write-site
land. Tests fail on:
- admission_rejected: schema not registered → AuditWriter.write returns
  False (silently drops; if test fixture calls without registration, the
  required-field validation never runs — but our assertion looks for the
  event in the JSONL and finds none, failing the assertion).
- task_timeout_killed: not emitted anywhere → assertion finds zero events.

After T6 implementation lands, both events land in the JSONL and the
required-field schema is enforced.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ekrs_rag.observability.audit import AuditWriter, set_writer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_events(audit_path: Path) -> list[dict]:
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
    """Install a real AuditWriter + register all schemas used by tests."""
    audit_path = tmp_path / "audit.log"
    writer = AuditWriter(str(audit_path))
    # Register ALL schemas we exercise in this file. Mirrors main.py lifespan
    # — the prod lifespan will register admission_rejected + task_timeout_killed
    # in T6; tests register them here so validation passes regardless of whether
    # main.py has been updated yet (TDD allows the test file to land first).
    writer.register_event_schema("admission_rejected", {"doc_hash", "reason", "actual_chunks"})
    writer.register_event_schema("task_timeout_killed", {"doc_hash", "task_id", "timeout_s"})
    # Schema for the lock_acquire_failed event the notify handler emits
    # when the Redis lock conflicts (used by the T5 code path).
    writer.register_event_schema("lock_acquire_failed", {"lock_key"})
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
            set_writer(None)


# ---------------------------------------------------------------------------
# admission_rejected — emitted by notify() on coarse_gate / chunk_gate reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_rejected_emitted_on_coarse_gate_reject(
    audit_writer, monkeypatch, tmp_path
):
    """When coarse_gate rejects (raw_chars > limit), admission_rejected
    lands in audit.log with doc_hash + reason + actual_chunks fields.

    Phase 13a T6: this is the real AuditWriter path, not a mock. The
    schema is registered (4-step #1) and the write-site calls
    writer.write("admission_rejected", ...) on the rejection branch (4-step #2).
    """
    from ekrs_rag.api.routes.ingestion import notify
    from ekrs_rag.observability.trace import set_trace_id

    _, audit_path = audit_writer

    shared = tmp_path / "shared"
    shared.mkdir()

    # Build a 1M+ char raw payload — coarse_gate rejects (limit = 1M).
    # coarse_gate reads {output_path}/data.jsonl (parser contract).
    big_raw = "x" * 1_000_001
    output_dir = shared / "doc-rej"
    output_dir.mkdir()
    jsonl_path = output_dir / "data.jsonl"
    obj = {
        "doc_id": "doc-rej",
        "block_id": "b0",
        "type": "text",
        "content": {"raw": big_raw, "md_preview": big_raw},
        "metadata": {"page_number": 1, "heading_path": []},
    }
    with jsonl_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    pool_stub = MagicMock()
    lock_stub = MagicMock()
    lock_stub.acquire = AsyncMock(return_value="tok")
    lock_stub.release = AsyncMock()
    repo_stub = MagicMock()
    # repo.try_insert returns True (new row); mark_failed_with_error no-op.
    repo_stub.try_insert = MagicMock(return_value=True)
    repo_stub.mark_failed_with_error = MagicMock()
    repo_stub.mark_status = MagicMock()

    token = "x" * 32
    monkeypatch.setenv("PARSER_TOKEN", token)
    set_trace_id("trace-rej")

    class _FakeState:
        def __init__(self):
            self.shared_storage_root = shared.resolve()
            self.task_repo = repo_stub
            self.redis_lock = lock_stub
            self.encoding_pool = pool_stub
            self.document_repo = None

    class _FakeRequest:
        def __init__(self):
            self.app = MagicMock()
            self.app.state = _FakeState()
            self.state = MagicMock()
            self.state.request_id = "req-rej"

    req = _FakeRequest()

    from ekrs_shared.models import IngestionNotification
    notification = IngestionNotification(
        doc_hash="doc-rej",
        version=1,
        trace_id="trace-rej",
        output_path=str(output_dir),
    )

    bg = MagicMock()
    result = await notify(
        notification=notification,
        background_tasks=bg,
        request=req,
        pool=pool_stub,
        lock=lock_stub,
        repo=repo_stub,
        _auth=None,
    )
    assert result["status"] == "rejected"

    events = _read_events(audit_path)
    rejected = [e for e in events if e["event"] == "admission_rejected"]
    assert len(rejected) == 1, (
        f"admission_rejected must land in audit.log; got {len(rejected)} "
        f"events of that type (events: {[e['event'] for e in events]})"
    )
    last = rejected[-1]
    assert last["doc_hash"] == "doc-rej"
    assert last["reason"] == "raw_chars_over_limit"
    # actual_chunks: count of blocks (1 in this fixture)
    assert last["actual_chunks"] >= 1
    # request_id is md5(trace_id|doc_hash|version); non-empty hex hash
    assert isinstance(last["request_id"], str)
    assert len(last["request_id"]) == 32


# ---------------------------------------------------------------------------
# task_timeout_killed — emitted by EncodingPool.wait() on ProcessExpired /
# concurrent.futures.TimeoutError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_timeout_killed_emitted_on_pebble_timeout(audit_writer):
    """When pebble kills a worker for timeout, EncodingPool.wait() emits
    task_timeout_killed with doc_hash + task_id + timeout_s fields.

    We construct a fake pool task whose .result() raises a
    concurrent.futures.TimeoutError (the pebble 5.x mapping for timeout
    kills per T4 plan). The pool's wait() must:
    1. Convert the timeout to a structured {'rag_status':'failed',...} dict.
    2. Emit task_timeout_killed before returning.
    """
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from ekrs_rag.services.encoding_pool import EncodingPool
    from ekrs_rag.core.config import Settings

    _, audit_path = audit_writer

    # Build a pool with a stub internal _pool that yields a future raising
    # TimeoutError on .result(). We don't actually spawn pebble workers.
    class _FakeFut:
        def result(self):
            raise FuturesTimeoutError("killed by 30min timeout")

    pool = EncodingPool.__new__(EncodingPool)  # bypass __init__ (no pebble spawn)
    pool._tasks = {"task-fake": _FakeFut()}
    pool._task_timeout_s = 1800.0

    outcome = await pool.wait("task-fake", doc_hash="doc-timeout")

    assert outcome["rag_status"] == "failed"
    assert outcome["error_code"] == "task_timeout"

    events = _read_events(audit_path)
    killed = [e for e in events if e["event"] == "task_timeout_killed"]
    assert len(killed) == 1, (
        f"task_timeout_killed must land in audit.log; got {len(killed)} "
        f"events of that type (events: {[e['event'] for e in events]})"
    )
    last = killed[-1]
    assert last["doc_hash"] == "doc-timeout"
    assert last["task_id"] == "task-fake"
    assert last["timeout_s"] == 1800.0


@pytest.mark.asyncio
async def test_task_timeout_killed_emitted_on_process_expired(audit_writer):
    """ProcessExpired (pebble 5.x alternative for timeout kills) also
    emits task_timeout_killed — both exception types converge."""
    from pebble import ProcessExpired

    from ekrs_rag.services.encoding_pool import EncodingPool

    _, audit_path = audit_writer

    class _FakeFut:
        def result(self):
            raise ProcessExpired("worker subprocess killed")

    pool = EncodingPool.__new__(EncodingPool)
    pool._tasks = {"task-pe": _FakeFut()}
    pool._task_timeout_s = 1800.0

    outcome = await pool.wait("task-pe", doc_hash="doc-pe")

    assert outcome["rag_status"] == "failed"
    assert outcome["error_code"] == "task_timeout"

    events = _read_events(audit_path)
    killed = [e for e in events if e["event"] == "task_timeout_killed"]
    assert len(killed) == 1
    last = killed[-1]
    assert last["doc_hash"] == "doc-pe"
    assert last["task_id"] == "task-pe"
    assert last["timeout_s"] == 1800.0


@pytest.mark.asyncio
async def test_task_timeout_killed_not_emitted_on_normal_completion(audit_writer):
    """Sanity: a task that completes normally (no timeout) does NOT emit
    task_timeout_killed. This guards against accidental always-emit."""
    from ekrs_rag.services.encoding_pool import EncodingPool

    _, audit_path = audit_writer

    class _FakeFut:
        def result(self):
            return {"rag_status": "success", "chunks_indexed": 5}

    pool = EncodingPool.__new__(EncodingPool)
    pool._tasks = {"task-ok": _FakeFut()}
    pool._task_timeout_s = 1800.0

    outcome = await pool.wait("task-ok")
    assert outcome["rag_status"] == "success"

    events = _read_events(audit_path)
    killed = [e for e in events if e["event"] == "task_timeout_killed"]
    assert killed == [], (
        f"normal completion must not emit task_timeout_killed; got {killed}"
    )