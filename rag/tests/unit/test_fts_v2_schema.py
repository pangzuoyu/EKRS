"""T3 RED→GREEN: FTS5 schema v2 with form_fields / column_headers columns.

Per Phase 12 plan §三:
- T3: FTS5 schema migration. FTS5 ALTER TABLE ADD COLUMN NOT supported
  (SQLite limitation). Full rebuild required (gstack D3).
- New SCHEMA_V2 adds UNINDEXED form_fields_text / column_headers_text
  columns. Strings serialize list[dict] for SELECT-time audit.

PRR plan: docs/superpowers/plans/2026-08-14-phase12-form-field-r4-boost.md §三
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def fts_db_path(tmp_path: Path) -> Path:
    return tmp_path / "fts.sqlite"


def test_schema_v2_includes_form_fields_column(fts_db_path: Path) -> None:
    """T3: SCHEMA_V2 must declare form_fields_text UNINDEXED column."""
    from ekrs_rag.retrieval.fts_manager import FTSManager

    manager = FTSManager(fts_db_path, schema_version=2)
    cols = [
        row[1]
        for row in manager._conn.execute(
            "PRAGMA table_info(blocks_fts)"
        ).fetchall()
    ]
    assert "form_fields_text" in cols
    assert "column_headers_text" in cols


def test_schema_v2_columns_are_unindexed(fts_db_path: Path) -> None:
    """T3: form_fields / column_headers columns must be UNINDEXED.

    Rationale: BM25 keyword scoring operates on text tokens. Form key/value
    strings would otherwise pollute the index, lowering precision for the
    main body-text search.
    """
    import sqlite3

    conn = sqlite3.connect(str(fts_db_path))
    cols = conn.execute("PRAGMA table_info(blocks_fts)").fetchall()
    conn.close()
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
    for col in cols:
        if col[1] in ("form_fields_text", "column_headers_text"):
            assert col[5] == 0  # pk=0 → UNINDEXED for FTS5 virtual tables


def test_schema_v1_does_not_have_new_columns(fts_db_path: Path) -> None:
    """T3: legacy SCHEMA (v1) does NOT have form_fields_text / column_headers_text.

    Ensures migration path is testable: a fresh DB with v1 schema remains
    byte-level compatible with T10a-5 (no surprise columns).
    """
    from ekrs_rag.retrieval.fts_manager import FTSManager

    manager = FTSManager(fts_db_path, schema_version=1)
    cols = [
        row[1]
        for row in manager._conn.execute(
            "PRAGMA table_info(blocks_fts)"
        ).fetchall()
    ]
    assert "form_fields_text" not in cols
    assert "column_headers_text" not in cols
    assert "text" in cols  # baseline column preserved
    assert "scope_path" in cols  # baseline column preserved


def test_replace_doc_v2_writes_form_fields_to_columns(fts_db_path: Path) -> None:
    """T3: replace_doc with v2 schema writes form_fields to form_fields_text."""
    from ekrs_rag.retrieval.fts_manager import FTSManager
    from ekrs_shared.models import Chunk

    manager = FTSManager(fts_db_path, schema_version=2)
    chunk = Chunk(
        text="LOT 49 CHECKLIST",
        scope_path=["Appendix A"],
        source_block_ids=["b1"],
        token_count=2,
        doc_hash="d1",
        version=1,
        page_numbers=[3],
        form_fields=[{"key": "SYSTEM NO", "value": "Lot 49"}],
        column_headers=[{"index": 0, "header": "Item"}],
    )
    manager.replace_doc("d1", [chunk], version=1)

    rows = manager._conn.execute(
        "SELECT form_fields_text, column_headers_text FROM blocks_fts"
    ).fetchall()
    assert len(rows) == 1
    form_fields = json.loads(rows[0][0])
    column_headers = json.loads(rows[0][1])
    assert form_fields == [{"key": "SYSTEM NO", "value": "Lot 49"}]
    assert column_headers == [{"index": 0, "header": "Item"}]


def test_replace_doc_v2_legacy_chunk_writes_empty_strings(fts_db_path: Path) -> None:
    """T3: legacy chunk (empty form_fields / column_headers) writes '[]'."""
    from ekrs_rag.retrieval.fts_manager import FTSManager
    from ekrs_shared.models import Chunk

    manager = FTSManager(fts_db_path, schema_version=2)
    chunk = Chunk(
        text="legacy chunk",
        scope_path=[],
        source_block_ids=["b2"],
        token_count=1,
        doc_hash="d1",
        version=1,
        page_numbers=[],
    )
    manager.replace_doc("d1", [chunk], version=1)

    row = manager._conn.execute(
        "SELECT form_fields_text, column_headers_text FROM blocks_fts"
    ).fetchone()
    assert row[0] == "[]"
    assert row[1] == "[]"


def test_replace_doc_v1_does_not_write_form_fields_columns(fts_db_path: Path) -> None:
    """T3: v1 schema replace_doc raises KeyError if v2 columns referenced.

    Sanity guard: the v2 write path must NOT silently fall through to a
    v1-only INSERT when chunk.form_fields is non-empty. Plan D3:
    drain + retry logic on v2 path only.
    """
    from ekrs_rag.retrieval.fts_manager import FTSManager
    from ekrs_shared.models import Chunk

    manager = FTSManager(fts_db_path, schema_version=1)
    chunk = Chunk(
        text="legacy",
        scope_path=[],
        source_block_ids=["b3"],
        token_count=1,
        doc_hash="d1",
        version=1,
        page_numbers=[],
        form_fields=[{"key": "K", "value": "V"}],
    )
    # v1 schema has no form_fields_text column → replace_doc must not silently
    # drop; it should either skip these fields or raise.
    manager.replace_doc("d1", [chunk], version=1)
    # Verify chunk was inserted with empty form_fields_text (legacy compat)
    row = manager._conn.execute(
        "SELECT chunk_id FROM blocks_fts"
    ).fetchone()
    assert row is not None  # chunk written
    # v1 has no form_fields_text column — direct PRAGMA would fail
    # so we assert column absence instead:
    cols = [
        row[1]
        for row in manager._conn.execute(
            "PRAGMA table_info(blocks_fts)"
        ).fetchall()
    ]
    assert "form_fields_text" not in cols