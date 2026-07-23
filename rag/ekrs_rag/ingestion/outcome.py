"""Ingestion outcome dataclass.

Replaces exception-based signaling of business failures
(JSONL missing / IR parse error / Qdrant failure) so the route
wrapper can map the outcome to TaskRepo status directly.

Phase 7 T3 widened `rag_status` from {success, failed} to the four
values below. `duplicate` and `business_failure` were added by the
reparse() path (idempotency short-circuit + distinct operator-routing
for ops-level errors). See `pipeline.py:reparse()` and the Phase 7
Decision §5 outcome table.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Phase 7 T3: reparse() adds 'duplicate' (idempotent skip) and
# 'business_failure' (ops-level error distinct from infra 'failed').
# Keep this tuple aligned with the validator check below.
_RAG_STATUS = Literal["success", "failed", "duplicate", "business_failure"]
_VALID_STATUSES = ("success", "failed", "duplicate", "business_failure")


@dataclass(frozen=True)
class IngestionOutcome:
    rag_status: _RAG_STATUS
    error: str | None = None
    error_code: str | None = None
    chunks_indexed: int = 0

    def __post_init__(self) -> None:
        if self.rag_status not in _VALID_STATUSES:
            raise ValueError(
                f"IngestionOutcome.rag_status must be one of "
                f"{list(_VALID_STATUSES)}; got {self.rag_status!r}"
            )
        if self.chunks_indexed < 0:
            raise ValueError(
                f"IngestionOutcome.chunks_indexed must be >= 0; got {self.chunks_indexed}"
            )