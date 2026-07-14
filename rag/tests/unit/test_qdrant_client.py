"""Behavior tests for the Qdrant client wrapper."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ekrs_shared.models import Chunk
from ekrs_rag.retrieval import qdrant_client as qdrant_module
from ekrs_rag.retrieval.qdrant_client import QdrantManager


class _FakeClient:
    def __init__(self):
        self.collection = None
        self.created = []
        self.deleted_collections = []
        self.upserted = []
        self.scrolled = ([], None)
        self.count_result = SimpleNamespace(count=0)
        self.search_results = []
        self.point_deletes = []

    def get_collection(self, name):
        if isinstance(self.collection, Exception):
            raise self.collection
        return self.collection

    def create_collection(self, **kwargs):
        self.created.append(kwargs)

    def delete_collection(self, name):
        self.deleted_collections.append(name)

    def upsert(self, **kwargs):
        self.upserted.append(kwargs)

    def scroll(self, **kwargs):
        if isinstance(self.scrolled, Exception):
            raise self.scrolled
        return self.scrolled

    def count(self, **kwargs):
        return self.count_result

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        return self.search_results

    def delete(self, **kwargs):
        self.point_deletes.append(kwargs)


@pytest.fixture
def manager(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(qdrant_module, "QdrantClient", lambda **kwargs: client)
    return QdrantManager(collection_name="test_chunks", vector_size=3), client


def _chunk(index: int = 1) -> Chunk:
    return Chunk(
        text=f"Temperature limit {index}",
        scope_path=["project", "alpha"],
        source_block_ids=[f"b{index}"],
        token_count=3,
        doc_hash="doc-hash",
        version=2,
        page_numbers=[index],
        numeric_hints=[],
    )


def test_ensure_collection_keeps_matching_collection(manager):
    qdrant, client = manager
    client.collection = SimpleNamespace(
        vectors_config={"dense": SimpleNamespace(size=384)}
    )

    qdrant.ensure_collection(vector_size=384)

    assert client.created == []
    assert client.deleted_collections == []


def test_ensure_collection_recreates_mismatched_collection(manager):
    qdrant, client = manager
    client.collection = SimpleNamespace(
        vectors_config={"dense": SimpleNamespace(size=1024)}
    )

    qdrant.ensure_collection(vector_size=384)

    assert client.deleted_collections == ["test_chunks"]
    created = client.created[0]
    assert created["collection_name"] == "test_chunks"
    assert created["vectors_config"]["dense"].size == 384
    assert "sparse" in created["sparse_vectors_config"]


def test_ensure_collection_creates_when_lookup_fails(manager):
    qdrant, client = manager
    client.collection = RuntimeError("missing")

    qdrant.ensure_collection(vector_size=384)

    assert len(client.created) == 1


def test_upsert_chunks_handles_empty_and_batches_points(manager):
    qdrant, client = manager

    assert qdrant.upsert_chunks([]) == 0
    chunks = [_chunk(i) for i in range(101)]
    assert qdrant.upsert_chunks(chunks) == 101

    assert [len(call["points"]) for call in client.upserted] == [100, 1]
    point = client.upserted[0]["points"][0]
    assert point.payload["doc_hash"] == "doc-hash"
    assert point.payload["scope_path"] == ["project", "alpha"]
    assert point.vector["dense"] == [0.0, 0.0, 0.0]


def test_get_ingestion_status_returns_none_when_document_missing(manager):
    qdrant, client = manager
    client.scrolled = ([], None)

    assert qdrant.get_ingestion_status("missing") is None


def test_get_ingestion_status_returns_count_and_version(manager):
    qdrant, client = manager
    client.scrolled = ([SimpleNamespace(payload={"version": 7})], None)
    client.count_result = SimpleNamespace(count=4)

    status = qdrant.get_ingestion_status("doc-hash")

    assert status.status == "success"
    assert status.chunks_indexed == 4
    assert status.version == 7


def test_get_ingestion_status_converts_client_error_to_failed_status(manager):
    qdrant, client = manager
    client.scrolled = RuntimeError("qdrant unavailable")

    status = qdrant.get_ingestion_status("doc-hash")

    assert status.status == "failed"
    assert status.chunks_indexed == 0
    assert status.error == "qdrant unavailable"


def test_search_returns_payload_score_pairs(manager):
    qdrant, client = manager
    client.search_results = [
        SimpleNamespace(payload={"text": "first"}, score=0.9),
        SimpleNamespace(payload={"text": "second"}, score=0.7),
    ]

    results = qdrant.search([0.1, 0.2, 0.3], top_k=2, score_threshold=0.5)

    assert results == [({"text": "first"}, 0.9), ({"text": "second"}, 0.7)]
    assert client.search_kwargs["query_vector"] == ("dense", [0.1, 0.2, 0.3])
    assert client.search_kwargs["limit"] == 2
    assert client.search_kwargs["score_threshold"] == 0.5


def test_delete_old_versions_issues_filtered_delete(manager):
    qdrant, client = manager

    result = qdrant.delete_old_versions("doc-hash", keep_version=3)

    assert result == 0
    call = client.point_deletes[0]
    assert call["collection_name"] == "test_chunks"
    conditions = call["points_selector"].filter.must
    assert [condition.key for condition in conditions] == ["doc_hash", "version"]
