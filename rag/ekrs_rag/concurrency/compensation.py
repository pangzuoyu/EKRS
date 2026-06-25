"""启动补偿扫描器: 重试 PENDING/FAILED 且超过 threshold_sec 的任务."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from ..storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class CompensationScanner:
    def __init__(
        self,
        task_repo: TaskRepo,
        handler: Handler,
        max_attempts: int = 3,
        threshold_sec: float = 60.0,
    ):
        self._repo = task_repo
        self._handler = handler
        self._max = max_attempts
        self._threshold = threshold_sec

    async def scan(self) -> int:
        tasks = self._repo.pending_tasks_older_than(self._threshold)
        retried = 0
        for task in tasks:
            if task["attempts"] >= self._max:
                logger.warning("Skip task %s: max attempts reached", task["request_id"])
                continue
            self._repo.mark_running(task["request_id"])
            self._repo.increment_attempts(task["request_id"])
            try:
                await self._handler(task)
                self._repo.mark_status(task["request_id"], "COMPLETED")
                retried += 1
            except Exception as e:
                logger.exception("Compensation handler failed for %s", task["request_id"])
                self._repo.mark_status(task["request_id"], "FAILED", error=str(e))
        return retried