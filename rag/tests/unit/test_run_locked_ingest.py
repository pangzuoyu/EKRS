"""Unit tests for the top-level _run_locked_ingest helper.

T10: extract the closure from routes/ingestion.py into a module-level
function so its outcome→status mapping can be unit-tested directly.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from ekrs_rag.api.routes.ingestion import _run_locked_ingest
from ekrs_rag.ingestion.outcome import IngestionOutcome


@pytest.mark.asyncio
async def test_run_locked_ingest_marks_completed_on_success():
    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(
        return_value=IngestionOutcome(rag_status="success", chunks_indexed=5),
    )
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()

    await _run_locked_ingest(
        pipeline=pipeline,
        repo=repo,
        lock=lock,
        lock_key="k",
        lock_token="t",
        notification=notification,
        request_id="r1",
    )

    repo.mark_status.assert_called_once_with("r1", "COMPLETED")
    repo.mark_failed_with_error.assert_not_called()
    lock.release.assert_called_once_with("k", "t")


@pytest.mark.asyncio
async def test_run_locked_ingest_marks_failed_on_failed_outcome():
    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(
        return_value=IngestionOutcome(
            rag_status="failed",
            error="JSONL missing",
            error_code="jsonl_missing",
        ),
    )
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()

    await _run_locked_ingest(
        pipeline=pipeline,
        repo=repo,
        lock=lock,
        lock_key="k",
        lock_token="t",
        notification=notification,
        request_id="r1",
    )

    repo.mark_status.assert_not_called()
    repo.mark_failed_with_error.assert_called_once_with("r1", "JSONL missing")
    lock.release.assert_called_once_with("k", "t")


@pytest.mark.asyncio
async def test_run_locked_ingest_marks_failed_on_unhandled_exception():
    """If pipeline.ingest raises (true system exception, not business failure),
    _run_locked_ingest marks FAILED, re-raises, and still releases the lock."""
    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(side_effect=RuntimeError("boom"))
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()

    with pytest.raises(RuntimeError):
        await _run_locked_ingest(
            pipeline=pipeline, repo=repo, lock=lock,
            lock_key="k", lock_token="t", notification=notification, request_id="r1",
        )

    repo.mark_failed_with_error.assert_called_once()
    assert "boom" in repo.mark_failed_with_error.call_args.args[1]
    lock.release.assert_called_once_with("k", "t")