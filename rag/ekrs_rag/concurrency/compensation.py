"""启动补偿扫描器: 重试 PENDING/FAILED 且超过 threshold_sec 的任务."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from ..storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)

# Phase 7 T3 (Decision §1 + §5): handler now returns bool to signal re-ingest
# outcome. The scanner measures wall-clock duration and emits the new
# required audit fields `reingest_outcome` + `reingest_duration_ms`.
# Exception paths (handler raises) still flow through the existing
# `handler_failed` emit, with outcome="failed".
Handler = Callable[[dict[str, Any]], Awaitable[bool]]

# Valid outcome values per Decision §5. Used by tests + emit sites.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_SKIPPED = "skipped"
VALID_OUTCOMES = frozenset(
    {OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_DUPLICATE, OUTCOME_SKIPPED}
)


class CompensationScanner:
    def __init__(
        self,
        task_repo: TaskRepo,
        handler: Handler,
        max_attempts: int = 3,
        threshold_sec: float = 60.0,
        handler_is_wired: bool = True,
    ):
        self._repo = task_repo
        self._handler = handler
        self._max = max_attempts
        self._threshold = threshold_sec
        # When False, scanner calls mark_handler_unwired on each task instead
        # of claim_for_retry + handler invocation. attempts is not bumped and
        # pending_tasks_older_than will skip the row on subsequent scans.
        self._handler_is_wired = handler_is_wired

    async def scan(self) -> int:
        tasks = self._repo.pending_tasks_older_than(self._threshold)
        retried = 0
        for task in tasks:
            if not self._handler_is_wired:
                # Stub handler path: skip the task permanently without
                # bumping attempts (audit trail shows last_error but not
                # "completed" for work that never ran).
                logger.warning(
                    "Compensation handler not wired; marking %s as unwired-skipped",
                    task["request_id"],
                )
                self._repo.mark_handler_unwired(
                    task["request_id"],
                    "handler not implemented (Phase 4.5)",
                )
                _emit_compensation_event(
                    task["request_id"],
                    reason="handler_not_wired",
                    outcome=OUTCOME_SKIPPED,
                    duration_ms=0,
                )
                continue
            # SQL 内再次校验 status / attempts / updated_at, 避免两个并发 scan
            # 拿到同一行都进入 handler. claim 失败 = 输掉竞争 / 行已不在窗口内,
            # 当作 "被别人接管了" 静默跳过.
            claimed = self._repo.claim_for_retry(
                task["request_id"],
                max_attempts=self._max,
                threshold_sec=self._threshold,
            )
            if not claimed:
                logger.warning("Skip task %s: lost claim race", task["request_id"])
                _emit_compensation_event(
                    task["request_id"],
                    reason="claim_race_lost",
                    outcome=OUTCOME_SKIPPED,
                    duration_ms=0,
                )
                continue
            attempt = int(task.get("attempts", 0)) + 1
            started = time.monotonic()
            try:
                ok = await self._handler(task)
                duration_ms = int((time.monotonic() - started) * 1000)
                # The handler returns True on success/duplicate and False on
                # hard failure. The scanner does NOT differentiate success vs
                # duplicate at this layer — handler does that — so we trust
                # the bool and map to OUTCOME_SUCCESS / OUTCOME_FAILED.
                if ok:
                    self._repo.mark_status(task["request_id"], "COMPLETED")
                    retried += 1
                else:
                    self._repo.mark_failed_with_error(
                        task["request_id"],
                        "compensation handler returned False (re-ingest failed)",
                    )
                _emit_compensation_event(
                    task["request_id"],
                    attempt=attempt,
                    reason="retry_invoked",
                    outcome=OUTCOME_SUCCESS if ok else OUTCOME_FAILED,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                duration_ms = int((time.monotonic() - started) * 1000)
                logger.exception("Compensation handler failed for %s", task["request_id"])
                self._repo.mark_failed_with_error(task["request_id"], str(e))
                _emit_compensation_event(
                    task["request_id"],
                    attempt=attempt,
                    reason=f"handler_failed:{e.__class__.__name__}",
                    outcome=OUTCOME_FAILED,
                    duration_ms=duration_ms,
                )
        return retried


def _emit_compensation_event(
    request_id: str,
    attempt: int | None = None,
    reason: str | None = None,
    outcome: str = OUTCOME_SKIPPED,
    duration_ms: int = 0,
) -> None:
    """Best-effort emit of `compensation_retry` to the audit log.

    Mirrors the `get_writer()` guard used elsewhere: missing writer
    in test fixtures is silently skipped.

    Phase 7 T3 (Decision §5): `outcome` + `duration_ms` are now REQUIRED
    by the registered schema. `outcome` is one of {success, failed,
    duplicate, skipped}; `duration_ms` is wall-clock milliseconds for
    the re-ingest attempt (0 for paths where no re-ingest ran, e.g.
    handler_not_wired / claim_race_lost).
    """
    if outcome not in VALID_OUTCOMES:
        # Defensive: don't silently corrupt the audit log; coerce to
        # "failed" so the invariant (string in {success,failed,duplicate,
        # skipped}) is preserved. The handler-side error is also surfaced
        # via `reason`.
        outcome = OUTCOME_FAILED
    try:
        from ..observability.audit import get_writer

        writer = get_writer()
    except Exception:  # pragma: no cover — module-load safety
        return
    if writer is None:
        return
    kwargs: dict[str, Any] = {
        "request_id": request_id,
        "reingest_outcome": outcome,
        "reingest_duration_ms": int(duration_ms),
    }
    if attempt is not None:
        kwargs["attempt"] = attempt
    if reason is not None:
        kwargs["reason"] = reason
    writer.write("compensation_retry", **kwargs)