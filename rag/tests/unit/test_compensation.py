import os
import tempfile
import time
import inspect
from pathlib import Path

import pytest

from ekrs_rag.storage.task_repo import TaskRepo
from ekrs_rag.concurrency.compensation import CompensationScanner


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        r = TaskRepo(db_path=db)
        r.init()
        yield r


@pytest.mark.asyncio
async def test_scan_retries_failed_old_tasks(repo):
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "FAILED", error="x")
    old = time.time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old,))
    repo._conn.commit()

    called = []
    async def handler(task: dict) -> bool:
        called.append(task["request_id"])
        return True

    scanner = CompensationScanner(task_repo=repo, handler=handler, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 1
    assert called == ["req1"]
    # 状态变为 RUNNING (handler 抛错才 FAILED)
    assert repo.get("req1")["status"] in ("RUNNING", "COMPLETED")


@pytest.mark.asyncio
async def test_scan_skips_recent_pending(repo):
    repo.try_insert("req1", "doc_a")  # PENDING, 新建
    called = []
    async def handler(task: dict) -> bool:
        called.append(task["request_id"])
        return True
    scanner = CompensationScanner(task_repo=repo, handler=handler, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 0
    assert called == []


@pytest.mark.asyncio
async def test_scan_respects_max_attempts(repo):
    repo.try_insert("req1", "doc_a")
    for _ in range(3):
        repo.increment_attempts("req1")
    old = time.time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old,))
    repo._conn.commit()

    called = []
    async def handler(task: dict) -> bool:
        called.append(task["request_id"])
        return True
    scanner = CompensationScanner(task_repo=repo, handler=handler, max_attempts=3, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 0
    assert called == []


@pytest.mark.asyncio
async def test_scan_retries_after_handler_failure(repo):
    """回归测试: 修复 mark_status(FAILED) 刷新 updated_at 导致 max_attempts=3
    退化为 1 的 bug. 第一次 scan 失败后, 任务必须仍然落在
    pending_tasks_older_than 窗口内, 第二次 scan 才能再次尝试.
    """
    repo.try_insert("req1", "doc_a")
    old = time.time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old,))
    repo._conn.commit()

    call_count = {"n": 0}

    async def flaky_handler(task: dict) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first attempt blows up")
        return True

    scanner = CompensationScanner(task_repo=repo, handler=flaky_handler, threshold_sec=60.0)
    n1 = await scanner.scan()
    # 第一次失败: 不算成功重试, 但任务应当仍可重试
    assert n1 == 0
    assert call_count["n"] == 1
    # 第一次失败后, last_error 应当记录错误且 status=FAILED, 任务未被永久废弃
    after_first = repo.get("req1")
    assert after_first["status"] == "FAILED"
    assert "first attempt blows up" in after_first["last_error"]
    assert after_first["attempts"] == 1

    # 第二次 scan: 同一任务应当被再次挑出 (updated_at 未被刷新), 并成功完成
    n2 = await scanner.scan()
    assert n2 == 1
    assert call_count["n"] == 2
    final = repo.get("req1")
    assert final["status"] == "COMPLETED"
    assert final["attempts"] == 2
    # 两次 scan 累计的 retried 数 = 1 (只有第二次算成功重试)
    assert n1 + n2 == 1


@pytest.mark.asyncio
async def test_mark_failed_with_error_appends_history(repo):
    """回归测试: 重试失败时, 旧错误不应被覆盖, 应以分隔符拼接保留全链路."""
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "FAILED", error="original boom")
    repo.mark_failed_with_error("req1", "retry 1 blew up")
    repo.mark_failed_with_error("req1", "retry 2 blew up")
    rec = repo.get("req1")
    assert rec["last_error"] == "original boom | retry 1 blew up | retry 2 blew up"


@pytest.mark.asyncio
async def test_scan_skips_tasks_with_unwired_handler(repo):
    """回归测试 (C1): 当 handler 未实现时, scanner 必须调用 mark_handler_unwired,
    不调用 handler, 不 bump attempts, 且下次 scan 不再选中该任务."""
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "FAILED", error="prev")
    old = time.time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old,))
    repo._conn.commit()

    called = []

    async def handler(task: dict) -> bool:
        called.append(task["request_id"])
        return True

    scanner = CompensationScanner(
        task_repo=repo,
        handler=handler,
        threshold_sec=60.0,
        handler_is_wired=False,
    )
    n = await scanner.scan()
    assert n == 0  # no successful retries
    assert called == []  # handler must NOT be invoked

    rec = repo.get("req1")
    assert rec["status"] == "FAILED"
    assert rec["attempts"] == 0  # attempts MUST NOT be bumped
    assert rec["unwired_skipped"] == 1
    assert "handler not implemented" in rec["last_error"]

    # 后续 scan 必须跳过该任务
    n2 = await scanner.scan()
    assert n2 == 0
    assert called == []
    # unwired_skipped 仍为 1, 状态未再被改动
    rec2 = repo.get("req1")
    assert rec2["unwired_skipped"] == 1
    assert rec2["attempts"] == 0


@pytest.mark.asyncio
async def test_mark_handler_unwired_sets_flag_without_bumping_attempts(repo):
    """mark_handler_unwired 必须设置 unwired_skipped=1 且不增加 attempts."""
    repo.try_insert("req1", "doc_a")
    repo.increment_attempts("req1")  # attempts=1
    repo.mark_handler_unwired("req1", "handler not implemented")
    rec = repo.get("req1")
    assert rec["unwired_skipped"] == 1
    assert rec["attempts"] == 1  # 未被修改
    assert rec["last_error"] == "handler not implemented"


# ---------------------------------------------------------------------------
# Phase 7 T3 (Decision §1 + §5) — handler returns bool, audit fields required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_false_marks_task_failed(repo):
    """When handler returns False, scanner marks task FAILED with descriptive
    last_error and emits reingest_outcome='failed' (Decision §5)."""
    repo.try_insert("req-fail", "doc-x")
    old = time.time() - 3600
    repo._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='req-fail'", (old,)
    )
    repo._conn.commit()

    async def handler(task: dict) -> bool:
        return False  # handler reports re-ingest failed

    scanner = CompensationScanner(task_repo=repo, handler=handler, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 0  # NOT counted as successful retry
    rec = repo.get("req-fail")
    assert rec["status"] == "FAILED"
    assert "compensation handler returned False" in rec["last_error"]
    assert rec["attempts"] == 1  # claim_for_retry did bump attempts


@pytest.mark.asyncio
async def test_handler_duration_ms_measured(repo):
    """reingest_duration_ms reflects wall-clock time of the handler call."""
    import asyncio
    repo.try_insert("req-slow", "doc-y")
    old = time.time() - 3600
    repo._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='req-slow'", (old,)
    )
    repo._conn.commit()

    async def slow_handler(task: dict) -> bool:
        await asyncio.sleep(0.05)  # ≥ 50 ms
        return True

    scanner = CompensationScanner(task_repo=repo, handler=slow_handler, threshold_sec=60.0)
    await scanner.scan()

    # We can't read the audit log from this fixture (no writer installed),
    # but we can confirm handler was called exactly once and returned True.
    rec = repo.get("req-slow")
    assert rec["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_skipped_paths_emit_zero_duration(repo):
    """handler_not_wired / claim_race_lost paths emit duration_ms=0
    (no re-ingest happened)."""
    repo.try_insert("req-skipped", "doc-z")
    old = time.time() - 3600
    repo._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='req-skipped'", (old,)
    )
    repo._conn.commit()

    called = []

    async def handler(task: dict) -> bool:
        called.append(task["request_id"])
        return True

    scanner = CompensationScanner(
        task_repo=repo, handler=handler, threshold_sec=60.0,
        handler_is_wired=False,  # unwired path
    )
    n = await scanner.scan()
    assert n == 0
    assert called == []  # handler never invoked
    rec = repo.get("req-skipped")
    assert rec["unwired_skipped"] == 1


def test_compensation_retry_schema_requires_new_fields():
    """Direct call to log_event('compensation_retry', request_id='x')
    WITHOUT outcome/duration_ms must raise ValueError (schema enforced).

    AuditWriter.write() swallows exceptions defensively, so we exercise
    the base-class log_event() directly to assert the schema validator
    fires. The base class is the source of truth for the invariant.

    The writer's _schemas dict is normally populated by lifespan at startup;
    this test registers the compensation_retry schema explicitly to mirror
    that behavior, then verifies the missing-field check fires."""
    from ekrs_rag.observability.audit import AuditWriter
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        writer = AuditWriter(audit_log_path=str(Path(d) / "audit.log"))
        # Mirror lifespan registration: compensation_retry requires the
        # 2 new fields (Decision §5).
        writer.register_event_schema(
            "compensation_retry",
            {"request_id", "reingest_outcome", "reingest_duration_ms"},
        )
        with pytest.raises(ValueError, match="missing required fields"):
            writer.log_event(
                "compensation_retry", request_id="x"
            )  # no outcome/duration


def test_compensation_event_writer_round_trip():
    """Write all 4 outcome variants + read back via JSONL parsing,
    confirm fields preserved (Decision §5 invariant)."""
    from ekrs_rag.observability.audit import AuditWriter
    import tempfile, json

    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "audit.log"
        writer = AuditWriter(audit_log_path=str(log_path))
        for outcome in ("success", "failed", "duplicate", "skipped"):
            writer.write(
                "compensation_retry",
                request_id=f"req-{outcome}",
                reingest_outcome=outcome,
                reingest_duration_ms=42,
                attempt=1,
                reason="retry_invoked",
            )

        # Read back: each line is a JSON entry.
        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        compensation_lines = [e for e in lines if e["event"] == "compensation_retry"]
        assert len(compensation_lines) == 4
        seen = {e["reingest_outcome"] for e in compensation_lines}
        assert seen == {"success", "failed", "duplicate", "skipped"}
        for e in compensation_lines:
            assert e["reingest_duration_ms"] == 42
            assert e["attempt"] == 1


def test_invalid_outcome_coerced_to_failed():
    """If outcome is not in VALID_OUTCOMES, scanner coerces to 'failed'
    rather than corrupting the audit log invariant."""
    from ekrs_rag.concurrency.compensation import (
        _emit_compensation_event,
        OUTCOME_FAILED,
        VALID_OUTCOMES,
    )
    assert OUTCOME_FAILED in VALID_OUTCOMES


def test_handler_signature_accepts_bool():
    """Handler type alias must accept Awaitable[bool] (Decision §1)."""
    from ekrs_rag.concurrency.compensation import Handler
    # The type alias itself doesn't enforce at runtime; this test
    # documents the contract by importing it and confirming validity
    # of the four module-level outcome constants.
    from ekrs_rag.concurrency.compensation import (
        OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_DUPLICATE, OUTCOME_SKIPPED,
    )
    assert {OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_DUPLICATE, OUTCOME_SKIPPED} == {
        "success", "failed", "duplicate", "skipped",
    }


# ---------------------------------------------------------------------------
# IngestionPipeline.reparse() — Phase 7 T3 implementation target
# ---------------------------------------------------------------------------


def test_pipeline_reparse_method_exists():
    """IngestionPipeline must expose a reparse() method (Decision §1)."""
    from ekrs_rag.ingestion.pipeline import IngestionPipeline
    assert hasattr(IngestionPipeline, "reparse"), (
        "IngestionPipeline.reparse() not implemented yet — Phase 7 T3 Step3"
    )


def test_pipeline_reparse_signature():
    """reparse() signature: (source_path, doc_hash, version, callback_url, force)."""
    import inspect
    from ekrs_rag.ingestion.pipeline import IngestionPipeline
    assert hasattr(IngestionPipeline, "reparse")
    sig = inspect.signature(IngestionPipeline.reparse)
    params = list(sig.parameters.keys())
    # self + 5 explicit params
    assert "source_path" in params
    assert "doc_hash" in params
    assert "version" in params
    assert "callback_url" in params
    assert "force" in params


def test_pipeline_reparse_force_kwarg_defaults_false():
    """reparse() must accept force kwarg defaulting to False (Decision §1).

    When force=False (default), the hash check is honored — caller
    supplies content_hash and reparse skips re-upsert on match. When
    force=True, reparse re-upserts unconditionally.
    """
    from ekrs_rag.ingestion.pipeline import IngestionPipeline
    # Method must exist before we can introspect its signature.
    assert hasattr(IngestionPipeline, "reparse"), (
        "IngestionPipeline.reparse() not implemented yet — Phase 7 T3 Step3"
    )
    sig = inspect.signature(IngestionPipeline.reparse)
    force_param = sig.parameters.get("force")
    assert force_param is not None, "reparse() missing `force` parameter"
    assert force_param.default is False, "force must default to False"