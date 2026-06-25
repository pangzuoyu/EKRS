import os
import tempfile
import time
import pytest

from ekrs_rag.storage.task_repo import TaskRepo


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        r = TaskRepo(db_path=db)
        r.init()
        yield r


def test_init_creates_table(repo):
    rows = repo._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchall()
    assert len(rows) == 1


def test_try_insert_idempotent(repo):
    assert repo.try_insert("req1", "doc_a") is True
    assert repo.try_insert("req1", "doc_a") is False  # UNIQUE 触发


def test_mark_status_updates(repo):
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "RUNNING")
    assert repo.get("req1")["status"] == "RUNNING"


def test_mark_status_with_error(repo):
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "FAILED", error="boom")
    assert repo.get("req1")["last_error"] == "boom"


def test_increment_attempts(repo):
    repo.try_insert("req1", "doc_a")
    n1 = repo.increment_attempts("req1")
    n2 = repo.increment_attempts("req1")
    assert n1 == 1
    assert n2 == 2


def test_pending_tasks_older_than(repo):
    repo.try_insert("req1", "doc_a")
    repo.try_insert("req2", "doc_b")
    repo.mark_status("req1", "FAILED", error="x")
    # 设 updated_at 为 1 小时前
    old_ts = __import__("time").time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old_ts,))
    repo._conn.commit()
    found = repo.pending_tasks_older_than(60.0)
    ids = [t["request_id"] for t in found]
    assert "req1" in ids
    assert "req2" not in ids  # PENDING 但不旧


def test_claim_for_retry_respects_eligibility(repo):
    """回归测试: claim_for_retry 必须在 SQL 内同时校验 status / attempts / updated_at.

    不能仅仅靠 pending_tasks_older_than 过滤: 两个并发 scan 会拿到相同行,
    都调 claim_for_retry, 必须靠 SQL 的 rowcount=0 让后到者退出.
    """
    now = time.time()
    old = now - 3600

    # Case A: FAILED + attempts=2 + max=3 + updated_at 是新的 (在阈值内)
    # 应被 claim_for_retry 拒绝 (threshold 不满足).
    repo._conn.execute(
        "INSERT INTO tasks(request_id, doc_id, status, attempts, created_at, updated_at) "
        "VALUES (?, ?, 'FAILED', 2, ?, ?)",
        ("req_a", "doc_a", now, now),
    )
    repo._conn.commit()

    # Case B: FAILED + attempts=4 + max=3 (max_attempts 已用尽)
    repo._conn.execute(
        "INSERT INTO tasks(request_id, doc_id, status, attempts, created_at, updated_at) "
        "VALUES (?, ?, 'FAILED', 4, ?, ?)",
        ("req_b", "doc_b", old, old),
    )
    repo._conn.commit()

    # Case C: FAILED + attempts=0 + max=3 + updated_at 旧 (满足所有条件)
    repo._conn.execute(
        "INSERT INTO tasks(request_id, doc_id, status, attempts, created_at, updated_at) "
        "VALUES (?, ?, 'FAILED', 0, ?, ?)",
        ("req_c", "doc_c", old, old),
    )
    repo._conn.commit()

    threshold = now - 60.0  # threshold_sec=60, so updated_at must be < now-60

    # Case A: 拒绝 (updated_at 太新)
    ok_a = repo.claim_for_retry("req_a", max_attempts=3, threshold_sec=60.0)
    assert ok_a is False
    assert repo.get("req_a")["status"] == "FAILED"
    assert repo.get("req_a")["attempts"] == 2

    # Case B: 拒绝 (attempts >= max_attempts)
    ok_b = repo.claim_for_retry("req_b", max_attempts=3, threshold_sec=60.0)
    assert ok_b is False
    assert repo.get("req_b")["status"] == "FAILED"
    assert repo.get("req_b")["attempts"] == 4

    # Case C: 成功 (满足全部条件)
    ok_c = repo.claim_for_retry("req_c", max_attempts=3, threshold_sec=60.0)
    assert ok_c is True
    rec_c = repo.get("req_c")
    assert rec_c["status"] == "RUNNING"
    assert rec_c["attempts"] == 1

    # 二次 claim 同一行: 状态已变为 RUNNING, 不再满足 status IN (PENDING, FAILED)
    ok_c2 = repo.claim_for_retry("req_c", max_attempts=3, threshold_sec=60.0)
    assert ok_c2 is False
    assert repo.get("req_c")["attempts"] == 1  # attempts 不再被自增


def test_mark_running_sets_status(repo):
    """mark_running 是 mark_status 的薄包装 — 仍需覆盖以免被重构掉."""
    repo.try_insert("req1", "doc_a")
    repo.mark_running("req1")
    assert repo.get("req1")["status"] == "RUNNING"


def test_close_resets_conn_and_is_idempotent(tmp_path):
    """close 应关闭连接并把 _conn 重置为 None, 再次调用是 no-op."""
    db = str(tmp_path / "close.db")
    r = TaskRepo(db_path=db)
    r.init()
    assert r._conn is not None
    r.close()
    assert r._conn is None
    # 再次调用不应抛错
    r.close()
    assert r._conn is None
