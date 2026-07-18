"""In-memory trace_id → file_offset index over audit.log.

Built once at startup (linear scan over audit.log). Replay seeks O(1) via
dict lookup, then reads lines from offset. New writes are kept in sync via
AuditWriter.write() which calls idx.append() when an index is attached
(see attach_index / get_index in audit.py).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("ekrs.observability.audit_index")

# Events that Query Replay cares about (A2 decision)
REPLAY_EVENTS = frozenset({"constraint_solve_started", "constraint_solved"})


@dataclass
class AuditLine:
    event: str
    trace_id: str
    offset: int
    raw: dict


class AuditIndex:
    """trace_id -> list[AuditLine] (ordered by offset)."""

    def __init__(self, audit_log_path: str):
        self._path = Path(audit_log_path)
        # trace_id -> list of (event, offset)
        self._index: dict[str, list[tuple[str, int]]] = {}
        self._load_seconds: float = 0.0

    @property
    def size(self) -> int:
        return len(self._index)

    @property
    def load_seconds(self) -> float:
        return self._load_seconds

    def build(self) -> None:
        """Scan audit.log once, populate index."""
        self._scan_and_populate()

    def rebuild(self) -> int:
        """Re-scan audit.log from scratch. Admin endpoint entrypoint.

        Returns the number of unique trace_ids indexed after the rebuild.
        Use after audit.log rotation or after a manual truncation.
        """
        self._scan_and_populate()
        return self.size

    def _scan_and_populate(self) -> None:
        import time
        start = time.monotonic()
        self._index.clear()

        if not self._path.exists():
            self._load_seconds = time.monotonic() - start
            return

        with open(self._path, "r", encoding="utf-8") as f:
            offset = 0
            for line in f:
                line_len = len(line)
                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    logger.warning(
                        "skipping corrupted audit line at offset %d", offset
                    )
                    offset += line_len
                    continue
                event = entry.get("event")
                trace_id = entry.get("trace_id")
                if event in REPLAY_EVENTS and trace_id:
                    self._index.setdefault(trace_id, []).append((event, offset))
                offset += line_len

        self._load_seconds = time.monotonic() - start
        logger.info(
            "audit index built: %d unique trace_ids in %.2fs",
            len(self._index), self._load_seconds,
        )

    def append(self, event: str, trace_id: str, offset: int) -> None:
        """Register a new line written at runtime (no re-scan)."""
        if event not in REPLAY_EVENTS:
            return
        self._index.setdefault(trace_id, []).append((event, offset))

    def seek(self, trace_id: str) -> list[AuditLine] | None:
        """Return all indexed audit lines for trace_id, or None."""
        entries = self._index.get(trace_id)
        if not entries:
            return None

        # Read each line from disk at known offset
        result = []
        with open(self._path, "r", encoding="utf-8") as f:
            for event, offset in entries:
                f.seek(offset)
                line = f.readline()
                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    logger.warning("seek: corrupted line at offset %d", offset)
                    continue
                result.append(AuditLine(
                    event=entry["event"],
                    trace_id=trace_id,
                    offset=offset,
                    raw=entry,
                ))
        return result
