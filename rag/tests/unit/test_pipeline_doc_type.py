"""Task C: IngestionPipeline reads index.json → classifies → stamps doc_type
on every produced Chunk. 3 tests cover happy path, missing index.json, and
classifier exception isolation."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ekrs_rag.ingestion.doc_classifier import load_rules
from ekrs_rag.ingestion.pipeline import IngestionPipeline


def _seed_data_jsonl(output_path: Path) -> None:
    """Write a minimal valid JSONL block that chunker accepts."""
    (output_path / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text",'
        '"content":{"raw":"hello","md_preview":"hello"},'
        '"metadata":{"page_number":1}}\n'
    )


def _make_pipeline(tmp_path: Path, output_path: Path) -> IngestionPipeline:
    """Build an IngestionPipeline with stubbed Qdrant (no real upsert)."""
    qdrant = MagicMock()
    qdrant.get_ingestion_status.return_value = None
    qdrant.upsert_chunks.return_value = 1
    return IngestionPipeline(
        qdrant=qdrant, storage_path=tmp_path, parser_token="x" * 32,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_classifies_from_index_json(tmp_path: Path) -> None:
    """End-to-end: pipeline reads index.json, classifies, stamps Chunk."""
    output_path = tmp_path / "Lot049 NCR Status Report"
    output_path.mkdir()
    (output_path / "index.json").write_text(
        json.dumps({"file_name": "Lot049 NCR Status Report.doc"})
    )
    _seed_data_jsonl(output_path)

    pipeline = _make_pipeline(tmp_path, output_path)
    notification = MagicMock()
    notification.doc_hash = "d1"
    notification.version = 1
    notification.output_path = str(output_path)
    notification.callback_url = ""
    notification.trace_id = "trace-1"

    outcome = await pipeline.ingest(notification)
    assert outcome.rag_status == "success"

    # Verify chunk was stamped with classified doc_type
    chunks_arg = pipeline._qdrant.upsert_chunks.call_args[0][0]
    assert all(c.doc_type == "lot_checklist" for c in chunks_arg)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_missing_index_json_defaults_to_unknown(tmp_path: Path) -> None:
    """No index.json → doc_type='unknown' (no failure)."""
    output_path = tmp_path / "no_index_here"
    output_path.mkdir()
    _seed_data_jsonl(output_path)

    pipeline = _make_pipeline(tmp_path, output_path)
    notification = MagicMock()
    notification.doc_hash = "d1"
    notification.version = 1
    notification.output_path = str(output_path)
    notification.callback_url = ""
    notification.trace_id = "trace-1"

    outcome = await pipeline.ingest(notification)
    assert outcome.rag_status == "success"

    chunks_arg = pipeline._qdrant.upsert_chunks.call_args[0][0]
    assert all(c.doc_type == "unknown" for c in chunks_arg)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_classifier_exception_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classifier raises → caught, doc_type='unknown', ingestion succeeds."""
    output_path = tmp_path / "broken_index"
    output_path.mkdir()
    (output_path / "index.json").write_text(json.dumps({"file_name": "Lot049.doc"}))
    _seed_data_jsonl(output_path)

    pipeline = _make_pipeline(tmp_path, output_path)

    # Force classifier to raise — simulates a corrupt rules file or runtime bug.
    # Patch the pipeline module's local binding (from .doc_classifier import
    # classify creates a module-level alias in pipeline; re-assigning the
    # symbol on doc_classifier would not affect that alias).
    import ekrs_rag.ingestion.pipeline as pipeline_mod

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("simulated classifier crash")

    monkeypatch.setattr(pipeline_mod, "classify", boom)

    notification = MagicMock()
    notification.doc_hash = "d1"
    notification.version = 1
    notification.output_path = str(output_path)
    notification.callback_url = ""
    notification.trace_id = "trace-1"

    outcome = await pipeline.ingest(notification)
    # Pipeline does NOT fail; defaults to 'unknown'
    assert outcome.rag_status == "success"
    chunks_arg = pipeline._qdrant.upsert_chunks.call_args[0][0]
    assert all(c.doc_type == "unknown" for c in chunks_arg)