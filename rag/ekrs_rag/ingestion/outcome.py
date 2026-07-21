"""Ingestion outcome dataclass.

Replaces exception-based signaling of business failures
(JSONL missing / IR parse error / Qdrant failure) so the route
wrapper can map the outcome to TaskRepo status directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


_RAG_STATUS = Literal["success", "failed"]


@dataclass(frozen=True)
class IngestionOutcome:
    rag_status: _RAG_STATUS
    error: str | None = None
    error_code: str | None = None
    chunks_indexed: int = 0

    def __post_init__(self) -> None:
        if self.rag_status not in ("success", "failed"):
            raise ValueError(
                f"IngestionOutcome.rag_status must be 'success' or 'failed'; "
                f"got {self.rag_status!r}"
            )
        if self.chunks_indexed < 0:
            raise ValueError(
                f"IngestionOutcome.chunks_indexed must be >= 0; got {self.chunks_indexed}"
            )