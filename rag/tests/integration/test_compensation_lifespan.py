"""Integration test for real CompensationScanner wiring via FastAPI lifespan.

Unlike test_ingestion_phase4.py which mocks CompensationScanner.scan to keep
the test focused on route-level idempotency / lock behaviour, this file
exercises the full startup compensation code path:
  lifespan() -> construct TaskRepo + RedisLock + CompensationScanner
                -> scan() runs against the seeded DB
                -> real handler is invoked, marks task COMPLETED.

The handler is patched via ekrs_rag.main._get_compensation_handler so the
real closure in lifespan() is replaced without rewriting production code.
The DB is seeded BEFORE TestClient is entered so lifespan() startup picks up
the row and processes it.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from ekrs_rag.storage.task_repo import TaskRepo


PARSER_TOKEN = "change-me-to-a-secure-random-string-32chars"


@pytest.fixture
def lifespan_with_seeded_db(tmp_path, monkeypatch):
    """TestClient with the real CompensationScanner wired (no mocking).

    The handler is monkeypatched to a recorder via _get_compensation_handler.
    The DB is seeded with one PENDING task (older than threshold_sec=60)
    BEFORE lifespan startup so the scanner picks it up on entry.
    """
    db = os.path.join(str(tmp_path), "tasks.db")
    fake_redis = fakeredis.aioredis.FakeRedis()

    # Pre-seed the DB so lifespan's startup scan() picks up the row.
    repo_for_seed = TaskRepo(db_path=db)
    repo_for_seed.init()
    repo_for_seed.try_insert("seeded_req", "seeded_doc")
    old = time.time() - 3600
    repo_for_seed._conn.execute(
        "UPDATE tasks SET updated_at=? WHERE request_id='seeded_req'", (old,)
    )
    repo_for_seed._conn.commit()
    repo_for_seed.close()

    # Monkeypatch the handler lookup so lifespan() picks up our recorder.
    called: list[str] = []

    async def recording_handler(task: dict) -> bool:
        called.append(task["request_id"])
        return True  # Phase 7 T3 (Decision §5): handlers must return bool

    monkeypatch.setattr("ekrs_rag.main._get_compensation_handler", lambda: recording_handler)
    monkeypatch.setattr("ekrs_rag.main.COMPENSATION_HANDLER_IMPLEMENTED", True)
    monkeypatch.setattr("ekrs_rag.main.settings.TASK_DB_PATH", db)
    monkeypatch.setattr("ekrs_rag.main.settings.DOCUMENTS_DB_PATH", os.path.join(str(tmp_path), "documents.db"))
    monkeypatch.setattr("ekrs_rag.main.settings.PARSER_TOKEN", PARSER_TOKEN)

    # Patch Qdrant + logging so lifespan doesn't try real connections.
    with patch("ekrs_rag.main.QdrantManager"), \
         patch("ekrs_rag.main.setup_logging"), \
         patch("ekrs_rag.main.aioredis.from_url", return_value=fake_redis):
        from ekrs_rag.main import app
        with TestClient(app):
            # By this point, lifespan startup has run the real scanner.
            pass

    return db, called


def test_lifespan_runs_real_compensation_scanner(lifespan_with_seeded_db):
    """回归测试 (I2): lifespan 启动时必须真实运行 CompensationScanner.scan,
    命中预先植入的 PENDING 旧任务, 调用 handler, 并把任务标为 COMPLETED."""
    db, called = lifespan_with_seeded_db

    # The recording handler was invoked for the seeded row.
    assert called == ["seeded_req"]

    # Reopen the DB and verify the seeded row is now COMPLETED — meaning
    # the real scanner wired by lifespan() ran against it on startup.
    repo = TaskRepo(db_path=db)
    repo.init()
    final = repo.get("seeded_req")
    assert final is not None
    assert final["status"] == "COMPLETED"
    assert final["attempts"] == 1