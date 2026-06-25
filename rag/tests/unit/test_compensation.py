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