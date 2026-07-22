"""Unit tests for QdrantManager (Phase 6B rewrite).

Fixes 3 production bugs from 6A final review:
- B1: search() replaced with query_points() (qdrant-client 1.17.1 API)
- B2: vectors_config["dense"] -> config.params.vectors["dense"]
- B3: upsert_chunks uses EmbeddingService for real dense+sparse vectors

Mock FlagEmbedding via EmbeddingService; mock QdrantClient.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import httpx
from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse

from ekrs_rag.retrieval.embedding_service import EmbeddingService
from ekrs_rag.retrieval.qdrant_client import QdrantManager


def _qdrant_not_found_exc() -> UnexpectedResponse:
    """Mimic the exception Qdrant raises for a missing collection (HTTP 404)."""
    return UnexpectedResponse(
        status_code=404,
        reason_phrase="Not Found",
        content=b"",
        headers=httpx.Headers(),
    )


@pytest.fixture
def mock_embedding_service() -> EmbeddingService:
    """EmbeddingService in real mode (not dummy), with fixed vectors."""
    svc = EmbeddingService(model_dir=Path("/fake/path"))
    svc._is_dummy = False  # Force real mode
    svc._model = MagicMock()
    # Real encode returns 1024d dense + sparse
    svc._model.encode.return_value = {
        "dense_vecs": [[0.1] * 1024, [0.2] * 1024],
        "lexical_weights": [{1: 0.5, 2: 0.3}, {3: 0.4}],
    }
    return svc


@pytest.fixture
def dummy_embedding_service() -> EmbeddingService:
    """EmbeddingService in dummy mode (no model)."""
    return EmbeddingService(model_dir=Path("/nonexistent"))  # is_dummy=True


def _make_qdrant(existing_size: int | None = None) -> MagicMock:
    """Build mock QdrantClient that returns CollectionInfo with given size."""
    client = MagicMock()
    if existing_size is None:
        # 6C-minor Finding 3: ensure_collection now narrows the inner except
        # to UnexpectedResponse/ApiException/ValueError/KeyError/AttributeError,
        # so the mock must raise UnexpectedResponse (the real 404 path) rather
        # than a bare Exception.
        client.get_collection.side_effect = _qdrant_not_found_exc()
    else:
        # B2 fix: real path is config.params.vectors["dense"].size
        info = SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=existing_size)}
                )
            )
        )
        client.get_collection.return_value = info
    return client


def test_ensure_collection_creates_dense_and_sparse(
    mock_embedding_service: EmbeddingService,
) -> None:
    """ensure_collection creates collection with dense (1024d) + sparse config."""
    client = _make_qdrant(existing_size=None)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.ensure_collection(vector_size=1024)

    # Verify create_collection was called with correct config
    client.create_collection.assert_called_once()
    args, kwargs = client.create_collection.call_args
    assert kwargs["collection_name"] == "rag_documents"
    # Dense config: 1024d cosine
    assert "dense" in kwargs["vectors_config"]
    dense_params = kwargs["vectors_config"]["dense"]
    assert dense_params.size == 1024
    assert dense_params.distance == models.Distance.COSINE
    # Sparse config present
    assert "sparse" in kwargs["sparse_vectors_config"]


def test_ensure_collection_recreates_on_dim_mismatch(
    mock_embedding_service: EmbeddingService,
) -> None:
    """B2 fix: dim mismatch (384 vs 1024) triggers delete + recreate."""
    client = _make_qdrant(existing_size=384)  # Old 6A dim
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.ensure_collection(vector_size=1024)

    client.delete_collection.assert_called_once_with("rag_documents")
    client.create_collection.assert_called_once()


def test_ensure_collection_no_recreate_when_dim_matches(
    mock_embedding_service: EmbeddingService,
) -> None:
    """When existing dim matches expected, no recreate."""
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.ensure_collection(vector_size=1024)

    client.delete_collection.assert_not_called()
    client.create_collection.assert_not_called()


def test_upsert_chunks_encodes_via_embedding_service(
    mock_embedding_service: EmbeddingService,
) -> None:
    """B3 fix: upsert_chunks calls EmbeddingService.encode on chunk.text."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(text="hello", scope_path=[], source_block_ids=["b1"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
        Chunk(text="world", scope_path=[], source_block_ids=["b2"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
    ]
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        n = mgr.upsert_chunks(chunks)

    assert n == 2
    # Verify the mock model was called with chunk texts
    mock_embedding_service._model.encode.assert_called_once()
    args, _ = mock_embedding_service._model.encode.call_args
    assert args[0] == ["hello", "world"]
    # Verify upsert received NamedVectors with dense + sparse
    upsert_call = client.upsert.call_args
    points = upsert_call.kwargs["points"]
    assert len(points) == 2
    assert "dense" in points[0].vector
    assert "sparse" in points[0].vector


def test_upsert_chunks_uses_named_vectors(
    mock_embedding_service: EmbeddingService,
) -> None:
    """upsert_chunks sends Qdrant sparse format {indices, values}."""
    from ekrs_shared.models import Chunk
    chunks = [
        Chunk(text="hi", scope_path=[], source_block_ids=["b1"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
    ]
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.upsert_chunks(chunks)

    points = client.upsert.call_args.kwargs["points"]
    sparse_vec = points[0].vector["sparse"]
    # D8: sparse is in Qdrant format {indices, values}. qdrant-client 1.17.1
    # wraps dict-shaped sparse into a SparseVector model on PointStruct, so we
    # check by attribute access (model_dump() keys would also work).
    assert hasattr(sparse_vec, "indices") and hasattr(sparse_vec, "values")
    assert isinstance(sparse_vec.indices, list)
    assert isinstance(sparse_vec.values, list)


def test_upsert_chunks_raises_when_embedding_service_dummy(
    dummy_embedding_service: EmbeddingService,
) -> None:
    """D1: upsert_chunks raises EmbeddingUnavailableError in dummy mode."""
    from ekrs_shared.models import Chunk
    from ekrs_rag.retrieval.embedding_service import EmbeddingUnavailableError
    chunks = [
        Chunk(text="x", scope_path=[], source_block_ids=["b1"],
              token_count=1, doc_hash="d1", version=1, page_numbers=[]),
    ]
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=dummy_embedding_service
        )
        with pytest.raises(EmbeddingUnavailableError, match="dummy mode"):
            mgr.upsert_chunks(chunks)


def test_search_calls_query_points(
    mock_embedding_service: EmbeddingService,
) -> None:
    """B1 fix: search uses query_points (not removed .search)."""
    client = _make_qdrant(existing_size=1024)
    # Mock query_points return
    client.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(payload={"text": "match"}, score=0.9),
        ]
    )
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        results = mgr.search(query_text="hello", top_k=5)

    assert client.query_points.called
    client.search.assert_not_called()  # B1: .search removed in 1.17.1
    assert results == [({"text": "match"}, 0.9)]


def test_search_encodes_query_text_via_service(
    mock_embedding_service: EmbeddingService,
) -> None:
    """search(query_text=...) calls EmbeddingService.encode on the text."""
    client = _make_qdrant(existing_size=1024)
    client.query_points.return_value = SimpleNamespace(points=[])
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.search(query_text="user query", top_k=10)

    encode_args = mock_embedding_service._model.encode.call_args[0]
    assert "user query" in encode_args[0]


def test_search_passes_named_vectors_to_query_points(
    mock_embedding_service: EmbeddingService,
) -> None:
    """search passes Prefetch (dense + sparse) + FusionQuery to query_points."""
    client = _make_qdrant(existing_size=1024)
    client.query_points.return_value = SimpleNamespace(points=[])
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.search(query_text="q", top_k=3)

    call_kwargs = client.query_points.call_args.kwargs
    # Two prefetches: dense + sparse
    assert isinstance(call_kwargs["prefetch"], list)
    assert len(call_kwargs["prefetch"]) == 2
    dense_prefetch = call_kwargs["prefetch"][0]
    sparse_prefetch = call_kwargs["prefetch"][1]
    assert dense_prefetch.using == "dense"
    assert sparse_prefetch.using == "sparse"
    # Fusion query
    assert call_kwargs["query"].fusion == models.Fusion.RRF


def test_ensure_collection_handles_qdrant_unreachable(
    mock_embedding_service: EmbeddingService,
) -> None:
    """If Qdrant is unreachable, ensure_collection handles exception gracefully."""
    client = MagicMock()
    client.get_collection.side_effect = ConnectionError("Qdrant down")
    client.create_collection.side_effect = ConnectionError("Qdrant down")

    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        # tenacity retries 3x, then raises; we just verify the retries happened
        with pytest.raises(ConnectionError):
            mgr.ensure_collection(vector_size=1024)
    # Verify retry happened
    assert client.get_collection.call_count >= 1


def test_search_passes_search_params_hnsw_ef(
    mock_embedding_service: EmbeddingService,
) -> None:
    """B1 fix: search uses query_points with SearchParams(hnsw_ef=128) for HNSW quality (6A Task 8)."""
    client = _make_qdrant(existing_size=1024)
    client.query_points.return_value = SimpleNamespace(points=[])
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.search(query_text="q", top_k=3)

    call_kwargs = client.query_points.call_args.kwargs
    # Preserve 6A Task 8 commit 033a8a3 optimization
    assert call_kwargs["search_params"].hnsw_ef == 128


def test_get_ingestion_status_returns_indexed_count(
    mock_embedding_service: EmbeddingService,
) -> None:
    """get_ingestion_status returns chunks_indexed count for a doc_hash."""
    client = _make_qdrant(existing_size=1024)
    # Mock scroll returning one match
    client.scroll.return_value = (
        [SimpleNamespace(payload={"version": 2})],
        None,  # next_page_offset
    )
    # Mock count returning N
    client.count.return_value = SimpleNamespace(count=42)

    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        status = mgr.get_ingestion_status(doc_hash="abc123")

    assert status.status == "success"
    assert status.chunks_indexed == 42
    assert status.version == 2


def test_delete_old_versions_calls_delete(
    mock_embedding_service: EmbeddingService,
) -> None:
    """T11: delete_old_versions uses Range(lt=keep_version) so future
    concurrent out-of-order ingests are preserved."""
    client = _make_qdrant(existing_size=1024)

    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.delete_old_versions(doc_hash="abc", keep_version=3)

    client.delete.assert_called_once()
    selector = client.delete.call_args.kwargs["points_selector"]
    must_keys = [c.key for c in selector.filter.must]
    must_not_keys = [c.key for c in (selector.filter.must_not or [])]
    assert "doc_hash" in must_keys
    assert "version" in must_keys
    # No must_not clause — Range replaces it.
    assert not must_not_keys


def test_delete_old_versions_filter_excludes_keep_version(
    mock_embedding_service: EmbeddingService,
) -> None:
    """T11: Range(lt=keep_version) excludes keep_version AND any future v > keep_version."""
    client = _make_qdrant(existing_size=1024)

    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=mock_embedding_service
        )
        mgr.delete_old_versions(doc_hash="doc-xyz", keep_version=2)

    selector = client.delete.call_args.kwargs["points_selector"]
    must_conditions = list(selector.filter.must)
    # doc_hash condition lives in must with the value passed in
    doc_hash_cond = next(c for c in must_conditions if c.key == "doc_hash")
    assert doc_hash_cond.match.value == "doc-xyz"
    # version condition lives in must with Range(lt=keep_version)
    version_cond = next(c for c in must_conditions if c.key == "version")
    assert version_cond.range.lt == 2


def test_search_logs_warning_when_dummy(
    dummy_embedding_service: EmbeddingService, caplog: pytest.LogCaptureFixture
) -> None:
    """search() in dummy mode logs WARN so operators see silent empty results."""
    import logging
    client = _make_qdrant(existing_size=1024)
    with patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
        mgr = QdrantManager(
            host="localhost", port=6333, embedding_service=dummy_embedding_service
        )
        with caplog.at_level(logging.WARNING, logger="ekrs_rag.retrieval.qdrant_client"):
            results = mgr.search(query_text="q", top_k=5)

    assert results == []
    assert any("dummy mode" in rec.message for rec in caplog.records)


# ---- Phase 6C T8 Finding #1: qdrant_write_failed audit emit ----

class TestQdrantWriteFailedAuditEmit:
    """Cover the 4 QdrantManager methods that perform real Qdrant operations.

    Each failure path must emit exactly one qdrant_write_failed audit event
    with the documented schema (collection + operation), then re-raise the
    original exception so retry/caller behavior is unchanged.
    """

    def _assert_emit(self, mock_writer: MagicMock, *, operation: str) -> None:
        """Verify qdrant_write_failed was emitted with the right operation."""
        assert mock_writer.write.called, "writer.write was not called"
        kwargs = mock_writer.write.call_args.kwargs
        # First positional or kw arg is the event name
        event = (
            mock_writer.write.call_args.args[0]
            if mock_writer.write.call_args.args
            else kwargs.get("event_type")
        )
        # The AuditWriter.write signature is (event_type, **kwargs).
        assert event == "qdrant_write_failed"
        assert kwargs.get("collection") == "rag_documents"
        assert kwargs.get("operation") == operation
        assert "error" in kwargs and kwargs["error"]
        assert "message" in kwargs

    def test_ensure_collection_emits_audit_event_on_failure(
        self, mock_embedding_service: EmbeddingService
    ) -> None:
        """ensure_collection emits qdrant_write_failed (operation=write) on Qdrant errors."""
        from ekrs_rag.retrieval import qdrant_client as qc_mod

        # get_collection is internally caught (treated as "not found"), so the
        # failure must surface in create_collection for the retry path to fire.
        client = MagicMock()
        client.get_collection.side_effect = ConnectionError("Qdrant down")
        client.create_collection.side_effect = ConnectionError("Qdrant down")

        mock_writer = MagicMock()
        with patch.object(qc_mod, "get_writer", return_value=mock_writer), \
             patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
            mgr = QdrantManager(
                host="localhost", port=6333,
                embedding_service=mock_embedding_service,
            )
            with pytest.raises(ConnectionError):
                mgr.ensure_collection(vector_size=1024)

        # Tenacity retries 3x → at least one emit (we don't pin exact count
        # to keep the test robust to retry-policy tweaks).
        assert mock_writer.write.call_count >= 1
        self._assert_emit(mock_writer, operation="write")

    def test_upsert_chunks_emits_audit_event_on_failure(
        self, mock_embedding_service: EmbeddingService
    ) -> None:
        """upsert_chunks emits qdrant_write_failed (operation=write) on Qdrant errors."""
        from ekrs_rag.retrieval import qdrant_client as qc_mod
        from ekrs_shared.models import Chunk

        chunks = [
            Chunk(text="hi", scope_path=[], source_block_ids=["b1"],
                  token_count=1, doc_hash="d1", version=1, page_numbers=[]),
        ]
        client = _make_qdrant(existing_size=1024)
        client.upsert.side_effect = ConnectionError("Qdrant down")

        mock_writer = MagicMock()
        with patch.object(qc_mod, "get_writer", return_value=mock_writer), \
             patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
            mgr = QdrantManager(
                host="localhost", port=6333,
                embedding_service=mock_embedding_service,
            )
            with pytest.raises(ConnectionError):
                mgr.upsert_chunks(chunks)

        assert mock_writer.write.call_count >= 1
        self._assert_emit(mock_writer, operation="write")

    def test_search_emits_audit_event_on_failure(
        self, mock_embedding_service: EmbeddingService
    ) -> None:
        """search emits qdrant_write_failed (operation=read) on Qdrant errors."""
        from ekrs_rag.retrieval import qdrant_client as qc_mod

        client = _make_qdrant(existing_size=1024)
        client.query_points.side_effect = ConnectionError("Qdrant down")

        mock_writer = MagicMock()
        with patch.object(qc_mod, "get_writer", return_value=mock_writer), \
             patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
            mgr = QdrantManager(
                host="localhost", port=6333,
                embedding_service=mock_embedding_service,
            )
            with pytest.raises(ConnectionError):
                mgr.search(query_text="q", top_k=5)

        # search() is NOT retry-wrapped → exactly 1 emit.
        assert mock_writer.write.call_count == 1
        self._assert_emit(mock_writer, operation="read")

    def test_delete_old_versions_emits_audit_event_on_failure(
        self, mock_embedding_service: EmbeddingService
    ) -> None:
        """delete_old_versions emits qdrant_write_failed (operation=delete) on errors."""
        from ekrs_rag.retrieval import qdrant_client as qc_mod

        client = _make_qdrant(existing_size=1024)
        client.delete.side_effect = ConnectionError("Qdrant down")

        mock_writer = MagicMock()
        with patch.object(qc_mod, "get_writer", return_value=mock_writer), \
             patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
            mgr = QdrantManager(
                host="localhost", port=6333,
                embedding_service=mock_embedding_service,
            )
            with pytest.raises(ConnectionError):
                mgr.delete_old_versions(doc_hash="abc", keep_version=3)

        assert mock_writer.write.call_count >= 1
        self._assert_emit(mock_writer, operation="delete")

    def test_successful_upsert_does_not_emit_qdrant_write_failed(
        self, mock_embedding_service: EmbeddingService
    ) -> None:
        """Happy-path upsert does NOT emit qdrant_write_failed (negative case)."""
        from ekrs_rag.retrieval import qdrant_client as qc_mod
        from ekrs_shared.models import Chunk

        chunks = [
            Chunk(text="hi", scope_path=[], source_block_ids=["b1"],
                  token_count=1, doc_hash="d1", version=1, page_numbers=[]),
        ]
        client = _make_qdrant(existing_size=1024)

        mock_writer = MagicMock()
        with patch.object(qc_mod, "get_writer", return_value=mock_writer), \
             patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
            mgr = QdrantManager(
                host="localhost", port=6333,
                embedding_service=mock_embedding_service,
            )
            n = mgr.upsert_chunks(chunks)

        assert n == 1
        # No qdrant_write_failed emitted on success
        for call in mock_writer.write.call_args_list:
            event = call.args[0] if call.args else call.kwargs.get("event_type")
            assert event != "qdrant_write_failed"

    def test_get_ingestion_status_emits_audit_event_on_count_failure(
        self, mock_embedding_service: EmbeddingService
    ) -> None:
        """get_ingestion_status emits qdrant_write_failed(operation="read") when
        client.count fails. The function still returns IngestionStatus(failed)
        so the route contract is preserved."""
        from ekrs_rag.retrieval import qdrant_client as qc_mod

        client = _make_qdrant(existing_size=1024)
        # scroll succeeds (returns the matched doc)
        client.scroll.return_value = (
            [SimpleNamespace(payload={"version": 1})],
            None,
        )
        # count fails
        client.count.side_effect = ConnectionError("Qdrant down on count")

        mock_writer = MagicMock()
        with patch.object(qc_mod, "get_writer", return_value=mock_writer), \
             patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
            mgr = QdrantManager(
                host="localhost", port=6333,
                embedding_service=mock_embedding_service,
            )
            status = mgr.get_ingestion_status(doc_hash="doc-count-fail")

        # Contract preserved: route still gets a structured failure, not 5xx.
        assert status is not None
        assert status.status == "failed"
        assert status.error is not None
        # But the failure is now observable in the audit log.
        assert mock_writer.write.call_count == 1
        self._assert_emit(mock_writer, operation="read")

    def test_get_ingestion_status_emits_audit_event_on_scroll_failure(
        self, mock_embedding_service: EmbeddingService
    ) -> None:
        """get_ingestion_status emits qdrant_write_failed(operation="read") when
        client.scroll fails. The function still returns IngestionStatus(failed)."""
        from ekrs_rag.retrieval import qdrant_client as qc_mod

        client = _make_qdrant(existing_size=1024)
        client.scroll.side_effect = ConnectionError("Qdrant down on scroll")

        mock_writer = MagicMock()
        with patch.object(qc_mod, "get_writer", return_value=mock_writer), \
             patch("ekrs_rag.retrieval.qdrant_client.QdrantClient", return_value=client):
            mgr = QdrantManager(
                host="localhost", port=6333,
                embedding_service=mock_embedding_service,
            )
            status = mgr.get_ingestion_status(doc_hash="doc-scroll-fail")

        assert status is not None
        assert status.status == "failed"
        assert mock_writer.write.call_count == 1
        self._assert_emit(mock_writer, operation="read")
