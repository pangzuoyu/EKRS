"""BM25-only recall@1 measurement for 3 engineering identifiers — Phase 10 T10a-6.

These tests **collect decision data** for T10c (cross-encoder rerank)
evaluation, not assertions on recall thresholds. They write
``[BM25-RECALL-1]`` lines to stdout so a human reviewer can compare
FTS5 quality against vector recall on real corpora.

Identifiers (parent §T10a-6):
- ``A312-TP316`` — ASME standard (English+digits+hyphen)
- ``GB/T 12459`` — Chinese national standard (CJK+Latin+digits+slash)
- ``1.6MPa`` — pressure value (digits+dot+unit)

Each fixture writes 5 chunks (4 with noise text + 1 "identifier-only"
target chunk). The BM25-only search runs ``fts.search(query, limit=10)``
and we check if the target chunk ranks in top-1.

**Soft assertion**: tests pass regardless of recall@1 value; the value
is logged for T10c decision-making. CJK + ``unicode61 remove_diacritics
2`` tokenizer is known to have limitations on CJK-only tokens (parent
§open questions); the data validates whether engineering identifiers
(mostly Latin+digit) survive the tokenizer.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest

from ekrs_rag.retrieval.fts_manager import FTSManager
from ekrs_shared.models import Chunk


def _write_fixture_chunks(
    fts: FTSManager,
    target_doc_hash: str,
    target_text: str,
    noise_chunks: List[Tuple[str, str, str]],  # (text, scope, block_id)
) -> None:
    """Write the target chunk + noise chunks to the FTS index."""
    # Target chunk (the one we want to find)
    target_chunk = Chunk(
        text=target_text,
        scope_path=["第3章"],
        source_block_ids=[f"b_target_{target_doc_hash}"],
        doc_hash=target_doc_hash,
        version=1,
        page_numbers=[1],
        numeric_hints=[],
    )
    target_chunk_id = FTSManager.generate_chunk_id(target_doc_hash, 0)
    fts.upsert(
        target_chunk_id,
        f"b_target_{target_doc_hash}",
        target_chunk,
        {"doc_hash": target_doc_hash},
    )

    # Noise chunks
    for i, (text, scope, block_id) in enumerate(noise_chunks):
        noise_doc_hash = f"noise_{target_doc_hash}_{i}"
        noise_chunk = Chunk(
            text=text,
            scope_path=[scope],
            source_block_ids=[block_id],
            doc_hash=noise_doc_hash,
            version=1,
            page_numbers=[i + 2],
            numeric_hints=[],
        )
        noise_chunk_id = FTSManager.generate_chunk_id(noise_doc_hash, 0)
        fts.upsert(noise_chunk_id, block_id, noise_chunk, {"doc_hash": noise_doc_hash})


def _measure_recall_at_1(
    fts: FTSManager,
    query: str,
    target_chunk_id: str,
    identifier_label: str,
) -> int:
    """Run BM25 search and check if target_chunk_id is rank 1.

    Returns 1 if target ranks first (recall@1), else 0. Logs result.
    """
    hits = fts.search(query, limit=10)
    rank_1_chunk_id = hits[0][0] if hits else None
    recall_1 = 1 if rank_1_chunk_id == target_chunk_id else 0
    print(
        f"\n[BM25-RECALL-1] {identifier_label}: "
        f"target={target_chunk_id}, rank1={rank_1_chunk_id}, recall@1={recall_1}"
    )
    return recall_1


# ===========================================================================
# 1. A312-TP316 — ASME standard
# ===========================================================================


def test_bm25_recall_at_1_for_A312_TP316(tmp_path: Path) -> None:
    """BM25-only recall@1 for ``A312-TP316`` (ASME stainless steel spec)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        _write_fixture_chunks(
            fts,
            target_doc_hash="d_target_asme",
            target_text="A312-TP316",
            noise_chunks=[
                ("管道规格标准", "第1章", "b_n1"),
                ("不锈钢材料要求", "第1章", "b_n2"),
                ("管道规格 A312-TP316 标准", "第2章", "b_n3"),  # has token but not the only one
                ("test data sample", "第5章", "b_n4"),
            ],
        )
        target_id = FTSManager.generate_chunk_id("d_target_asme", 0)
        recall = _measure_recall_at_1(fts, "A312-TP316", target_id, "A312-TP316")

        # Soft assertion — log the result, do not block on recall value
        # (CJK + unicode61 may degrade CJK-heavy queries; Latin+digit
        # identifiers like this one should recall reliably)
        assert isinstance(recall, int)
        assert recall in (0, 1)
    finally:
        fts.close()


# ===========================================================================
# 2. GB/T 12459 — Chinese national standard
# ===========================================================================


def test_bm25_recall_at_1_for_GB_T_12459(tmp_path: Path) -> None:
    """BM25-only recall@1 for ``GB/T 12459`` (Chinese national standard)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        _write_fixture_chunks(
            fts,
            target_doc_hash="d_target_gb",
            target_text="GB/T 12459",
            noise_chunks=[
                ("钢管标准要求", "第1章", "b_n1"),
                ("弯头壁厚", "第1章", "b_n2"),
                ("国标 GB/T 12459 标准", "第2章", "b_n3"),
                ("test data sample", "第5章", "b_n4"),
            ],
        )
        target_id = FTSManager.generate_chunk_id("d_target_gb", 0)
        recall = _measure_recall_at_1(fts, "GB/T 12459", target_id, "GB/T 12459")

        # CJK + slash mixed — unicode61 splits on non-alphanumeric
        assert isinstance(recall, int)
        assert recall in (0, 1)
    finally:
        fts.close()


# ===========================================================================
# 3. 1.6MPa — pressure value
# ===========================================================================


def test_bm25_recall_at_1_for_1_6MPa(tmp_path: Path) -> None:
    """BM25-only recall@1 for ``1.6MPa`` (pressure value)."""
    fts = FTSManager(tmp_path / "fts.db")
    try:
        _write_fixture_chunks(
            fts,
            target_doc_hash="d_target_mpa",
            target_text="1.6MPa",
            noise_chunks=[
                ("压力等级", "第1章", "b_n1"),
                ("管道压力 1.0MPa", "第1章", "b_n2"),
                ("工作压力 2.5MPa", "第2章", "b_n3"),
                ("test data sample", "第5章", "b_n4"),
            ],
        )
        target_id = FTSManager.generate_chunk_id("d_target_mpa", 0)
        recall = _measure_recall_at_1(fts, "1.6MPa", target_id, "1.6MPa")

        # unicode61 may split digits from dot — log result, don't block
        assert isinstance(recall, int)
        assert recall in (0, 1)
    finally:
        fts.close()


# ===========================================================================
# 4. Summary — log aggregate for T10c decision data
# ===========================================================================


def test_bm25_recall_at_1_summary_logged(tmp_path: Path) -> None:
    """Aggregate recall@1 across all 3 identifiers, log summary.

    Phase 10 plan §T10c decision gate: if recall@1 < 1 for ≥2/3
    identifiers on this controlled fixture, cross-encoder rerank
    becomes a higher-priority follow-up.
    """
    fts = FTSManager(tmp_path / "fts.db")
    try:
        identifiers = [
            ("A312-TP316", "d_target_asme", "A312-TP316", [
                ("管道规格标准", "第1章", "b_n1"),
                ("不锈钢材料要求", "第1章", "b_n2"),
                ("test data sample", "第5章", "b_n3"),
            ]),
            ("GB/T 12459", "d_target_gb", "GB/T 12459", [
                ("钢管标准要求", "第1章", "b_n1"),
                ("弯头壁厚", "第1章", "b_n2"),
                ("test data sample", "第5章", "b_n3"),
            ]),
            ("1.6MPa", "d_target_mpa", "1.6MPa", [
                ("压力等级", "第1章", "b_n1"),
                ("管道压力 1.0MPa", "第1章", "b_n2"),
                ("test data sample", "第5章", "b_n3"),
            ]),
        ]

        recall_results = []
        for query, doc_hash, target_text, noise in identifiers:
            _write_fixture_chunks(fts, doc_hash, target_text, noise)
            target_id = FTSManager.generate_chunk_id(doc_hash, 0)
            r = _measure_recall_at_1(fts, query, target_id, query)
            recall_results.append((query, r))

        total = sum(r for _, r in recall_results)
        print(
            f"\n[BM25-RECALL-1-SUMMARY] total_recall={total}/3, "
            f"details={recall_results}"
        )

        # Phase 10 T10c decision data — soft assertion only
        assert total in (0, 1, 2, 3)
    finally:
        fts.close()