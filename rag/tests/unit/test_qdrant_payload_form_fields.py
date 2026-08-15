"""T2 RED→GREEN: Qdrant payload must carry form_fields / column_headers.

Per Phase 12 plan §三:
- T2: chunker.py passthrough + Qdrant payload write
- QdrantManager.upsert_chunks payload includes form_fields + column_headers
  so R4 boost can read them at retrieval time

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md §三
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ekrs_rag.retrieval.embedding_service import EmbeddingService
from ekrs_rag.retrieval.qdrant_client import QdrantManager


@pytest.fixture
def mock_embedding_service() -> EmbeddingService:
    """EmbeddingService in real mode (not dummy), with fixed vectors."""
    svc = EmbeddingService(model_dir=Path("/fake/path"))
    svc._is_dummy = False  # Force real mode
    svc._model = MagicMock()  # type: ignore[assignment]
    svc._model.encode.return_value = {  # type: ignore[attr-defined]
        "dense_vecs": [[0.1] * 1024],
        "lexical_weights": [{1: 0.5}],
    }
    return svc


def _make_qdrant(existing_size: int = 1024) -> MagicMock:
    """Build mock QdrantClient that returns CollectionInfo with given size."""
    from types import SimpleNamespace

    client = MagicMock()
    info = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors={"dense": SimpleNamespace(size=existing_size)}
            )
        )
    )
    client.get_collection.return_value = info
    return client


def test_upsert_payload_carries_form_fields(mock_embedding_service: EmbeddingService) -> None:
    """T2 GREEN: payload["form_fields"] populated from chunk.form_fields."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(
            text="LOT 49 CHECKLIST",
            scope_path=["Appendix A"],
            source_block_ids=["b1"],
            token_count=2,
            doc_hash="d1",
            version=1,
            page_numbers=[3],
            form_fields=[{"key": "SYSTEM NO", "value": "Lot 49"}],
        )
    ]
    client = _make_qdrant()
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.upsert_chunks(chunks)

    points = client.upsert.call_args.kwargs["points"]
    assert points[0].payload["form_fields"] == [{"key": "SYSTEM NO", "value": "Lot 49"}]


def test_upsert_payload_carries_column_headers(mock_embedding_service: EmbeddingService) -> None:
    """T2 GREEN: payload["column_headers"] populated from chunk.column_headers."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(
            text="Item | Material",
            scope_path=["Appendix A"],
            source_block_ids=["b2"],
            token_count=2,
            doc_hash="d1",
            version=1,
            page_numbers=[3],
            column_headers=[{"index": 0, "header": "Item"}, {"index": 1, "header": "Material"}],
        )
    ]
    client = _make_qdrant()
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.upsert_chunks(chunks)

    points = client.upsert.call_args.kwargs["points"]
    assert points[0].payload["column_headers"] == [
        {"index": 0, "header": "Item"},
        {"index": 1, "header": "Material"},
    ]


def test_upsert_payload_defaults_to_empty_lists_for_legacy_chunks(
    mock_embedding_service: EmbeddingService,
) -> None:
    """T2: legacy chunks (no form_fields/column_headers) write empty lists, not None."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(
            text="legacy chunk",
            scope_path=[],
            source_block_ids=["b3"],
            token_count=1,
            doc_hash="d1",
            version=1,
            page_numbers=[],
        )
    ]
    client = _make_qdrant()
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.upsert_chunks(chunks)

    points = client.upsert.call_args.kwargs["points"]
    assert points[0].payload["form_fields"] == []
    assert points[0].payload["column_headers"] == []
    assert points[0].payload["form_fields"] is not None
    assert points[0].payload["column_headers"] is not None


def test_upsert_payload_carries_both_fields_concurrently(
    mock_embedding_service: EmbeddingService,
) -> None:
    """T2: form_fields + column_headers propagated together (multi-feature block)."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(
            text="LOT 49 | Item | Material",
            scope_path=["Appendix A"],
            source_block_ids=["b4"],
            token_count=3,
            doc_hash="d1",
            version=1,
            page_numbers=[3],
            form_fields=[{"key": "SYSTEM NO", "value": "Lot 49"}],
            column_headers=[{"index": 0, "header": "Item"}],
        )
    ]
    client = _make_qdrant()
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.upsert_chunks(chunks)

    points = client.upsert.call_args.kwargs["points"]
    assert points[0].payload["form_fields"] == [{"key": "SYSTEM NO", "value": "Lot 49"}]
    assert points[0].payload["column_headers"] == [{"index": 0, "header": "Item"}]