"""Unit tests for pipeline.ingest() returning IngestionOutcome.

T9: E1 helper-based refactor. ingest() must return an IngestionOutcome;
each business-failure branch emits a failed outcome with a stable
error_code. Callback transport errors are swallowed by
_send_callback_safely and must not propagate.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from ekrs_rag.ingestion.outcome import IngestionOutcome
from ekrs_rag.ingestion.pipeline import IngestionPipeline


def _notification(doc_hash="d1", version=1, output_path=None, callback_url=""):
    n = MagicMock()
    n.doc_hash = doc_hash
    n.version = version
    n.output_path = str(output_path) if output_path else "/dev/null"
    n.callback_url = callback_url
    n.trace_id = "trace-x"
    return n


def _seed_jsonl(path) -> None:
    """Create the directory and write a one-block JSONL file matching the
    DocumentBlockIR schema (doc_id/block_id/type/content/raw/metadata)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text",'
        '"content":{"raw":"hello","md_preview":"hello","structured":{}},'
        '"metadata":{"page_number":1,"heading_path":[]}}\n'
    )


@pytest.mark.asyncio
async def test_ingest_returns_outcome_success(tmp_path):
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    qdrant = MagicMock()
    qdrant.get_ingestion_status = MagicMock(return_value=None)
    qdrant.upsert_chunks = MagicMock(return_value=1)
    qdrant.delete_old_versions = MagicMock(return_value=0)

    pipeline = IngestionPipeline(
        qdrant=qdrant, storage_path=storage, parser_token="x" * 32,
    )
    outcome = await pipeline.ingest(_notification(output_path=doc_dir))
    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1


@pytest.mark.asyncio
async def test_ingest_returns_outcome_failed_on_missing_jsonl(tmp_path):
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    doc_dir.mkdir(parents=True)  # no data.jsonl

    pipeline = IngestionPipeline(
        qdrant=MagicMock(), storage_path=storage, parser_token="x" * 32,
    )
    outcome = await pipeline.ingest(_notification(output_path=doc_dir))
    assert outcome.rag_status == "failed"
    assert outcome.error_code == "jsonl_missing"


@pytest.mark.asyncio
async def test_ingest_returns_outcome_failed_on_out_of_scope_path(tmp_path):
    """Pipeline-level defense-in-depth: output_path outside SHARED_STORAGE_PATH."""
    storage = tmp_path / "root"
    storage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    pipeline = IngestionPipeline(
        qdrant=MagicMock(), storage_path=storage, parser_token="x" * 32,
    )
    outcome = await pipeline.ingest(_notification(output_path=outside))
    assert outcome.rag_status == "failed"
    assert outcome.error_code == "output_path_out_of_scope"


@pytest.mark.asyncio
async def test_ingest_returns_outcome_success_when_already_indexed(tmp_path):
    """Idempotency: if Qdrant reports success for this version, return success."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    doc_dir.mkdir(parents=True)

    qdrant = MagicMock()
    existing = MagicMock()
    existing.status = "success"
    existing.version = 1
    existing.chunks_indexed = 7
    qdrant.get_ingestion_status = MagicMock(return_value=existing)
    qdrant.upsert_chunks = MagicMock()

    pipeline = IngestionPipeline(
        qdrant=qdrant, storage_path=storage, parser_token="x" * 32,
    )
    outcome = await pipeline.ingest(_notification(output_path=doc_dir))
    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 7
    qdrant.upsert_chunks.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_failed_outcome_does_not_break_on_callback_4xx(
    monkeypatch, tmp_path,
):
    """T9: a CallbackURLBlockedError from validate_callback_url must be
    swallowed by _send_callback_safely (best-effort callback)."""
    storage = tmp_path / "root"
    doc_dir = storage / "doc1" / "v1"
    _seed_jsonl(doc_dir)

    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    qdrant = MagicMock()
    qdrant.get_ingestion_status = MagicMock(return_value=None)
    qdrant.upsert_chunks = MagicMock(return_value=1)
    qdrant.delete_old_versions = MagicMock(return_value=0)

    pipeline = IngestionPipeline(
        qdrant=qdrant, storage_path=storage, parser_token="x" * 32,
    )
    # callback_url uses http which won't match the https-only allowlist,
    # so validate_callback_url raises CallbackURLBlockedError. The
    # safe wrapper must absorb that and return success anyway.
    outcome = await pipeline.ingest(
        _notification(
            output_path=doc_dir,
            callback_url="http://unauthorized.example.com/cb",
        ),
    )
    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1