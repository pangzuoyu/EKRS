"""F2 RED→GREEN: ConsistencyChecker suppresses drift checks during migration.

Per Phase 12 follow-ups §F2 (D1 plan):
- ConsistencyChecker._check_once skips when migration_state flag is True.
- migration_state is a process-local ContextVar (no schema table needed).
- Returns 0 (no drift counted) and emits no audit/counter when suppressed.

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from ekrs_rag.concurrency.consistency_checker import ConsistencyChecker
from ekrs_rag.concurrency.migration_state import (
    is_migration_in_progress,
    reset_migration_in_progress,
    set_migration_in_progress,
)


def _make_checker() -> ConsistencyChecker:
    fts = MagicMock()
    qdrant = MagicMock()
    audit = MagicMock()
    metrics = MagicMock()
    return ConsistencyChecker(
        fts=fts, qdrant=qdrant, audit_writer=audit,
        metrics_collector=metrics, interval_s=300,
    )


@pytest.mark.unit
def test_checker_default_state_no_migration():
    """F2: default state is no migration in progress."""
    assert is_migration_in_progress() is False


@pytest.mark.unit
def test_checker_migration_flag_round_trip():
    """F2: set + reset round-trip restores prior value."""
    assert is_migration_in_progress() is False
    token = set_migration_in_progress(True)
    assert is_migration_in_progress() is True
    reset_migration_in_progress(token)
    assert is_migration_in_progress() is False


@pytest.mark.asyncio
async def test_checker_skips_drift_audit_when_migration_in_progress():
    """F2: _check_once skips when migration in progress; no audit emit."""
    from ekrs_rag.concurrency import migration_state

    checker = _make_checker()
    # Mock fts.count_active returns 0 (drift) but migration suppresses
    checker._fts.count_active.return_value = 0
    checker._qdrant.count_points.return_value = 100  # drift = 100

    token = set_migration_in_progress(True)
    try:
        drift = await checker._check_once()
    finally:
        reset_migration_in_progress(token)

    assert drift == 0  # suppressed — no drift reported
    checker._audit_writer.write.assert_not_called()
    # fts.count_active should NOT have been called (early return)
    checker._fts.count_active.assert_not_called()
    checker._qdrant.count_points.assert_not_called()


@pytest.mark.asyncio
async def test_checker_normal_drift_when_no_migration():
    """F2: without migration flag, drift detection runs as before (Phase 10 baseline)."""
    checker = _make_checker()
    checker._fts.count_active.return_value = 5
    checker._qdrant.count_points.return_value = 10  # drift = 5

    drift = await checker._check_once()

    assert drift == 5
    checker._audit_writer.write.assert_called_once()
    # write(event_type, **kwargs) — event_type is positional arg
    args, kwargs = checker._audit_writer.write.call_args
    assert args[0] == "fts_consistency_drift"
    assert kwargs["drift_count"] == 5


@pytest.mark.asyncio
async def test_checker_after_migration_clear_resumes_drift_detection():
    """F2: after reset, drift detection resumes immediately."""
    checker = _make_checker()
    checker._fts.count_active.return_value = 5
    checker._qdrant.count_points.return_value = 10  # drift = 5

    token = set_migration_in_progress(True)
    drift_during = await checker._check_once()
    reset_migration_in_progress(token)
    drift_after = await checker._check_once()

    assert drift_during == 0  # suppressed
    assert drift_after == 5   # resumed