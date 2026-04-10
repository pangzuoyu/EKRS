"""Metrics endpoint — placeholder for Phase 1.

Phase 5 adds full Prometheus counters/histograms.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["metrics"])

METRICS_PLACEHOLDER = """# EKRS RAG Service Metrics
# Phase 5 will add:
#   rag_ingestion_total{status="success|failed"}
#   rag_ingestion_duration_seconds
#   rag_retrieve_duration_seconds
#   rag_constraint_solve_duration_seconds
#   rag_queue_size
#   rag_worker_active
#   rag_db_operation_errors
"""


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    """Prometheus metrics endpoint (placeholder)."""
    return PlainTextResponse(content=METRICS_PLACEHOLDER, media_type="text/plain")
