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

    with pytest.raises(ValueError, match="SHARED_STORAGE_PATH"):
        await pipeline.ingest(notification)
