"""Phase 13a T2 — admission gates (P0-4 doc-bomb protection).

Two gates, both must reject (return ok=False) or accept (ok=True):

- coarse_gate: read JSONL line-by-line, sum len(content.raw).
  - > ADMISSION_RAW_CHAR_LIMIT → reject "raw_chars_over_limit"
  - missing JSONL or read error → reject "jsonl_unreadable"
  - malformed JSONL line → reject "jsonl_unreadable"
- chunk_gate: after chunking.
  - chunk_count >= 3001 → reject "chunks_over_limit"
  - chunk_count <= 3000 → accept

Used by T5 notify handler (admission before pool.submit) and T3 step5
worker (chunk_gate as defense-in-depth inside the worker).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ekrs_rag.core.config import settings
from ekrs_rag.services.admission import (
    chunk_gate,
    coarse_gate,
)


def _write_jsonl(path: Path, blocks: list[dict]) -> None:
    """Write a list of dicts as JSONL into path/data.jsonl."""
    path.mkdir(parents=True, exist_ok=True)
    with (path / "data.jsonl").open("w", encoding="utf-8") as f:
        for b in blocks:
            f.write(json.dumps(b, ensure_ascii=False))
            f.write("\n")


def _block(raw: str = "hello", block_id: str = "b1") -> dict:
    return {
        "doc_id": "d1",
        "block_id": block_id,
        "type": "text",
        "content": {"raw": raw, "md_preview": raw},
        "metadata": {"page_number": 1, "heading_path": []},
    }


# ---------------------------------------------------------------------------
# coarse_gate — plan T2.1 enumeration + boundary + error paths
# ---------------------------------------------------------------------------


def test_coarse_gate_under_limit_accepts(tmp_path: Path) -> None:
    """Small JSONL (raw well under 1M) → ok=True."""
    _write_jsonl(tmp_path, [_block(raw="small text")])
    result = coarse_gate(str(tmp_path))
    assert result["ok"] is True
    assert result["raw_chars"] == len("small text")


def test_coarse_gate_over_raw_rejects(tmp_path: Path) -> None:
    """Single block raw > 1M → ok=False reason='raw_chars_over_limit'.

    Plan T2.1 verbatim (test_coarse_gate_over_raw).
    """
    big = "x" * (settings.ADMISSION_RAW_CHAR_LIMIT + 1)
    _write_jsonl(tmp_path, [_block(raw=big)])
    result = coarse_gate(str(tmp_path))
    assert result["ok"] is False
    assert result["reason"] == "raw_chars_over_limit"


def test_coarse_gate_just_under_limit_accepts(tmp_path: Path) -> None:
    """Boundary: 1M-1 chars ok; 1M+1 over.

    Settings default = 1_000_000; test boundary is values -1/+1.
    """
    limit = settings.ADMISSION_RAW_CHAR_LIMIT
    _write_jsonl(tmp_path, [_block(raw="a")])
    # 1 char per line × 1 line = 1 (well under)
    assert coarse_gate(str(tmp_path))["ok"] is True

    # Build exactly limit-1 via multi-line accumulation
    blocks = [_block(raw="b" * 1000, block_id=f"b{i}") for i in range(limit // 1000)]
    _write_jsonl(tmp_path, blocks)
    result = coarse_gate(str(tmp_path))
    # Each b is 1000 chars; limit // 1000 = 1000 blocks → 1_000_000 chars;
    # that's the boundary, should be ok (= is not > limit).
    assert result["ok"] is True

    # Now exceed: 1 extra block
    blocks.append(_block(raw="b" * 1001, block_id="extra"))
    _write_jsonl(tmp_path, blocks)
    assert coarse_gate(str(tmp_path))["ok"] is False


def test_missing_jsonl_rejects(tmp_path: Path) -> None:
    """output_path has no data.jsonl → ok=False reason='jsonl_unreadable'.

    Plan T2.1 verbatim (test_missing_jsonl_rejects). Conservative reject —
    any I/O issue trips this reason.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)  # exists but empty
    result = coarse_gate(str(tmp_path))
    assert result["ok"] is False
    assert result["reason"] == "jsonl_unreadable"


def test_invalid_jsonl_line_rejects(tmp_path: Path) -> None:
    """Malformed JSON line → ok=False reason='jsonl_unreadable'."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data.jsonl").write_text("{this is not valid json\n")
    result = coarse_gate(str(tmp_path))
    assert result["ok"] is False
    assert result["reason"] == "jsonl_unreadable"


def test_empty_jsonl_accepts_with_zero_chars(tmp_path: Path) -> None:
    """Empty JSONL (0 lines) → ok=True, raw_chars=0."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "data.jsonl").write_text("")
    result = coarse_gate(str(tmp_path))
    assert result["ok"] is True
    assert result["raw_chars"] == 0


def test_coarse_gate_skips_blank_lines(tmp_path: Path) -> None:
    """Blank lines (whitespace-only) are skipped, not parsed.

    Defensive: parser-side may produce trailing newline; we should
    tolerate it without rejecting.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(_block(raw="hello")) + "\n"
        + "   \n"  # whitespace-only line
        + "\n"
        + json.dumps(_block(raw="world")) + "\n"
    )
    (tmp_path / "data.jsonl").write_text(content)
    result = coarse_gate(str(tmp_path))
    assert result["ok"] is True
    assert result["raw_chars"] == len("hello") + len("world")


# ---------------------------------------------------------------------------
# chunk_gate — plan T2.1 enumeration
# ---------------------------------------------------------------------------


def test_chunk_gate_zero_accepts() -> None:
    """Empty chunks list → ok=True (corner case, not a bomb)."""
    assert chunk_gate(0)["ok"] is True


def test_chunk_gate_under_limit_accepts() -> None:
    """2999 → ok=True."""
    assert chunk_gate(2999)["ok"] is True


def test_chunk_gate_3000_accepts_at_boundary() -> None:
    """3000 = upper bound OK (plan T2.1 verbatim).

    Per user decision 2026-08-24: ≥3001 才拒. So 3000 = OK, 3001 = reject.
    """
    result = chunk_gate(3000)
    assert result["ok"] is True


def test_chunk_gate_3001_rejects_at_boundary() -> None:
    """3001 → ok=False reason='chunks_over_limit' (plan T2.1 verbatim)."""
    result = chunk_gate(3001)
    assert result["ok"] is False
    assert result["reason"] == "chunks_over_limit"
    assert result["chunk_count"] == 3001


def test_chunk_gate_far_over_rejects() -> None:
    """5000 → reject; same reason."""
    result = chunk_gate(5000)
    assert result["ok"] is False
    assert result["reason"] == "chunks_over_limit"