"""Integration tests for Phase 4 notify flow.

End-to-end exercise of:
- Idempotency: same (trace_id, doc_hash, version) → second call returns "duplicate".
- Distributed lock: pre-acquired lock → POST returns "in_flight".
- Compensation: old FAILED task → scanner.scan() picks it up and retries.

Uses fakeredis.aioredis.FakeRedis (no real Redis), an in-memory TaskRepo via
tempfile, a mocked IngestionPipeline, and FastAPI TestClient with patched
lifespan side effects (real Redis + writable /var/lib/ekrs path are not
available in the test env).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from ekrs_rag.concurrency.redis_lock import RedisLock
from ekrs_rag.concurrency.compensation import CompensationScanner
from ekrs_rag.storage.task_repo import TaskRepo


PARSER_TOKEN = "change-me-to-a-secure-random-string-32chars"


@pytest.fixture
def client():
    """TestClient with patched lifespan: fake Redis, in-memory TaskRepo,
    mock ingestion pipeline. Real route handler runs end-to-end."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "tasks.db")
        fake_redis = fakeredis.aioredis.FakeRedis()
        repo = TaskRepo(db_path=db)
        # check_same_thread=False so the test thread can call repo methods
        # even though TaskClient runs lifespan in a worker thread.
        import sqlite3 as _sqlite3
        _orig_init = repo.init

        def _init_thread_safe() -> None:
            _orig_init()
            assert repo._conn is not None
            repo._conn.close()
            repo._conn = _sqlite3.connect(
                db, check_same_thread=False, isolation_level=None
            )
            repo._conn.row_factory = _sqlite3.Row

        repo.init = _init_thread_safe  # type: ignore[assignment]
        repo.init()
        lock = RedisLock(fake_redis)

        mock_pipeline = MagicMock()
        mock_pipeline.ingest = AsyncMock()

        with patch("ekrs_rag.main.QdrantManager"), \
             patch("ekrs_rag.main.setup_logging"), \
             patch("ekrs_rag.main.TaskRepo", return_value=repo), \
             patch("ekrs_rag.main.aioredis.from_url", return_value=fake_redis), \
             patch("ekrs_rag.main.RedisLock", return_value=lock), \
             patch("ekrs_rag.main.CompensationScanner") as mock_scanner_cls:
            mock_scanner = MagicMock()
            mock_scanner.scan = AsyncMock(return_value=0)
            mock_scanner_cls.return_value = mock_scanner

            from ekrs_rag.main import app
            # Wire route deps via dependency_overrides (set_X deleted in T3)
            from ekrs_rag.api.routes.ingestion import (
                get_pipeline, get_redis_lock, get_task_repo,
            )
            app.dependency_overrides[get_pipeline] = lambda: mock_pipeline
            app.dependency_overrides[get_redis_lock] = lambda: lock
            app.dependency_overrides[get_task_repo] = lambda: repo

            with TestClient(app) as c:
                yield c, repo, lock, mock_pipeline


def _notify_payload(doc_hash: str, trace_id: str = "t", version: int = 1) -> dict:
    return {
        "trace_id": trace_id,
        "doc_hash": doc_hash,
        "version": version,
        "output_path": "/tmp/x.jsonl",
        "callback_url": "",
    }


def test_duplicate_request_id_is_idempotent(client):
    """Same (trace_id, doc_hash, version) twice → second call returns 'duplicate'."""
    c, _repo, _lock, _pipeline = client
    headers = {"X-Parser-Token": PARSER_TOKEN}
    payload = _notify_payload(doc_hash="doc_a", trace_id="t1")

    r1 = c.post("/v1/ingestion/notify", json=payload, headers=headers)
    r2 = c.post("/v1/ingestion/notify", json=payload, headers=headers)

    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["status"] == "queued"
    assert r2.json()["status"] == "duplicate"
    assert r1.json()["doc_hash"] == "doc_a"
    assert r2.json()["doc_hash"] == "doc_a"


def test_lock_prevents_concurrent_ingest(client):
    """Pre-acquired lock on doc_hash → POST returns 'in_flight'."""
    c, _repo, lock, _pipeline = client

    token = asyncio.run(lock.acquire("lock:ingest:doc_b", ttl_sec=10))
    assert token is not None  # sanity: lock was actually acquired

    headers = {"X-Parser-Token": PARSER_TOKEN}
    payload = _notify_payload(doc_hash="doc_b", trace_id="t2")

    r = c.post("/v1/ingestion/notify", json=payload, headers=headers)
    assert r.status_code == 202
    assert r.json()["status"] == "in_flight"


def test_compensation_picks_up_old_failed(client):
    """Manually-inserted old FAILED task (attempts < max_attempts) is retried by scan()."""
    _c, repo, _lock, _pipeline = client

    # Simulate a FAILED task from 1h ago with 2 attempts (max is 3).
    repo.try_insert("old_req", "doc_c")
    repo.mark_status("old_req", "FAILED", error="prev")
    repo.increment_attempts("old_req")
    repo.increment_attempts("old_req")
    old = time.time() - 3600
    repo._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='old_req'", (old,)
    )
    repo._conn.commit()

    counter = {"n": 0}

    async def handler(task: dict) -> None:
        counter["n"] += 1

    scanner = CompensationScanner(
        task_repo=repo, handler=handler, max_attempts=3, threshold_sec=60.0
    )
    n = asyncio.run(scanner.scan())

    assert n == 1
    assert counter["n"] == 1
    final = repo.get("old_req")
    assert final is not None
    assert final["status"] == "COMPLETED"
    assert final["attempts"] == 3


def test_lock_acquire_none_skips_try_insert(client):
    """回归测试 (I1): 当 Redis 锁已被持有时, /notify 必须直接返回 in_flight,
    不能在 tasks 表里留下未完成的 PENDING 行."""
    c, repo, lock, _pipeline = client

    token = asyncio.run(lock.acquire("lock:ingest:doc_d", ttl_sec=10))
    assert token is not None  # sanity: lock was actually acquired

    headers = {"X-Parser-Token": PARSER_TOKEN}
    payload = _notify_payload(doc_hash="doc_d", trace_id="t_d")

    r = c.post("/v1/ingestion/notify", json=payload, headers=headers)
    assert r.status_code == 202
    assert r.json()["status"] == "in_flight"

    # 关键断言: tasks 表不应被本次 notify 写入任何行
    rows = repo._conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
    assert rows["n"] == 0