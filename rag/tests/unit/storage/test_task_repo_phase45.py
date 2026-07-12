"""Phase 4.5: TaskRepo schema extension for ingestion replay.

Adds source_path + payload_sha256 columns to support Task 12
(Ingestion Replay endpoint). The new columns allow replay handlers to
locate the original parser payload without scanning shared storage.
"""
import tempfile
from pathlib import Path

import pytest

from ekrs_rag.storage.task_repo import TaskRepo


@pytest.fixture
def repo(tmp_path):
    r = TaskRepo(db_path=str(tmp_path / "tasks.db"))
    r.init()
    yield r
    r.close()


def test_init_adds_source_path_and_sha256_columns(repo):
    """Schema migration adds source_path and payload_sha256 columns.

    Regression for Phase 4.5: pre-Phase4.5 DBs only have the original
    7 columns. After init(), both new columns must exist (whether the
    DB was fresh or migrated in-place).
    """
    cols = {r["name"] for r in repo._c().execute("PRAGMA table_info(tasks)").fetchall()}
    assert "source_path" in cols
    assert "payload_sha256" in cols


def test_try_insert_with_source_stores_both_fields(repo):
    """try_insert_with_source persists source_path and payload_sha256."""
    ok = repo.try_insert_with_source(
        "req-1", "doc-abc",
        source_path="/parsed_lib/doc-abc.jsonl",
        payload_sha256="abc123def456",
    )
    assert ok is True
    row = repo.get("req-1")
    assert row is not None
    assert row["source_path"] == "/parsed_lib/doc-abc.jsonl"
    assert row["payload_sha256"] == "abc123def456"


def test_try_insert_without_source_allows_null(repo):
    """Backward compat: try_insert without source info persists NULL.

    Pre-Phase4.5 callers that don't yet know about source_path /
    payload_sha256 must keep working.
    """
    ok = repo.try_insert("req-2", "doc-xyz")
    assert ok is True
    row = repo.get("req-2")
    assert row is not None
    assert row["source_path"] is None
    assert row["payload_sha256"] is None


def test_pre_phase45_rows_have_null_source_path(repo):
    """Pre-Phase4.5 rows (no source columns at insert time) → NULL.

    Simulates a replay scenario where a task was inserted before
    source_path/payload_sha256 existed. After migration the columns
    exist but their values are NULL — replay code uses this NULL to
    decide whether to attempt source-based recovery (Task 12 will
    branch on this and return 409 {reason: 'pre_phase5'} when NULL).
    """
    # Insert via direct SQL, omitting the new columns — mimics a
    # pre-Phase4.5 write against the upgraded schema.
    import time
    now = time.time()
    repo._c().execute(
        "INSERT INTO tasks(request_id, doc_id, status, attempts, "
        "created_at, updated_at) VALUES (?, ?, 'PENDING', 0, ?, ?)",
        ("legacy-req", "legacy-doc", now, now),
    )
    repo._c().commit()

    row = repo.get("legacy-req")
    assert row is not None
    assert row["source_path"] is None
    assert row["payload_sha256"] is None
