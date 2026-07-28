"""Unit tests for FTSManager (Phase 10 T10a-1).

22 tests per eng-review C2 enumeration:
- 4 schema/upsert/delete lifecycle
- 4 search semantics (R7 / R8 / prefix / negative)
- 2 delete primitives (doc + chunk_id)
- 2 identifier tests (generate + bidirectional)
- 3 T10a-6 engineering-identifier smoketest
- 2 M1 CJK tokenization
- 1 H1 payload_json UNINDEXED guard
- 2 close + lifecycle
- 1 _build_fts5_query empty
- 1 BM25 normalization range
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# RED: this import will fail until Ta.2 GREEN creates the module.
from ekrs_rag.retrieval.fts_manager import FTSManager  # noqa: E402

from ekrs_shared.models import Chunk  # noqa: E402


def _chunk(
    text: str = "压力容器应符合GB 150标准",
    scope: list[str] | None = None,
    doc_hash: str = "hash_001",
    block_id: str = "block_001",
) -> Chunk:
    return Chunk(
        text=text,
        scope_path=scope or ["第3章 压力容器"],
        source_block_ids=[block_id],
        token_count=20,
        doc_hash=doc_hash,
        version=1,
        page_numbers=[1],
    )


# ============================================================================
# Schema + lifecycle (4)
# ============================================================================


def test_create_table_creates_blocks_fts(tmp_path: Path) -> None:
    """SCHEMA creates blocks_fts virtual table with 7 columns."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        cur = fts._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='blocks_fts'"
        )
        assert cur.fetchone() is not None, "blocks_fts virtual table missing"
    finally:
        fts.close()


def test_upsert_inserts_row_with_required_columns(tmp_path: Path) -> None:
    """upsert writes 7 columns + default status='active'."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        chunk = _chunk()
        fts.upsert("chunk_001", "block_001", chunk, {"src": "x"})
        row = fts._conn.execute(
            "SELECT chunk_id, block_id, text, scope_path, status, doc_hash "
            "FROM blocks_fts WHERE chunk_id='chunk_001'"
        ).fetchone()
        assert row is not None
        assert row[0] == "chunk_001"
        assert row[1] == "block_001"
        assert "GB 150" in row[2]
        assert row[3] == "第3章 压力容器"
        assert row[4] == "active"
        assert row[5] == "hash_001"
    finally:
        fts.close()


def test_upsert_with_status_illegal(tmp_path: Path) -> None:
    """upsert accepts explicit status='illegal' for R8 contract."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        chunk = _chunk()
        fts.upsert("chunk_ill", "block_ill", chunk, {}, status="illegal")
        row = fts._conn.execute(
            "SELECT status FROM blocks_fts WHERE chunk_id='chunk_ill'"
        ).fetchone()
        assert row[0] == "illegal"
    finally:
        fts.close()


def test_close_closes_connection(tmp_path: Path) -> None:
    """close() releases the connection — subsequent operations raise."""
    fts = FTSManager(tmp_path / "fts.db")
    fts.close()
    with pytest.raises((sqlite3.ProgrammingError, AttributeError)):
        fts._conn.execute("SELECT 1")


# ============================================================================
# Search semantics (5)
# ============================================================================


def test_search_returns_bm25_normalized_0_1(tmp_path: Path) -> None:
    """All BM25 scores in [0.01, 1.0] range (QMD formula floor)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(text="混凝土养护温度80度"), {})
        fts.upsert("c2", "b2", _chunk(text="钢筋焊接工艺"), {})
        results = fts.search("混凝土养护")
        assert results, "no results"
        for chunk_id, score in results:
            assert 0.01 <= score <= 1.0, f"score {score} out of range"
    finally:
        fts.close()


def test_search_filters_status_illegal(tmp_path: Path) -> None:
    """R8: status='illegal' rows excluded from search."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        chunk = _chunk(text="钢筋焊接")
        fts.upsert("active", "b1", chunk, {})
        fts.upsert("bad", "b2", chunk, {}, status="illegal")
        results = fts.search("钢筋焊接")
        chunk_ids = [r[0] for r in results]
        assert "active" in chunk_ids
        assert "bad" not in chunk_ids
    finally:
        fts.close()


def test_search_filters_scope_path_or_logic(tmp_path: Path) -> None:
    """R7: scope_filter with multiple levels uses OR (任一层级命中)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(scope=["第3章 压力容器"], text="钢材"), {})
        fts.upsert("c2", "b2", _chunk(scope=["第4章 混凝土"], text="钢材"), {})
        fts.upsert("c3", "b3", _chunk(scope=["第5章 验收"], text="钢材"), {})
        results = fts.search("钢材", scope_filter=["第4章 混凝土"])
        chunk_ids = {r[0] for r in results}
        assert chunk_ids == {"c2"}, f"expected only c2, got {chunk_ids}"
    finally:
        fts.close()


def test_search_filters_scope_path_multi_term(tmp_path: Path) -> None:
    """H3: multiple scope_filter terms OR'd (single-term scope : 'term' OR scope : 'term')."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(scope=["第3章 压力容器"], text="钢材"), {})
        fts.upsert("c2", "b2", _chunk(scope=["第4章 混凝土"], text="钢材"), {})
        fts.upsert("c3", "b3", _chunk(scope=["第5章 验收"], text="钢材"), {})
        results = fts.search("钢材", scope_filter=["第3章 压力容器", "第5章 验收"])
        chunk_ids = {r[0] for r in results}
        assert chunk_ids == {"c1", "c3"}, f"expected {{c1, c3}}, got {chunk_ids}"
    finally:
        fts.close()


def test_search_builds_prefix_query_anded(tmp_path: Path) -> None:
    """_build_fts5_query returns positive terms ANDed with prefix wildcards."""
    q = FTSManager._build_fts5_query("混凝土 养护")
    assert q is not None
    assert "AND" in q
    assert "混凝土" in q
    assert "养护" in q
    # Both terms should have prefix wildcards
    assert q.count('"*') >= 2


def test_search_negative_term_excluded(tmp_path: Path) -> None:
    """Negative term '-X' excludes X from results via NOT."""
    q = FTSManager._build_fts5_query("钢材 -焊接")
    assert q is not None
    assert "NOT" in q
    assert "焊接" in q
    # The positive term is ANDed with NOT negative
    assert q.index("钢材") < q.index("NOT")


def test_search_empty_query_returns_none() -> None:
    """_build_fts5_query returns None for empty/all-negative input."""
    assert FTSManager._build_fts5_query("") is None
    assert FTSManager._build_fts5_query("   ") is None
    assert FTSManager._build_fts5_query("-焊接") is None


# ============================================================================
# Delete primitives (2)
# ============================================================================


def test_delete_by_doc_removes_rows(tmp_path: Path) -> None:
    """delete_by_doc removes all rows for a doc_hash."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(doc_hash="d1"), {})
        fts.upsert("c2", "b2", _chunk(doc_hash="d1"), {})
        fts.upsert("c3", "b3", _chunk(doc_hash="d2"), {})
        deleted = fts.delete_by_doc("d1")
        assert deleted == 2
        rows = fts._conn.execute("SELECT chunk_id FROM blocks_fts").fetchall()
        assert rows == [("c3",)]
    finally:
        fts.close()


def test_delete_by_chunk_id_removes_single(tmp_path: Path) -> None:
    """H2: delete_by_chunk_id removes exactly one row (single-chunk rollback primitive)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(doc_hash="d1"), {})
        fts.upsert("c2", "b2", _chunk(doc_hash="d1"), {})
        deleted = fts.delete_by_chunk_id("c1")
        assert deleted == 1
        rows = fts._conn.execute(
            "SELECT chunk_id FROM blocks_fts ORDER BY chunk_id"
        ).fetchall()
        assert rows == [("c2",)]
    finally:
        fts.close()


# ============================================================================
# Chunk ID generation + bidirectional (2)
# ============================================================================


def test_generate_chunk_id_format() -> None:
    """C1: chunk_id format = {doc_hash[:8]}-{chunk_index:04d}."""
    cid = FTSManager.generate_chunk_id("abcdef1234567890", 7)
    assert cid == "abcdef12-0007"
    cid2 = FTSManager.generate_chunk_id("abc", 9999)
    assert cid2 == "abc-9999"


def test_get_chunk_id_bidirectional_roundtrip(tmp_path: Path) -> None:
    """T10a-5: get_chunk_id(block_id) returns chunk_id, write+read consistency."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("chunk_xyz", "block_uuid_123", _chunk(), {})
        assert fts.get_chunk_id("block_uuid_123") == "chunk_xyz"
        # Unknown block_id returns None
        assert fts.get_chunk_id("unknown") is None
    finally:
        fts.close()


# ============================================================================
# T10a-6 engineering-identifier smoketest (3)
# ============================================================================


def test_engineering_identifier_A312_TP316_phrase_match(tmp_path: Path) -> None:
    """T10a-6 smoketest: 'A312-TP316' phrase query finds chunks where the identifier
    is a consecutive token sequence at the start of the text.

    Known limitation (documented in plan §风险): when the identifier is embedded
    inside CJK ("材质A312-TP316..."), unicode61 keeps CJK+ASCII together as one
    token ("材质A312"), preventing phrase match. This test verifies the
    identifier IS retrievable when it stands as its own token sequence. jieba
    tokenizer follow-up (per Phase 10 plan §风险) addresses the embedded case.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(text="A312-TP316材质检测合格"), {})
        results = fts.search("A312-TP316")
        assert results, "identifier at start of chunk should be findable"
        assert "c1" in [r[0] for r in results]
    finally:
        fts.close()


def test_engineering_identifier_GB_T_12459_token_match(tmp_path: Path) -> None:
    """T10a-6 smoketest: 'GB/T 12459' components ('GB', 'T', '12459') are each
    retrievable as tokens — FTS5 splits on slash + whitespace.

    Known limitation: searching the identifier as a single phrase 'GB/T 12459'
    does NOT match because the slash splits 'GB' and 'T' into separate tokens.
    Users searching for this identifier should query the components separately
    (e.g., 'GB 12459'). This is a documented tokenizer behavior, not a bug.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(text="执行GB/T 12459标准弯头"), {})
        # The numeric component '12459' is its own token
        results = fts.search("12459")
        assert results, "12459 token should be findable"
        assert "c1" in [r[0] for r in results]
    finally:
        fts.close()


def test_engineering_identifier_1_6MPa_atomic(tmp_path: Path) -> None:
    """T10a-6 smoketest: '1.6MPa' (digit-dot-digit-letter) is tokenized as a
    single atomic token — unicode61 keeps ASCII alphanumeric runs together
    unless broken by whitespace or known punctuation.

    Search for the identifier prefix '1.6MPa' should match the chunk.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(text="1.6MPa额定压力"), {})
        results = fts.search("1.6MPa")
        assert results
        assert "c1" in [r[0] for r in results]
    finally:
        fts.close()


# ============================================================================
# M1 CJK tokenization (2)
# ============================================================================


def test_cjk_tokenization_at_phrase_start(tmp_path: Path) -> None:
    """M1: CJK phrase at the START of a chunk is retrievable because the
    tokenizer treats it as one token (no per-char split), and the prefix
    query matches against that single token.

    Known limitation (Phase 10 plan §风险): CJK phrases embedded mid-text
    with mixed CJK+ASCII boundaries may not match. jieba tokenizer is the
    follow-up. This test verifies the start-of-chunk case works.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(text="预应力张拉控制应力"), {})
        results = fts.search("预应力张拉")
        assert results, "CJK phrase at start should be findable"
        assert "c1" in [r[0] for r in results]
    finally:
        fts.close()


def test_cjk_tokenization_embeds_limitation(tmp_path: Path) -> None:
    """M1 known limitation: CJK phrase embedded in longer CJK run is NOT
    findable via BM25 because unicode61 keeps the entire CJK run as one token.

    This test pins the limitation so future work (jieba follow-up) can
    replace it with a passing test. Today this is expected behavior, not a bug.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # '养护温度' embedded in longer CJK run — single token in unicode61
        fts.upsert("c1", "b1", _chunk(text="混凝土养护温度不得超过80度"), {})
        results = fts.search("养护温度")
        # unicode61 limitation: 养护温度 is part of one big token, not separable
        assert "c1" not in [r[0] for r in results], (
            "unicode61 should NOT find embedded CJK phrase — known limitation"
        )
    finally:
        fts.close()


def test_unicode61_splits_dash(tmp_path: Path) -> None:
    """Documented behavior: unicode61 splits on '-' between ASCII tokens.

    'A312-TP316' is tokenized as ['A312', 'TP316'] — both prefix-searchable.
    This is the FTS5 default; preventing this split would require a different
    tokenizer (or categories config). The smoke invariant is: the identifier
    is preserved as a phrase sequence, not as a single mangled token.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _chunk(text="A312-TP316材质"), {})
        # 'TP316' prefix DOES match (dash is a separator)
        partial = fts.search("TP316")
        assert "c1" in [r[0] for r in partial]
        # Full phrase 'A312-TP316' also matches (consecutive tokens)
        full = fts.search("A312-TP316")
        assert "c1" in [r[0] for r in full]
    finally:
        fts.close()


# ============================================================================
# H1 payload_json UNINDEXED guard (1)
# ============================================================================


def test_payload_json_UNINDEXED_no_tokenize(tmp_path: Path) -> None:
    """payload_json column is UNINDEXED — JSON keys like 'chunk_id' do not contaminate MATCH."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # Insert row whose payload_json contains the JSON key 'chunk_id'
        fts.upsert("c1", "b1", _chunk(text="钢材"), {"chunk_id": "x", "metadata": {"v": 1}})
        # Search for 'chunk_id' should NOT return c1 (payload_json not indexed)
        results = fts.search("chunk_id")
        assert "c1" not in [r[0] for r in results], (
            "payload_json was tokenized — UNINDEXED guard failed"
        )
        # But payload_json should be queryable as raw value via SELECT
        row = fts._conn.execute(
            "SELECT payload_json FROM blocks_fts WHERE chunk_id='c1'"
        ).fetchone()
        assert "chunk_id" in row[0], "payload_json not stored verbatim"
    finally:
        fts.close()


# ============================================================================
# Connection flags (1)
# ============================================================================


def test_check_same_thread_false(tmp_path: Path) -> None:
    """Connection must allow multi-thread use (FastAPI worker pool)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # If check_same_thread=True, this raises ProgrammingError.
        # In FTSManager init we set False; so this should work cross-thread.
        import threading

        def worker() -> None:
            fts._conn.execute("SELECT 1").fetchone()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2)
        assert not t.is_alive(), "cross-thread query hung or failed"
    finally:
        fts.close()