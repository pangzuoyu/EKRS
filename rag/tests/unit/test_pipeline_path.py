"""T2 (updated for T9 contract): out-of-root output_path must produce
a failed IngestionOutcome instead of raising (T9 E1 helper-based refactor)."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ekrs_rag.ingestion.pipeline import IngestionPipeline


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_ingest_rejects_output_outside_root(tmp_path: Path) -> None:
    storage_root = tmp_path / "root"
    storage_root.mkdir()
    pipeline = IngestionPipeline(
        qdrant=MagicMock(),
        storage_path=storage_root,
        parser_token="x" * 32,
    )
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.output_path = str(outside)
    notification.callback_url = ""

    # T9 contract: ingest() returns IngestionOutcome; no raise.
    outcome = await pipeline.ingest(notification)
    assert outcome.rag_status == "failed"
    assert outcome.error_code == "output_path_out_of_scope"
    assert "SHARED_STORAGE_PATH" in outcome.error
