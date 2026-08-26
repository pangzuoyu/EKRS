"""Unit tests for ingestion row_status → IngestionStatus.status mapper.

Phase 13c T3 D1: ingest pipeline内部 task_state (TaskRepo row['status']) 是
"queued"/"running"/"pending"/"failed"/"completed" 5 路, 但 IngestionStatus
公开契约的 Literal enum 是 "pending"/"processing"/"success"/"failed" 4 路。
mapper 函数负责内部→外部映射, 防止 get_status 误报 (Phase 12 GPU PoC
发现的 pending 误报 pre-existing bug, ingestion.py:599-606).
"""

from ekrs_rag.services.ingestion_mapper import (
    map_row_status_to_ingestion_status,
)


class TestMapRowStatusToIngestionStatus:
    """5-path coverage — every TaskRepo row_status must round-trip correctly."""

    def test_queued_maps_to_pending(self):
        """queued → pending (Phase 13c D1 contract: 'queued' is internal-only)."""
        assert map_row_status_to_ingestion_status("queued") == "pending"

    def test_running_maps_to_processing(self):
        """running → processing (Phase 13c D1 contract)."""
        assert map_row_status_to_ingestion_status("running") == "processing"

    def test_pending_maps_to_pending(self):
        """pending → pending (identity — get_status synthesized branch)."""
        assert map_row_status_to_ingestion_status("pending") == "pending"

    def test_failed_maps_to_failed(self):
        """failed → failed (KEY FIX: pre-13c this returned 'pending' wrongly)."""
        assert map_row_status_to_ingestion_status("failed") == "failed"

    def test_completed_maps_to_success(self):
        """completed → success (terminal happy path)."""
        assert map_row_status_to_ingestion_status("completed") == "success"

    def test_unknown_defaults_to_failed(self):
        """Defensive: unknown row_status → 'failed' (loud, not silent)."""
        assert map_row_status_to_ingestion_status("garbage_unknown") == "failed"
        assert map_row_status_to_ingestion_status("") == "failed"


class TestMapperReturnType:
    """Type contract: return value must be one of the 4 Literal enum values."""

    def test_returns_literal_value(self):
        """All 5 known inputs return Literal-enum-compatible strings."""
        valid = {"pending", "processing", "success", "failed"}
        for row_status in ("queued", "running", "pending", "failed", "completed"):
            assert map_row_status_to_ingestion_status(row_status) in valid