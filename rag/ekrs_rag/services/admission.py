"""Phase 13a T2 — admission gates (P0-4 doc-bomb protection).

Two hard gates run before any encoding work to prevent a single
malformed/oversized bundle from saturating the pebble worker pool
(T4) for hours:

1. ``coarse_gate(output_path)`` — read JSONL line-by-line, sum
   ``len(content.raw)`` across all blocks. > ``ADMISSION_RAW_CHAR_LIMIT``
   (default 1M, configurable via Settings) → reject
   ``raw_chars_over_limit``. Any I/O / JSON parse issue → reject
   ``jsonl_unreadable`` (conservative; pipeline idempotency handles later
   retries — better safe than wedged).

2. ``chunk_gate(chunk_count)`` — after chunking. ``chunk_count >= 3001``
   → reject ``chunks_over_limit``. Per plan T2.1 user decision: 3000 =
   upper bound OK, 3001 = first reject.

Conservative semantics are deliberate: we prefer a false positive (reject
a borderline doc) over letting one bad bundle wedge the worker pool.

Both gates return ``dict[str, Any]`` for forward compatibility (extra
fields like ``raw_chars`` / ``chunk_count`` don't break callers).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..core.config import settings

logger = logging.getLogger(__name__)


def coarse_gate(output_path: str) -> dict[str, Any]:
    """Read JSONL line-by-line, sum ``len(content.raw)``.

    Returns ``{"ok": True, "raw_chars": N}`` on accept,
    ``{"ok": False, "reason": "..."}`` on reject.
    """
    jsonl_path = Path(output_path) / "data.jsonl"
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            total = 0
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "admission: malformed line in %s", jsonl_path,
                    )
                    return {"ok": False, "reason": "jsonl_unreadable"}
                raw = data.get("content", {}).get("raw", "")
                total += len(raw) if isinstance(raw, str) else 0
                if total > settings.ADMISSION_RAW_CHAR_LIMIT:
                    return {"ok": False, "reason": "raw_chars_over_limit"}
    except OSError as e:
        logger.warning(
            "admission: open/read failed for %s: %s", jsonl_path, e,
        )
        return {"ok": False, "reason": "jsonl_unreadable"}
    return {"ok": True, "raw_chars": total}


def chunk_gate(chunk_count: int) -> dict[str, Any]:
    """Reject if ``chunk_count > ADMISSION_CHUNK_LIMIT`` (default 3000).

    Per plan T2.1 user decision (2026-08-24): 3000 = upper bound OK,
    3001 = first reject. Strict-greater semantics: ``chunk_count > limit``.
    """
    limit = settings.ADMISSION_CHUNK_LIMIT
    if chunk_count > limit:
        return {
            "ok": False,
            "reason": "chunks_over_limit",
            "chunk_count": chunk_count,
        }
    return {"ok": True}