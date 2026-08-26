"""Phase 13c T3 D1 — internal TaskRepo row.status → IngestionStatus.status mapper.

TaskRepo (aiosqlite) stores ingestion state with 5 internal values:
    "queued" / "running" / "pending" / "failed" / "completed"

IngestionStatus (公开契约, Pydantic Literal enum) accepts only 4 values:
    "pending" / "processing" / "success" / "failed"

This module owns the boundary translation. Producers (ingest pipeline,
get_status route) call ``map_row_status_to_ingestion_status`` before
constructing an IngestionStatus so the public Literal guard never rejects
internal values.

Why a module instead of an inline dict literal at each call site:
- 5 paths × 2 call sites (notify pipeline + get_status route) is enough
  surface area that a typo in any one would silently break contract.
- Single source of truth for future status additions (e.g. ``cancelled``).
"""
from __future__ import annotations

from typing import Literal

# Public Literal alias — matches IngestionStatus.status in shared models.
IngestionStatusLiteral = Literal["pending", "processing", "success", "failed"]

# Internal TaskRepo row.status (lowercased) → public enum value.
# Defensive default = "failed" (loud, not silent).
_ROW_STATUS_TO_INGESTION_STATUS: dict[str, IngestionStatusLiteral] = {
    "queued": "pending",
    "running": "processing",
    "pending": "pending",
    "failed": "failed",
    "completed": "success",
}


def map_row_status_to_ingestion_status(
    row_status: str,
) -> IngestionStatusLiteral:
    """Translate TaskRepo row.status to IngestionStatus.status.

    Args:
        row_status: internal status string (lowercased; case-insensitive
            lookup is NOT performed — callers are expected to ``.lower()``
            first as ``ingestion.py:594`` does).

    Returns:
        One of the 4 IngestionStatus Literal values. Unknown inputs default
        to ``"failed"`` (defensive: silent mapping to "pending" would mask
        bugs; loud default surfaces them in audit/observability).

    See Also:
        ``ekrs_shared.models.IngestionStatus`` for the public Literal enum.
    """
    return _ROW_STATUS_TO_INGESTION_STATUS.get(
        row_status.lower() if row_status else "",
        "failed",
    )


__all__ = [
    "IngestionStatusLiteral",
    "map_row_status_to_ingestion_status",
]