"""FTS5 ↔ Qdrant consistency checker — Phase 10 T10a-2.

5-minute background task that compares FTS active row count with Qdrant
total point count. On drift, emits `fts_consistency_drift` audit event and
increments `ekrs_index_consistency_drift_total` counter. Does NOT auto-repair
(avoid accidental deletion; parent plan §T10a-2 row mandates detect-only).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prometheus_client import Counter
    from ekrs_rag.observability.audit import AuditWriter
    from ekrs_rag.retrieval.fts_manager import FTSManager
    from ekrs_rag.retrieval.qdrant_client import QdrantManager

logger = logging.getLogger(__name__)


class ConsistencyChecker:
    """Periodic FTS-vs-Qdrant drift detector.

    Runs in lifespan via `asyncio.create_task(checker.run_forever())`.
    Cancellation propagates cleanly through `asyncio.sleep` so lifespan
    shutdown stops the task without leaks.
    """

    def __init__(
        self,
        fts: "FTSManager",
        qdrant: "QdrantManager",
        audit_writer: "AuditWriter | None",
        metrics_collector: Any | None,
        *,
        interval_s: int = 300,
    ) -> None:
        self._fts = fts
        self._qdrant = qdrant
        self._audit_writer = audit_writer
        self._metrics = metrics_collector
        self._interval_s = interval_s

    async def run_forever(self) -> None:
        """Loop: sleep → check → repeat. Cancel via task.cancel()."""
        while True:
            await asyncio.sleep(self._interval_s)
            await self._check_once()

    async def _check_once(self) -> int:
        """Run one consistency check.

        Returns:
            drift_count (int): |fts_active - qdrant_total|. 0 = in sync.
        """
        try:
            fts_count = self._fts.count_active()
            qdrant_count = self._qdrant.count_points()
        except Exception as exc:
            logger.warning("consistency_check_failed: %s", exc)
            return 0

        drift = abs(fts_count - qdrant_count)
        if drift == 0:
            return 0

        logger.warning(
            "fts_consistency_drift: fts=%d qdrant=%d drift=%d",
            fts_count, qdrant_count, drift,
        )
        if self._audit_writer is not None:
            self._audit_writer.write(
                "fts_consistency_drift",
                drift_count=drift,
                fts_count=fts_count,
                qdrant_count=qdrant_count,
            )
        if self._metrics is not None and hasattr(self._metrics, "drift_total"):
            self._metrics.drift_total.inc()
        return drift
