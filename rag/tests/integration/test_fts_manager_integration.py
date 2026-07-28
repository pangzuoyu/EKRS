"""Integration tests for FTSManager (Phase 10 T10a-1).

Round-trip tests using real Chunk IR from shared/ekrs_shared/models.
Complement to tests/unit/test_fts_manager.py which uses _chunk() helpers.

8 tests per plan §Ta.4:
- R8 status='illegal' integration
- R7 scope_path with multi-level real paths
- H2 delete_by_chunk_id isolation
- T10a-5 bidirectional roundtrip
- T10a-6 3 engineering identifiers (smoketest set)
- M1 CJK roundtrip (real text fixtures)
- Cross-doc isolation
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_shared.models import Chunk


def _real_chunk(
    text: str,
    *,
    doc_hash: str = "doc_hash_abc",
    scope_path: list[str] | None = None,
    block_ids: list[str] | None = None,
    version: int = 1,
) -> Chunk:
    """Real Chunk IR from shared models. No mocks — exercises serializer contract."""
    return Chunk(
        text=text,
        scope_path=scope_path or [],
        source_block_ids=block_ids or [f"block_{abs(hash(text)) % 10000:04d}"],
        token_count=max(1, len(text) // 4),
        doc_hash=doc_hash,
        version=version,
        page_numbers=[1],
        numeric_hints=[],
        payload_version=1,
    )


# ============================================================================
# 1. Basic round-trip
# ============================================================================


def test_fts_upsert_search_roundtrip(tmp_path: Path) -> None:
    """Real Chunk IR → FTS upsert → search returns the chunk with normalized score.

    Query uses a CJK phrase that the tokenizer keeps as a single token at the
    start of the text (CJK run + ASCII with whitespace = separate tokens).
    Searches for embedded-in-CJK ASCII like "GB" inside a CJK run are NOT
    expected to work — that's the documented plan §风险 limitation.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        chunk = _real_chunk("压力容器应符合GB 150标准，shell设计采用SA-516 Grade 70钢材")
        fts.upsert("c1", chunk.source_block_ids[0], chunk, {"src": "ir_parser"})
        # CJK phrase at start of chunk retrievable as single-token prefix
        results = fts.search("压力容器")
        assert len(results) >= 1
        chunk_id, score = results[0]
        assert chunk_id == "c1"
        assert 0.01 <= score <= 1.0
    finally:
        fts.close()


# ============================================================================
# 2. R8 status filter integration
# ============================================================================


def test_fts_status_illegal_filter(tmp_path: Path) -> None:
    """R8 integration: status='illegal' rows excluded from search."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # 3 active + 2 illegal chunks with same searchable text
        for i in range(3):
            fts.upsert(f"a{i}", f"b_a{i}", _real_chunk("钢筋焊接工艺要求"), {})
        for i in range(2):
            fts.upsert(f"x{i}", f"b_x{i}", _real_chunk("钢筋焊接工艺要求"), {}, status="illegal")
        results = fts.search("钢筋焊接")
        chunk_ids = {r[0] for r in results}
        assert chunk_ids == {"a0", "a1", "a2"}, f"got {chunk_ids}"
    finally:
        fts.close()


# ============================================================================
# 3. R7 multi-level scope_path filter
# ============================================================================


def test_fts_scope_path_filter_real_paths(tmp_path: Path) -> None:
    """R7 with multi-element real paths: '第3章 压力容器' phrase matches c1 only."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _real_chunk("钢材标准", scope_path=["第3章", "压力容器"]), {})
        fts.upsert("c2", "b2", _real_chunk("钢材标准", scope_path=["第4章", "混凝土"]), {})
        fts.upsert("c3", "b3", _real_chunk("钢材标准", scope_path=["第5章", "验收"]), {})
        results = fts.search("钢材", scope_filter=["第3章 压力容器"])
        chunk_ids = {r[0] for r in results}
        assert chunk_ids == {"c1"}
    finally:
        fts.close()


# ============================================================================
# 4. Cross-doc isolation
# ============================================================================


def test_fts_delete_by_doc_isolation(tmp_path: Path) -> None:
    """delete_by_doc removes only target doc_hash; other doc remains intact."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        fts.upsert("c1", "b1", _real_chunk("混凝土养护", doc_hash="d1"), {})
        fts.upsert("c2", "b2", _real_chunk("混凝土养护", doc_hash="d1"), {})
        fts.upsert("c3", "b3", _real_chunk("混凝土养护", doc_hash="d2"), {})
        deleted = fts.delete_by_doc("d1")
        assert deleted == 2
        # d2 still queryable
        results = fts.search("混凝土养护")
        chunk_ids = {r[0] for r in results}
        assert chunk_ids == {"c3"}, f"d2 should remain, got {chunk_ids}"
    finally:
        fts.close()


# ============================================================================
# 5. H2 single-chunk rollback primitive
# ============================================================================


def test_fts_delete_by_chunk_id_only_target(tmp_path: Path) -> None:
    """H2: delete_by_chunk_id removes exactly one row, leaves others."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        for i in range(5):
            fts.upsert(f"c{i}", f"b{i}", _real_chunk(f"钢材标准{i}"), {})
        deleted = fts.delete_by_chunk_id("c2")
        assert deleted == 1
        # Search still finds c0, c1, c3, c4 but not c2
        results = fts.search("钢材")
        chunk_ids = {r[0] for r in results}
        assert "c2" not in chunk_ids
        assert {"c0", "c1", "c3", "c4"}.issubset(chunk_ids)
    finally:
        fts.close()


# ============================================================================
# 6. T10a-5 bidirectional mapping invariant
# ============================================================================


def test_fts_chunk_id_block_id_bidirectional_mapping(tmp_path: Path) -> None:
    """T10a-5 invariant: get_chunk_id(block_id) → original chunk_id, every time."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # Insert with explicit chunk_id generated by generate_chunk_id
        for idx in range(10):
            cid = FTSManager.generate_chunk_id("doc_hash_xyz", idx)
            bid = f"block_uuid_{idx:04d}"
            fts.upsert(cid, bid, _real_chunk(f"内容{idx}", doc_hash="doc_hash_xyz"), {})
        # All 10 mappings retrievable
        for idx in range(10):
            expected_cid = FTSManager.generate_chunk_id("doc_hash_xyz", idx)
            expected_bid = f"block_uuid_{idx:04d}"
            assert fts.get_chunk_id(expected_bid) == expected_cid
        # Unknown block_id returns None (not empty string)
        assert fts.get_chunk_id("never_existed") is None
    finally:
        fts.close()


# ============================================================================
# 7. T10a-6 engineering-identifier smoketest set (all 3 at once)
# ============================================================================


def test_fts_three_engineering_identifiers(tmp_path: Path) -> None:
    """T10a-6 smoketest: A312-TP316 / GB-T 12459 / 1.6MPa all retrievable.

    Text fixtures chosen so each identifier starts the chunk (token sequence
    at beginning of text). Per unicode61 tokenizer behavior, this is the
    reliable match condition. Embedded-in-CJK case is documented limitation.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # Each identifier at start of chunk — token sequence match guaranteed
        fts.upsert("c_a", "b_a", _real_chunk("A312-TP316材质检测合格"), {})
        fts.upsert("c_g", "b_g", _real_chunk("GB-T 12459标准规定"), {})
        fts.upsert("c_p", "b_p", _real_chunk("1.6MPa额定工作压力"), {})
        # Distractors
        fts.upsert("c_x", "b_x", _real_chunk("普通钢筋混凝土施工"), {})

        # Each identifier retrievable
        assert "c_a" in [r[0] for r in fts.search("A312-TP316")]
        # 'GB-T 12459' — slash replaced with dash for tokenizer stability
        results = fts.search("12459")
        assert "c_g" in [r[0] for r in results]
        assert "c_p" in [r[0] for r in fts.search("1.6MPa")]
        # Distractor not in any identifier query
        assert "c_x" not in [r[0] for r in fts.search("A312-TP316")]
    finally:
        fts.close()


# ============================================================================
# 8. M1 CJK roundtrip on real corpus text
# ============================================================================


def test_fts_chinese_cjk_roundtrip(tmp_path: Path) -> None:
    """M1 roundtrip: CJK-heavy text from live_stress_60 corpus shape.

    Phrase at start-of-chunk retrievable. Embedded phrase is the documented
    limitation (jieba follow-up).
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        # Real-feel CJK fixture text (mirrors scripts/live_stress_60.py style)
        fts.upsert(
            "c1",
            "b1",
            _real_chunk("压力容器shell设计采用SA-516 Grade 70钢材"),
            {},
        )
        fts.upsert(
            "c2",
            "b2",
            _real_chunk("混凝土养护温度不得超过80度，养护时间不少于7天"),
            {},
        )
        fts.upsert(
            "c3",
            "b3",
            _real_chunk("预应力张拉控制应力fptk=1860MPa"),
            {},
        )

        # c1: 压力容器 at start — retrievable as prefix
        results = fts.search("压力容器")
        assert "c1" in [r[0] for r in results]
        # c2: 混凝土养护 at start — retrievable
        results = fts.search("混凝土养护")
        assert "c2" in [r[0] for r in results]
        # c3: 预应力张拉 at start — retrievable
        results = fts.search("预应力张拉")
        assert "c3" in [r[0] for r in results]
    finally:
        fts.close()