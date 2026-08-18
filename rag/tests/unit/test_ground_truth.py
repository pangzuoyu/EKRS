"""Unit tests for rag/ekrs_rag/ground_truth.py heuristic extractors.

Phase 12-A follow-up: per-(bundle, anchor) ground-truth labels for the
real-infra round of recall@10 baseline. Three heuristics:

- form_field: chunk whose text contains the LOT value AND form_fields is non-empty
- column_header: chunk whose column_headers list contains the header value AND
  text contains it (substring match — column_headers values are dicts shaped
  ``{index: int, header: str}`` so we check the dict's ``header`` field as a
  string within the text)
- heading: chunk whose heading_path ends with the heading value — DEFERRED
  (heading_path is not propagated to Qdrant payload per the data-quality
  issue documented in [[phase10-t10b2-closed]]; heuristic returns None for
  now with a module-level WARNING).

Tests use plain dicts shaped like Qdrant payload (see Chunk._payload_to_chunk
in retrieval/retriever.py:243).
"""
from __future__ import annotations

from typing import List

import pytest

from ekrs_rag.ground_truth import (
    pick_form_field_chunk,
    pick_column_header_chunk,
    pick_heading_chunk,
    parse_lot_from_filename,
    first_column_header_value,
    extract_anchors_for_bundle,
    build_sidecar,
    anchors_from_sidecar,
    filter_chunks_by_doc_prefix,
)


def _chunk(
    chunk_id: str,
    text: str = "",
    scope_path: List[str] | None = None,
    form_fields: List[dict] | None = None,
    column_headers: List[dict] | None = None,
    source_block_ids: List[str] | None = None,
) -> dict:
    """Build a Qdrant-payload-shaped dict for testing.

    Mirrors Chunk.__init__ defaults from shared/ekrs_shared/models.py:186-216
    plus what the retriever._payload_to_chunk reads (retriever.py:243).
    """
    return {
        "chunk_id": chunk_id,
        "text": text,
        "scope_path": scope_path or [],
        "form_fields": form_fields or [],
        "column_headers": column_headers or [],
        "source_block_ids": source_block_ids or [],
    }


# ---------------------------------------------------------------------------
# form_field heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_form_field_picks_chunk_with_lot_in_text_and_nonempty_form_fields() -> None:
    chunks = [
        _chunk("aaaaaaaa-0001", text="LOT 7 NCR Status Report.doc — see header"),
        _chunk(
            "aaaaaaaa-0002",
            text="Lot 7 NCR Status Report.doc with form fields populated",
            form_fields=[{"name": "LOT", "value": "Lot 7"}],
        ),
    ]
    assert pick_form_field_chunk(chunks, "Lot 7") == "aaaaaaaa-0002"


@pytest.mark.unit
def test_form_field_returns_none_when_form_fields_empty() -> None:
    """LOT in text but no form_fields → heuristic must not pick it."""
    chunks = [
        _chunk("aaaaaaaa-0001", text="Lot 7 mentioned in body"),
    ]
    assert pick_form_field_chunk(chunks, "Lot 7") is None


@pytest.mark.unit
def test_form_field_tie_break_prefers_higher_scope_priority() -> None:
    """Two matches: scope_path[0] national > industry → national wins."""
    chunks = [
        _chunk(
            "aaaaaaaa-0001",
            text="Lot 7 reference",
            scope_path=["industry", "nuclear"],
            form_fields=[{"name": "LOT"}],
        ),
        _chunk(
            "aaaaaaaa-0002",
            text="Lot 7 reference",
            scope_path=["national", "nuclear"],
            form_fields=[{"name": "LOT"}],
        ),
    ]
    assert pick_form_field_chunk(chunks, "Lot 7") == "aaaaaaaa-0002"


@pytest.mark.unit
def test_form_field_returns_none_for_empty_chunks() -> None:
    assert pick_form_field_chunk([], "Lot 7") is None


# ---------------------------------------------------------------------------
# column_header heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_column_header_picks_chunk_with_header_in_list_and_text() -> None:
    chunks = [
        _chunk("aaaaaaaa-0001", text="Unrelated table"),
        _chunk(
            "aaaaaaaa-0002",
            text="Material Spec table with A105 column header",
            column_headers=[{"index": 0, "header": "A105"}],
        ),
    ]
    assert pick_column_header_chunk(chunks, "A105") == "aaaaaaaa-0002"


@pytest.mark.unit
def test_column_header_returns_none_when_header_not_in_text() -> None:
    """Header in column_headers list but text doesn't contain it → skip."""
    chunks = [
        _chunk(
            "aaaaaaaa-0001",
            text="Different content entirely",
            column_headers=[{"index": 0, "header": "A105"}],
        ),
    ]
    assert pick_column_header_chunk(chunks, "A105") is None


@pytest.mark.unit
def test_column_header_returns_none_for_empty_chunks() -> None:
    assert pick_column_header_chunk([], "A105") is None


@pytest.mark.unit
def test_column_header_picks_chunk_with_real_schema_index_and_header() -> None:
    """Regression test: column_header heuristic must use the REAL schema
    ``{index: int, header: str}`` (NOT ``{name: str}``).

    Real Qdrant bundles carry:
        ``column_headers: [{"index": 0, "header": "Item"}, ...]``

    Before the fix, the heuristic read ``h.get("name")`` and never matched
    real data — column_header recall@10 was 0/8 in the Phase 12 baseline.
    """
    chunks = [
        _chunk(
            "aaaaaaaa-0002",
            text="Material Spec table with A105 column header",
            column_headers=[{"index": 0, "header": "A105"}],
        ),
    ]
    assert pick_column_header_chunk(chunks, "A105") == "aaaaaaaa-0002"


@pytest.mark.unit
def test_first_column_header_value_uses_real_schema_header_key() -> None:
    """Regression test: ``first_column_header_value`` must read ``header``
    key from the REAL schema ``{index, header}`` (NOT ``name``).

    Before the fix, this helper returned ``None`` for every real bundle
    because it read ``h.get("name")`` which doesn't exist in the real schema.
    """
    chunks = [
        _chunk(
            "aaaaaaaa-0002",
            text="Has A105 column header",
            column_headers=[{"index": 0, "header": "A105"}],
        ),
    ]
    assert first_column_header_value(chunks) == "A105"


# ---------------------------------------------------------------------------
# heading heuristic (DEFERRED — heading_path not in payload)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_heading_returns_none_because_heading_path_not_in_payload() -> None:
    """Known data-quality gap: heading_path not propagated to Qdrant payload.

    See [[phase10-t10b2-closed]]: doc-to-md doesn't propagate outline.json
    heading_path into data.jsonl. Until that fix lands, the heading
    heuristic cannot work — it must return None and the baseline script
    must SKIP the heading anchor (per the lock-in ruling 2026-08-18).
    """
    chunks = [
        _chunk("aaaaaaaa-0001", text="Material Specification\n\nSome body"),
    ]
    assert pick_heading_chunk(chunks, "Material Specification") is None


# ---------------------------------------------------------------------------
# Orchestrator helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_lot_from_filename_extracts_number() -> None:
    assert parse_lot_from_filename("7-Lot00 NCR Status Report.doc") == "Lot 0"
    assert parse_lot_from_filename("6-Lot11 DCN Status Report.doc") == "Lot 11"
    assert parse_lot_from_filename("LOT 42 something") == "Lot 42"


@pytest.mark.unit
def test_parse_lot_from_filename_returns_none_when_no_lot() -> None:
    assert parse_lot_from_filename("Material Spec.pdf") is None
    assert parse_lot_from_filename("") is None


@pytest.mark.unit
def test_first_column_header_value_picks_first_name() -> None:
    chunks = [
        _chunk("aaaaaaaa-0001", text="Unrelated"),
        _chunk(
            "aaaaaaaa-0002",
            text="Has A105 column header",
            column_headers=[{"index": 0, "header": "A105"}],
        ),
    ]
    assert first_column_header_value(chunks) == "A105"


@pytest.mark.unit
def test_first_column_header_value_returns_none_when_empty() -> None:
    chunks = [
        _chunk("aaaaaaaa-0001", text="No columns here"),
    ]
    assert first_column_header_value(chunks) is None


@pytest.mark.unit
def test_extract_anchors_for_bundle_emits_form_field_and_column_header() -> None:
    """Happy path: form_field + column_header both match; heading skipped."""
    bundle = {"bundle_id": "cccccccc", "filename": "7-Lot00 NCR Status Report.doc"}
    chunks = [
        _chunk("aaaaaaaa-0001", text="Unrelated block"),
        _chunk(
            "aaaaaaaa-0002",
            text="Lot 0 material spec with A105 column",
            form_fields=[{"name": "LOT", "value": "Lot 0"}],
            column_headers=[{"index": 0, "header": "A105"}],
        ),
    ]
    anchors = extract_anchors_for_bundle(bundle, chunks)
    types = [a["anchor_type"] for a in anchors]
    assert "form_field" in types
    assert "column_header" in types
    assert "heading" not in types  # always skipped in this version
    assert len(anchors) == 2


@pytest.mark.unit
def test_extract_anchors_for_bundle_skips_individually() -> None:
    """When one anchor has no candidate, only the other is emitted."""
    bundle = {"bundle_id": "dddddddd", "filename": "7-Lot00 NCR Status Report.doc"}
    chunks = [
        # Only form_field fits; column_headers is empty so column_header skipped
        _chunk(
            "aaaaaaaa-0001",
            text="Lot 0 spec",
            form_fields=[{"name": "LOT"}],
        ),
    ]
    anchors = extract_anchors_for_bundle(bundle, chunks)
    assert len(anchors) == 1
    assert anchors[0]["anchor_type"] == "form_field"


@pytest.mark.unit
def test_build_sidecar_merges_ground_truth_per_bundle() -> None:
    bundles = [
        {"bundle_id": "aaa", "filename": "x.doc", "n_blocks": 5},
        {"bundle_id": "bbb", "filename": "y.doc", "n_blocks": 3},
    ]
    gt = {
        "aaa": [{"anchor_type": "form_field", "anchor_value": "Lot 1",
                 "expected_chunk_id": "aaaaaaaa-0001"}],
        "bbb": [],
    }
    sidecar = build_sidecar(bundles, gt)
    assert sidecar["recommended_first"][0]["ground_truth"] == gt["aaa"]
    assert sidecar["recommended_first"][1]["ground_truth"] == []
    assert "summary" in sidecar
    assert "full_list" not in sidecar  # we only label recommended_first


# ---------------------------------------------------------------------------
# anchors_from_sidecar — feeder into recall@10 baseline script
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_anchors_from_sidecar_emits_one_anchor_per_ground_truth_entry() -> None:
    """Two bundles × two ground_truth entries each → 4 anchors emitted."""
    bundles = [
        {"bundle_id": "aaa", "filename": "x.doc"},
        {"bundle_id": "bbb", "filename": "y.doc"},
    ]
    sidecar = {
        "recommended_first": [
            {"bundle_id": "aaa", "ground_truth": [
                {"anchor_type": "form_field", "anchor_value": "Lot 1",
                 "expected_chunk_id": "aaaaaaaa-0001"},
                {"anchor_type": "column_header", "anchor_value": "A105",
                 "expected_chunk_id": "aaaaaaaa-0002"},
            ]},
            {"bundle_id": "bbb", "ground_truth": [
                {"anchor_type": "form_field", "anchor_value": "Lot 2",
                 "expected_chunk_id": "bbbbbbbb-0001"},
                {"anchor_type": "column_header", "anchor_value": "A105",
                 "expected_chunk_id": "bbbbbbbb-0002"},
            ]},
        ],
    }
    anchors = anchors_from_sidecar(bundles, sidecar)
    assert len(anchors) == 4
    assert anchors[0]["bundle_id"] == "aaa"
    assert anchors[0]["anchor_type"] == "form_field"
    assert anchors[0]["anchor_value"] == "Lot 1"
    assert anchors[0]["expected_chunk_id"] == "aaaaaaaa-0001"
    assert anchors[2]["bundle_id"] == "bbb"
    assert anchors[2]["expected_chunk_id"] == "bbbbbbbb-0001"


@pytest.mark.unit
def test_anchors_from_sidecar_skips_bundle_with_empty_ground_truth() -> None:
    """Bundle in sidecar with empty ground_truth list → SKIP+WARNING, no anchors."""
    bundles = [
        {"bundle_id": "aaa", "filename": "x.doc"},
        {"bundle_id": "bbb", "filename": "y.doc"},
    ]
    sidecar = {
        "recommended_first": [
            {"bundle_id": "aaa", "ground_truth": [
                {"anchor_type": "form_field", "anchor_value": "Lot 1",
                 "expected_chunk_id": "aaaaaaaa-0001"},
            ]},
            {"bundle_id": "bbb", "ground_truth": []},
        ],
    }
    anchors = anchors_from_sidecar(bundles, sidecar)
    assert len(anchors) == 1
    assert anchors[0]["bundle_id"] == "aaa"


@pytest.mark.unit
def test_anchors_from_sidecar_skips_bundle_not_in_sidecar() -> None:
    """Bundle in input but not in sidecar's recommended_first → SKIP+WARNING."""
    bundles = [
        {"bundle_id": "aaa", "filename": "x.doc"},
        {"bundle_id": "missing", "filename": "z.doc"},
    ]
    sidecar = {
        "recommended_first": [
            {"bundle_id": "aaa", "ground_truth": [
                {"anchor_type": "form_field", "anchor_value": "Lot 1",
                 "expected_chunk_id": "aaaaaaaa-0001"},
            ]},
        ],
    }
    anchors = anchors_from_sidecar(bundles, sidecar)
    assert len(anchors) == 1
    assert all(a["bundle_id"] == "aaa" for a in anchors)


@pytest.mark.unit
def test_anchors_from_sidecar_returns_empty_for_all_empty_ground_truth() -> None:
    """All bundles empty → returns empty list, no anchors emitted."""
    bundles = [
        {"bundle_id": "aaa", "filename": "x.doc"},
        {"bundle_id": "bbb", "filename": "y.doc"},
    ]
    sidecar = {
        "recommended_first": [
            {"bundle_id": "aaa", "ground_truth": []},
            {"bundle_id": "bbb", "ground_truth": []},
        ],
    }
    anchors = anchors_from_sidecar(bundles, sidecar)
    assert anchors == []


@pytest.mark.unit
def test_anchors_from_sidecar_tolerates_empty_sidecar() -> None:
    """Empty sidecar (no recommended_first) → all bundles skipped."""
    bundles = [
        {"bundle_id": "aaa", "filename": "x.doc"},
    ]
    assert anchors_from_sidecar(bundles, {}) == []
    assert anchors_from_sidecar(bundles, {"recommended_first": []}) == []


# ---------------------------------------------------------------------------
# filter_chunks_by_doc_prefix — used by extractor when bundle_id is a
# prefix of the actual Qdrant doc_hash (parser adds ``_r<timestamp>``
# suffix during ingestion-notify). Pure helper, unit-tested.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filter_chunks_by_doc_prefix_matches_exact() -> None:
    """Exact match (no suffix) is included."""
    chunks = [
        {"doc_hash": "abc123", "text": "x"},
        {"doc_hash": "xyz789", "text": "y"},
    ]
    assert filter_chunks_by_doc_prefix(chunks, "abc123") == chunks[:1]


@pytest.mark.unit
def test_filter_chunks_by_doc_prefix_matches_with_suffix() -> None:
    """Prefix matches even when Qdrant has ``_r<timestamp>`` suffix."""
    chunks = [
        {"doc_hash": "abc123_r20260728T045717Z", "text": "x"},
        {"doc_hash": "xyz789_r20260728T045717Z", "text": "y"},
    ]
    out = filter_chunks_by_doc_prefix(chunks, "abc123")
    assert len(out) == 1
    assert out[0]["doc_hash"] == "abc123_r20260728T045717Z"


@pytest.mark.unit
def test_filter_chunks_by_doc_prefix_returns_empty_for_no_match() -> None:
    chunks = [
        {"doc_hash": "xyz789", "text": "y"},
        {"doc_hash": "qqq000", "text": "z"},
    ]
    assert filter_chunks_by_doc_prefix(chunks, "abc123") == []


@pytest.mark.unit
def test_filter_chunks_by_doc_prefix_skips_missing_doc_hash() -> None:
    """Chunks without doc_hash field are filtered out (no key error)."""
    chunks = [
        {"text": "no hash field"},
        {"doc_hash": "abc123", "text": "x"},
    ]
    out = filter_chunks_by_doc_prefix(chunks, "abc123")
    assert len(out) == 1
    assert out[0]["doc_hash"] == "abc123"


@pytest.mark.unit
def test_filter_chunks_by_doc_prefix_returns_empty_for_empty_chunks() -> None:
    assert filter_chunks_by_doc_prefix([], "abc123") == []


@pytest.mark.unit
def test_filter_chunks_by_doc_prefix_empty_prefix_matches_all() -> None:
    """Empty prefix is a degenerate case — matches everything (str.startswith)."""
    chunks = [
        {"doc_hash": "abc", "text": "x"},
        {"doc_hash": "xyz", "text": "y"},
    ]
    # str.startswith("") returns True for all strings — this is the
    # Python default. Documented behavior: caller must pass non-empty
    # prefix; the extractor guarantees this.
    out = filter_chunks_by_doc_prefix(chunks, "")
    assert len(out) == 2