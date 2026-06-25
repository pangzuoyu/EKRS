import os
import tempfile
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
