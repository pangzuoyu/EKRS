"""Task C: Chunk model accepts optional doc_type field."""
import pytest

from ekrs_shared.models import Chunk


@pytest.mark.unit
def test_chunk_doc_type_default_is_none():
    """Legacy chunks (pre-Task-C) have doc_type=None — preserves R4
    fallback to scope_path[0] in _scope_priority."""
    c = Chunk(text="hello")
    assert c.doc_type is None


@pytest.mark.unit
def test_chunk_doc_type_round_trip():
    """doc_type serializes + deserializes round-trip via model_dump."""
    c = Chunk(text="hello", doc_type="national_standard")
    assert c.doc_type == "national_standard"
    dumped = c.model_dump()
    assert dumped["doc_type"] == "national_standard"