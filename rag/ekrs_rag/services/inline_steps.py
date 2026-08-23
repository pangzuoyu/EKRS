"""Phase 13a T5 — Inline coarse_gate for notify handler.

The notify route runs cheap admission checks inline (sub-second) and
dispatches the heavy Step 5 work (encode + Qdrant upsert + FTS) to the
pebble subprocess pool (T4). This module is the inline-prep layer.

Design choice (eng-review T5 simplification): only ``coarse_gate`` runs
inline. Parse + chunk + idempotency + ``chunk_gate`` stay in the worker
(T3 step5_worker._step5_async already does parse+chunk+chunk_gate). This
avoids duplicating the JSONL read and the chunker pass.

The trade-off is that coarse_gate only catches the most extreme
"doc bomb" inputs (>1M raw chars). Borderline chunk counts (>3000) are
caught inside the worker after parse+chunk completes. This is acceptable
because:
- coarse_gate eliminates the worst category (megadoc) before any
  pool.submit / subprocess spawn
- chunk_gate still rejects 3001+ chunk docs, just later in the pipeline
- The /ready probe (T1) is independent of pool state, so a doc bomb
  doesn't trigger /ready=503

Returns ``(ok, error_outcome_or_None)``:
- ``(True, None)`` — coarse_gate passed; safe to submit to pool
- ``(False, outcome)`` — coarse_gate rejected; outcome has error_code
"""
from __future__ import annotations

import logging
from typing import Any

from ekrs_shared.models import IngestionNotification

from ..ingestion.outcome import IngestionOutcome
from .admission import coarse_gate

logger = logging.getLogger(__name__)


def run_inline_admission(
    notification: IngestionNotification,
) -> tuple[bool, IngestionOutcome | None]:
    """Run coarse_gate inline (cheap raw-char scan).

    Returns ``(True, None)`` on accept; ``(False, outcome)`` on reject.
    The outcome has ``rag_status="failed"`` and an ``error_code`` from
    coarse_gate (``raw_chars_over_limit`` / ``jsonl_unreadable``).

    Args:
        notification: parser notification (only ``output_path`` is read).
    """
    output_path = notification.output_path
    gate = coarse_gate(output_path)
    if gate["ok"]:
        return (True, None)

    reason = gate.get("reason", "raw_chars_over_limit")
    logger.warning(
        "inline_admission: reject doc=%s v=%d reason=%s",
        notification.doc_hash, notification.version, reason,
    )
    return (
        False,
        IngestionOutcome(
            rag_status="failed",
            error=f"admission coarse_gate: {reason}",
            error_code=reason,
        ),
    )


__all__ = ["run_inline_admission"]
# Suppress unused-import warning for Any.
_ = Any