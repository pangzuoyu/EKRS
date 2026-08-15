"""T1 RED: form_fields / column_headers field defaults on Chunk + Metadata.

Per Phase 12 plan §三.0 (gstack D4): default_factory=list, not Optional[list] = None.
Mirrors Phase 10 T10a-5 chunk_id Optional[str] = None precedent for legacy chunks,
but applies D4 (downstream consumers like retriever._scope_priority and FTS5 string
builder should not need None checks).

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md §三
"""

import pytest

from shared.ekrs_shared.models import Chunk, Metadata


@pytest.mark.unit
def test_chunk_default_factory_yields_empty_list():
    """D4: Chunk() must default form_fields / column_headers to [] not None."""
    c = Chunk(text="hello")
    assert c.form_fields == []
    assert c.column_headers == []
    assert isinstance(c.form_fields, list)
    assert isinstance(c.column_headers, list)


@pytest.mark.unit
def test_metadata_default_factory_yields_empty_list():
    """D4: Metadata() must default form_fields / column_headers to [] not None."""
    m = Metadata()
    assert m.form_fields == []
    assert m.column_headers == []
    assert isinstance(m.form_fields, list)
    assert isinstance(m.column_headers, list)


@pytest.mark.unit
def test_chunk_roundtrip_with_form_fields_column_headers():
    """T1: Chunk must accept and roundtrip form_fields / column_headers."""
    form_fields = [{"key": "SYSTEM NO", "value": "Lot 49"}]
    column_headers = [{"index": 0, "header": "A105"}]
    c = Chunk(
        text="block body",
        form_fields=form_fields,
        column_headers=column_headers,
    )
    assert c.form_fields == form_fields
    assert c.column_headers == column_headers
    # JSON round-trip preserves fields
    j = c.model_dump_json()
    c2 = Chunk.model_validate_json(j)
    assert c2.form_fields == form_fields
    assert c2.column_headers == column_headers


@pytest.mark.unit
def test_metadata_empty_lists_serialize_to_json_array():
    """T1: empty list (default) serializes to JSON [] not null for new fields."""
    m = Metadata()
    j = m.model_dump_json()
    assert '"form_fields":[]' in j
    assert '"column_headers":[]' in j
    # Note: Metadata also has Optional fields (heading_path, bbox) that
    # legitimately serialize to null — those are not under test here.


@pytest.mark.unit
def test_metadata_default_factory_independent_per_instance():
    """D4 guard: default_factory=list must not share list across instances (mutable default trap)."""
    m1 = Metadata()
    m2 = Metadata()
    m1.form_fields.append({"key": "X", "value": "Y"})
    assert m2.form_fields == []  # m2 not affected


@pytest.mark.unit
def test_chunk_default_factory_independent_per_instance():
    """D4 guard: Chunk default_factory=list must not share list across instances."""
    c1 = Chunk(text="a")
    c2 = Chunk(text="b")
    c1.form_fields.append({"key": "X", "value": "Y"})
    assert c2.form_fields == []
