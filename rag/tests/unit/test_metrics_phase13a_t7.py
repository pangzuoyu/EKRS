"""Phase 13a T7 — queue depth + task duration + rejection counter + boot recovery.

TDD red: this file is added before the metrics + recovery land. Tests
fail on:
- Histogram bucket list [10,30,60,120,300,600,1800] exact match (eng-review
  Issue 5 校正: hard boundary assertion)
- rag_task_queue_depth Gauge declared
- rag_doc_rejections_total Counter with `reason` label declared
- TaskRepo.recover_in_flight() updates queued/running → pending
- ConsistencyChecker drift firing: FTS count != Qdrant count → audit emit
  AND ekrs_index_consistency_drift_total counter increment
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ekrs_rag.observability.metrics import METRICS


# ---------------------------------------------------------------------------
# Metric definitions + bucket boundaries
# ---------------------------------------------------------------------------


def test_task_duration_histogram_buckets() -> None:
    """``rag_task_duration_seconds`` buckets are exactly [10,30,60,120,300,600,1800].

    Phase 13a T7 (eng-review Issue 5 校正): hard boundary assertion. Any
    drift in bucket boundaries silently coarsens the latency distribution
    that operators use for SLO compliance checks. The bucket list is the
    contract — tests catch accidental reordering or addition.
    """
    h = METRICS.task_duration_seconds
    expected = (10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)
    # prometheus_client stores the bucket boundaries on the instance
    # `_kwargs` dict as the "buckets" key (passed through from the
    # constructor). `_upper_bounds` adds an implicit +Inf bucket, so we
    # use `_kwargs` to read the user-declared boundary list verbatim.
    actual = h._kwargs.get("buckets")  # type: ignore[attr-defined]
    assert actual == expected, (
        f"rag_task_duration_seconds buckets must equal {expected}; "
        f"got {actual}. Bucket drift silently breaks SLO compliance "
        f"checks — fix in the metric definition, not here."
    )


def test_task_queue_depth_gauge_exists() -> None:
    """``rag_task_queue_depth`` Gauge declared (no labels)."""
    g = METRICS.task_queue_depth
    # prometheus_client Gauge: assert type + name
    from prometheus_client import Gauge

    assert isinstance(g, Gauge)
    assert "_task_queue_depth" in g._name  # type: ignore[attr-defined]


def test_doc_rejections_counter_has_reason_label() -> None:
    """``rag_doc_rejections_total`` Counter with `reason` label declared."""
    c = METRICS.doc_rejections_total
    from prometheus_client import Counter

    assert isinstance(c, Counter)
    # labels() is what callers use to attach the reason value
    assert c._labelnames == ("reason",), (  # type: ignore[attr-defined]
        f"doc_rejections_total must have a single 'reason' label; "
        f"got {c._labelnames}"  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# TaskRepo recovery (boot-time pending-state reset)
# ---------------------------------------------------------------------------


def test_task_repo_recover_in_flight_returns_pending(tmp_path: Path) -> None:
    """TaskRepo.recover_in_flight(): UPDATE queued+running → pending.

    Phase 13a T7.2: after a pod restart, in-flight tasks (queued/running)
    need to land in a re-notify-able state. Idempotency keys in the parser
    prevent duplicate ingestion; re-notify replays safely.
    """
    from ekrs_rag.storage.task_repo import TaskRepo

    db = tmp_path / "tasks.db"
    repo = TaskRepo(db_path=str(db))
    repo.init()

    # Insert 3 rows: 1 completed (must NOT be touched) + 1 queued + 1 running
    repo.try_insert("req-c", "doc-c")
    repo.mark_status("req-c", "COMPLETED")
    repo.try_insert("req-q", "doc-q")
    repo.mark_status("req-q", "QUEUED")
    repo.try_insert("req-r", "doc-r")
    repo.mark_running("req-r")
    repo.try_insert("req-f", "doc-f")
    repo.mark_failed_with_error("req-f", "prior error")

    n_recovered = repo.recover_in_flight()

    assert n_recovered == 2, (
        f"expected 2 rows recovered (queued + running); got {n_recovered}"
    )
    # The completed and failed rows stay put
    assert repo.get("req-c")["status"] == "COMPLETED"
    assert repo.get("req-f")["status"] == "FAILED"
    # The two in-flight rows land at PENDING so re-notify replays them
    assert repo.get("req-q")["status"] == "PENDING"
    assert repo.get("req-r")["status"] == "PENDING"


def test_task_repo_recover_in_flight_idempotent(tmp_path: Path) -> None:
    """Calling recover_in_flight twice in a row does NOT reset the
    second time — pending rows are no longer in (queued, running)."""
    from ekrs_rag.storage.task_repo import TaskRepo

    db = tmp_path / "tasks.db"
    repo = TaskRepo(db_path=str(db))
    repo.init()

    repo.try_insert("req-q", "doc-q")
    repo.mark_status("req-q", "QUEUED")

    assert repo.recover_in_flight() == 1
    # Second call: row is now PENDING, no longer in the WHERE clause
    assert repo.recover_in_flight() == 0


# ---------------------------------------------------------------------------
# ConsistencyChecker drift firing (T10.2 sync per eng-review Issue 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_checker_detects_drift() -> None:
    """When FTS active count != Qdrant total count, ConsistencyChecker
    emits `fts_consistency_drift` audit event AND increments
    `ekrs_index_consistency_drift_total` counter.

    Phase 13a T7.3: hard-assert the firing path (T10.2 sync per eng-review
    Issue 5). Mock the FTS/Qdrant managers + audit writer; the assertion
    is on the side effects, not on the drift count itself.
    """
    from ekrs_rag.concurrency.consistency_checker import ConsistencyChecker

    # Mock FTS: 100 active rows
    fts = MagicMock()
    fts.count_active = MagicMock(return_value=100)
    # Mock Qdrant: 95 points (drift = 5)
    qdrant = MagicMock()
    qdrant.count_points = MagicMock(return_value=95)

    # Mock AuditWriter: capture writes
    audit = MagicMock()
    audit_writes: list[tuple[str, dict]] = []

    def _capture(event: str, **kwargs: Any) -> bool:
        audit_writes.append((event, kwargs))
        return True

    audit.write = MagicMock(side_effect=_capture)

    # Mock metrics collector with `drift_total` counter
    metrics = MagicMock()
    metrics.drift_total = MagicMock()

    checker = ConsistencyChecker(
        fts=fts,
        qdrant=qdrant,
        audit_writer=audit,
        metrics_collector=metrics,
        interval_s=300,
    )

    drift = await checker._check_once()

    # Drift count matches what we set up
    assert drift == 5

    # Audit emit fired exactly once with the right event + count
    drift_writes = [(e, k) for (e, k) in audit_writes if e == "fts_consistency_drift"]
    assert len(drift_writes) == 1, (
        f"Expected exactly 1 fts_consistency_drift audit emit; "
        f"got {len(drift_writes)}: {drift_writes}"
    )
    event, kwargs = drift_writes[0]
    assert kwargs["drift_count"] == 5

    # Counter incremented exactly once
    assert metrics.drift_total.inc.call_count >= 1, (
        f"ekrs_index_consistency_drift_total counter must increment on "
        f"drift; got {metrics.drift_total.inc.call_count} calls"
    )


@pytest.mark.asyncio
async def test_concurrency_checker_no_drift_no_emit() -> None:
    """When FTS count == Qdrant count, no audit + no counter increment.

    Sanity guard: the firing path must not fire false positives during
    steady-state operation (otherwise the dashboard lights up for no
    reason and operators stop trusting the drift alert).
    """
    from ekrs_rag.concurrency.consistency_checker import ConsistencyChecker

    fts = MagicMock()
    fts.count_active = MagicMock(return_value=100)
    qdrant = MagicMock()
    qdrant.count_points = MagicMock(return_value=100)
    audit = MagicMock()
    metrics = MagicMock()
    metrics.drift_total = MagicMock()

    checker = ConsistencyChecker(
        fts=fts, qdrant=qdrant,
        audit_writer=audit, metrics_collector=metrics,
    )

    drift = await checker._check_once()
    assert drift == 0
    audit.write.assert_not_called()
    metrics.drift_total.inc.assert_not_called()