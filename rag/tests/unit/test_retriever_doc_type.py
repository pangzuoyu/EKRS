"""Task C: retriever _scope_priority reads doc_type first; legacy
chunks (doc_type=None) fall back to scope_path[0] lookup."""
import pytest

from ekrs_rag.retrieval.retriever import EKRSRetriever
from ekrs_shared.models import Chunk


@pytest.mark.unit
def test_scope_priority_uses_doc_type_national_standard():
    """doc_type='national_standard' → priority 1.0 regardless of scope_path."""
    chunk = Chunk(
        text="x", scope_path=[], doc_type="national_standard",
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == 1.0


@pytest.mark.unit
def test_scope_priority_uses_doc_type_lot_checklist():
    """doc_type='lot_checklist' → priority 0.6 (outranks default project=0.4)."""
    chunk = Chunk(
        text="x", scope_path=[], doc_type="lot_checklist",
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == 0.6


@pytest.mark.unit
def test_scope_priority_falls_back_to_scope_path_for_legacy():
    """doc_type=None → reads scope_path[0] (Phase 6B behavior preserved)."""
    chunk = Chunk(
        text="x", scope_path=["national"], doc_type=None,
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == 1.0  # national=100/100=1.0


@pytest.mark.unit
def test_payload_to_chunk_reads_doc_type():
    """_payload_to_chunk extracts doc_type from Qdrant/FTS payload dict."""
    payload = {
        "text": "x", "scope_path": [], "source_block_ids": ["b1"],
        "doc_hash": "abc", "version": 1, "doc_type": "lot_checklist",
    }
    chunk = EKRSRetriever._payload_to_chunk(payload, score=0.5)
    assert chunk.doc_type == "lot_checklist"


@pytest.mark.unit
def test_payload_to_chunk_legacy_no_doc_type_defaults_none():
    """Legacy payload missing doc_type field → Chunk.doc_type=None."""
    payload = {
        "text": "x", "scope_path": ["project"], "source_block_ids": ["b1"],
        "doc_hash": "abc", "version": 1,
    }
    chunk = EKRSRetriever._payload_to_chunk(payload, score=0.5)
    assert chunk.doc_type is None