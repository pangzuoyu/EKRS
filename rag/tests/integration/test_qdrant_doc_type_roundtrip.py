"""Task C defect fix: Qdrant upsert MUST persist doc_type in payload so
retriever._payload_to_chunk reads it back. Tasks 4-6 all assumed this
worked but never asserted it — bug found during Task 7 verification.

Round-trip test strategy: mock QdrantManager._client.upsert to capture
the PointStruct list, then verify the payload carries doc_type.

Coordinator note: real QdrantManager.upsert_chunks calls
``self._embedding_service.encode(texts)`` (NOT ``embed_chunks``), so the
MagicMock is wired to ``encode`` and ``to_qdrant_sparse`` per the real
EmbeddingService signature in ``retrieval/embedding_service.py``.
"""
from unittest.mock import MagicMock

import pytest
from ekrs_shared.models import Chunk
from ekrs_rag.retrieval.qdrant_client import QdrantManager


def _make_chunk(doc_type: str | None) -> Chunk:
    return Chunk(
        text="hello",
        scope_path=["project"],
        source_block_ids=["b1"],
        doc_hash="abc",
        version=1,
        doc_type=doc_type,
    )


def _build_manager() -> tuple[QdrantManager, list]:
    """Build a bare QdrantManager (bypass __init__) with stubbed internals.

    Returns (manager, captured_points) where ``captured_points`` is the
    list the mocked ``_client.upsert`` will append to. Each captured
    PointStruct exposes its ``payload`` as a dict attribute, which is
    what the asserts below read.
    """
    manager = QdrantManager.__new__(QdrantManager)
    manager._client = MagicMock()
    manager._collection_name = "test_collection"
    captured_points: list = []

    def capture_upsert(*, collection_name, points):
        captured_points.extend(points)
        return MagicMock()

    manager._client.upsert = capture_upsert

    # Stub embedding service: encode() returns one EncodedVector-shaped
    # MagicMock per input text; to_qdrant_sparse echoes a SparseVector;
    # is_dummy = False so upsert_chunks proceeds past the dummy-mode guard.
    manager._embedding_service = MagicMock()
    manager._embedding_service.is_dummy = False
    manager._embedding_service.encode.return_value = [
        MagicMock(dense=[0.0] * 4, sparse={0: 0.5}),
    ]
    manager._embedding_service.to_qdrant_sparse.return_value = MagicMock()
    return manager, captured_points


@pytest.mark.integration
def test_upsert_chunks_persists_doc_type_in_payload():
    """QdrantManager.upsert_chunks MUST write chunk.doc_type into payload."""
    manager, captured_points = _build_manager()
    chunks = [_make_chunk("lot_checklist")]
    manager.upsert_chunks(chunks)

    assert len(captured_points) == 1
    payload = captured_points[0].payload
    assert payload["doc_type"] == "lot_checklist"


@pytest.mark.integration
def test_upsert_chunks_legacy_chunk_doc_type_is_none():
    """Legacy chunks (doc_type=None) round-trip as None — preserved behavior."""
    manager, captured_points = _build_manager()
    chunks = [_make_chunk(None)]
    manager.upsert_chunks(chunks)

    payload = captured_points[0].payload
    assert payload["doc_type"] is None