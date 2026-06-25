import os
import tempfile
import time
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
    async def handler(task: dict) -> None:
        called.append(task["request_id"])

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
    async def handler(task: dict) -> None:
        called.append(task["request_id"])
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
    async def handler(task: dict) -> None:
        called.append(task["request_id"])
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

    async def flaky_handler(task: dict) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first attempt blows up")

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