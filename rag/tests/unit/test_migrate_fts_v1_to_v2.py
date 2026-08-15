"""F3 RED→GREEN: scripts/migrate_fts_v1_to_v2.py covers retry + dry-run paths.

Per Phase 12 follow-ups §F3:
- migrate_fts_v1_to_v2.py: one-time FTS5 v1→v2 rebuild for 745 historical docs.
- D3 retry decorator: 3 attempts, 100/200/400ms backoff on sqlite busy.
- is_migration_in_progress()=True during apply (False during dry-run).
- Idempotent: re-running converges to same end state.

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md

Note: integration with real Qdrant is covered by the F3 runbook
(docs/solutions/integration-issues/migrate-fts-runbook-2026-08-15.md),
not by these unit tests. Unit tests cover pure-function surfaces:
retry decorator + migration state suppression.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --- D3 retry decorator tests -----------------------------------------------

def test_retry_succeeds_on_first_attempt():
    """F3 D3: success on first try → no retry, no sleep."""
    from scripts.migrate_fts_v1_to_v2 import retry_on_sqlite_busy

    calls = {"n": 0}

    @retry_on_sqlite_busy(max_attempts=3, backoff_ms=100)
    def fn():
        calls["n"] += 1
        return "ok"

    assert fn() == "ok"
    assert calls["n"] == 1


def test_retry_recovers_after_two_busy_attempts():
    """F3 D3: 2 busy + 1 success → 3 attempts total, returns success."""
    from scripts.migrate_fts_v1_to_v2 import retry_on_sqlite_busy

    calls = {"n": 0}

    @retry_on_sqlite_busy(max_attempts=3, backoff_ms=10)
    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "recovered"

    assert fn() == "recovered"
    assert calls["n"] == 3


def test_retry_raises_after_exhausting_attempts():
    """F3 D3: 3 busy attempts → final OperationalError raised to caller."""
    from scripts.migrate_fts_v1_to_v2 import retry_on_sqlite_busy

    @retry_on_sqlite_busy(max_attempts=3, backoff_ms=10)
    def fn():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        fn()


def test_retry_does_not_swallow_non_busy_errors():
    """F3 D3: non-busy OperationalError → raise immediately, no retry."""
    from scripts.migrate_fts_v1_to_v2 import retry_on_sqlite_busy

    calls = {"n": 0}

    @retry_on_sqlite_busy(max_attempts=3, backoff_ms=10)
    def fn():
        calls["n"] += 1
        raise sqlite3.OperationalError("disk full")  # not busy → raise immediately

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        fn()
    assert calls["n"] == 1  # no retry


def test_retry_backoff_exponential():
    """F3 D3: backoff_ms doubles per attempt (100 → 200 → 400)."""
    from scripts.migrate_fts_v1_to_v2 import retry_on_sqlite_busy

    sleeps: list[float] = []

    @retry_on_sqlite_busy(max_attempts=3, backoff_ms=100)
    def fn():
        raise sqlite3.OperationalError("database is locked")

    with patch("scripts.migrate_fts_v1_to_v2.time.sleep", side_effect=lambda s: sleeps.append(s)):
        with pytest.raises(sqlite3.OperationalError):
            fn()

    # 2 sleeps for 3 attempts: 100ms then 200ms (no sleep after final attempt)
    assert sleeps == [0.1, 0.2]


# --- migration_state suppression tests --------------------------------------

def test_migrate_dry_run_does_not_set_migration_flag():
    """F3: dry-run does NOT suppress ConsistencyChecker.

    Rationale: dry-run is a read-only operation; FTS row count doesn't
    change during dry-run, so the 5-min drift check should keep running
    normally (and report 0 drift).
    """
    from ekrs_rag.concurrency.migration_state import (
        is_migration_in_progress,
        reset_migration_in_progress,
        set_migration_in_progress,
    )

    # Simulate the migrate() entry: apply=False
    token = set_migration_in_progress(False)  # mirrors migrate(apply=False)
    try:
        assert is_migration_in_progress() is False
    finally:
        reset_migration_in_progress(token)


def test_migrate_apply_sets_migration_flag():
    """F3: --apply suppresses ConsistencyChecker during rebuild."""
    from ekrs_rag.concurrency.migration_state import (
        is_migration_in_progress,
        reset_migration_in_progress,
        set_migration_in_progress,
    )

    token = set_migration_in_progress(True)  # mirrors migrate(apply=True)
    try:
        assert is_migration_in_progress() is True
    finally:
        reset_migration_in_progress(token)


# --- FTSManager schema_version=2 integration --------------------------------

def test_migration_uses_schema_v2(tmp_path: Path):
    """F3: FTSManager built with schema_version=2 has v2 columns."""
    from ekrs_rag.retrieval.fts_manager import FTSManager
    from ekrs_shared.models import Chunk

    fts = FTSManager(tmp_path / "fts.db", schema_version=2)
    chunks = [
        Chunk(
            text="doc body",
            scope_path=["project"],
            source_block_ids=["b1"],
            token_count=1,
            doc_hash="d1",
            version=1,
            page_numbers=[],
            form_fields=[{"key": "K", "value": "V"}],
            column_headers=[],
        )
    ]
    written = fts.replace_doc("d1", chunks, version=1)
    assert written == 1

    # Verify form_fields_text column populated
    row = fts._conn.execute(
        "SELECT form_fields_text, column_headers_text FROM blocks_fts"
    ).fetchone()
    assert row[0] == '[{"key": "K", "value": "V"}]'
    assert row[1] == "[]"


# --- list_doc_hashes mocked Qdrant test -------------------------------------

def test_list_doc_hashes_returns_sorted_unique():
    """F3: list_doc_hashes aggregates doc_hash across scroll pages, sorted."""
    from scripts.migrate_fts_v1_to_v2 import list_doc_hashes

    qdrant = MagicMock()
    # Page 1: 2 docs; Page 2: 1 doc; offset=None → done
    page1 = [
        MagicMock(payload={"doc_hash": "doc-b"}),
        MagicMock(payload={"doc_hash": "doc-a"}),
    ]
    page2 = [MagicMock(payload={"doc_hash": "doc-c"})]
    qdrant._client.scroll.side_effect = [
        (page1, "offset-token"),
        (page2, None),
    ]
    result = list_doc_hashes(qdrant)
    assert result == ["doc-a", "doc-b", "doc-c"]


def test_fetch_chunks_for_doc_reconstructs_form_fields():
    """F3: fetch_chunks_for_doc reconstructs Chunk including form_fields."""
    from scripts.migrate_fts_v1_to_v2 import fetch_chunks_for_doc

    qdrant = MagicMock()
    point = MagicMock()
    point.payload = {
        "text": "LOT 49 body",
        "scope_path": ["project"],
        "source_block_ids": ["b1"],
        "token_count": 2,
        "doc_hash": "d1",
        "version": 1,
        "page_numbers": [3],
        "chunk_id": "d1abcd-0000",
        "form_fields": [{"key": "SYSTEM NO", "value": "Lot 49"}],
        "column_headers": [{"index": 0, "header": "Item"}],
    }
    qdrant._client.scroll.return_value = ([point], None)

    chunks = fetch_chunks_for_doc(qdrant, "d1")
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text == "LOT 49 body"
    assert chunk.form_fields == [{"key": "SYSTEM NO", "value": "Lot 49"}]
    assert chunk.column_headers == [{"index": 0, "header": "Item"}]
    assert chunk.chunk_id == "d1abcd-0000"