"""Phase 13c T2 — Prometheus multiproc stale file cleanup.

Each pebble worker subprocess writes to ``$PROMETHEUS_MULTIPROC_DIR`` with
files named ``<pid>_<type>.db``. When the worker dies (graceful, SIGKILL,
or OOM-killed), its files linger. The sidecar exporter then reads
stale values — ``gpu_memory_peak_bytes`` can balloon to whatever the
last write was, masking real GPU activity.

This module:
1. ``is_pid_alive(pid)`` — defensively probes ``os.kill(pid, 0)`` and
   treats ``ProcessLookupError`` / ``PermissionError`` as "not alive".
2. ``cleanup_stale_prometheus_files(dir)`` — walks ``dir``, identifies
   files whose embedded pid is dead AND ``mtime < now - 60s``, calls
   ``prometheus_client.multiprocess.mark_process_dead(pid)`` + unlinks.
3. ``async_cleanup_loop(dir, interval_s=300)`` — async wrapper that
   runs the scan via ``asyncio.to_thread`` (D3 mtime check is heavy
   I/O — must not block the event loop).

D3 rationale for ``mtime < now - 60s``:
Without the mtime guard, an active worker that hasn't flushed its
counters in the last few seconds would be misclassified as "stale"
and its files deleted, causing exporter to report zero metrics for
that pid. 60s gives active workers a wide flush window while still
catching truly dead workers within one cleanup interval.

Verified by ``tests/unit/test_stale_cleanup.py``:
- stale file (dead pid + mtime old) → cleaned + mark_process_dead called
- fresh file (dead pid + mtime new) → preserved (active worker race)
- alive pid file → preserved regardless of mtime
- asyncio.to_thread wrap → event loop stays free during scan
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# Filenames look like ``<pid>_<type>.db``. We anchor on the leading digits
# to avoid mistaking a type name like ``gpu_0.db`` (no pid) for a pid.
_FILENAME_RE = re.compile(r"^(?P<pid>\d+)_(?P<type>.+)\.db$")

# Seconds since last mtime before considering a file "stale enough" to delete.
# Wider than the longest expected worker flush interval so we don't kill
# live workers that are mid-write. Narrower than the cleanup-loop interval
# (300s) so a single cleanup pass catches a worker that's been dead for
# one full interval.
STALE_MTIME_THRESHOLD_S = 60


def is_pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is a running process.

    Defensive against ``ProcessLookupError`` (dead pid) and
    ``PermissionError`` (other user's process — we don't own it).
    PID 0 is the current process group; not a real pid, treat as not-alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Pid exists but we can't signal it (another user's process).
        # Conservative: treat as alive so we don't accidentally unlink
        # another process's counters.
        return True
    except OSError:
        return False


def _parse_pid_from_filename(name: str) -> int | None:
    """Extract pid from ``<pid>_<type>.db`` filename. None if unparseable."""
    m = _FILENAME_RE.match(name)
    if not m:
        return None
    return int(m.group("pid"))


def cleanup_stale_prometheus_files(
    multiproc_dir: Path | str,
    *,
    now: float | None = None,
) -> list[Path]:
    """One-shot scan of ``multiproc_dir`` cleaning dead workers' files.

    Args:
        multiproc_dir: directory containing worker ``.db`` files.
        now: override current time (test hook). Defaults to ``time.time()``.

    Returns:
        List of file paths that were cleaned. Empty list on no-op.
    """
    multiproc_dir = Path(multiproc_dir)
    if not multiproc_dir.is_dir():
        logger.debug("stale_cleanup: dir %s missing; nothing to do", multiproc_dir)
        return []
    if now is None:
        now = time.time()

    cleaned: list[Path] = []
    stale_threshold = now - STALE_MTIME_THRESHOLD_S
    for path in _iter_db_files(multiproc_dir):
        pid = _parse_pid_from_filename(path.name)
        if pid is None:
            # Filename doesn't match pattern; skip (don't touch).
            continue
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            # Race: file deleted between iter and stat. Skip.
            continue
        if mtime > stale_threshold:
            # Recently written — likely an active worker that hasn't
            # flushed yet. Skip (D3 mtime race guard).
            continue
        if is_pid_alive(pid):
            # Pid still alive (just slow). Skip.
            continue
        # Stale: dead pid + mtime old → mark + unlink.
        try:
            if mark_process_dead is not None:
                mark_process_dead(pid)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("stale_cleanup: mark_process_dead(%d) failed: %s", pid, e)
        try:
            path.unlink()
            cleaned.append(path)
            logger.info(
                "stale_cleanup: removed %s (pid=%d, mtime=%.0fs old)",
                path.name, pid, now - mtime,
            )
        except FileNotFoundError:
            # Race: deleted between stat and unlink. Fine.
            pass
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("stale_cleanup: unlink(%s) failed: %s", path, e)
    return cleaned


def _iter_db_files(dirpath: Path) -> Iterable[Path]:
    """Yield *.db files in ``dirpath`` (sorted for deterministic test order)."""
    try:
        yield from sorted(dirpath.glob("*.db"))
    except FileNotFoundError:
        return


async def async_cleanup_loop(
    multiproc_dir: Path | str,
    *,
    interval_s: float = 300.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periodically clean stale Prometheus multiproc files (D3: to_thread).

    Wraps the synchronous ``cleanup_stale_prometheus_files`` in
    ``asyncio.to_thread`` so the I/O-heavy file walk never blocks the
    FastAPI event loop. Default 5-minute interval matches the parent
    plan UQ-4 + D3 decision.

    Args:
        multiproc_dir: directory to scan.
        interval_s: seconds between scans (default 300 = 5 min).
        stop_event: optional asyncio.Event — when set, the loop exits
            cleanly. If None, the loop runs forever (callable is
            cancelled from outside).
    """
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(cleanup_stale_prometheus_files, multiproc_dir)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("stale_cleanup: loop iter failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass  # interval elapsed → next iter


# Imported here for path-style imports (services.stale_cleanup.time).
import time  # noqa: E 02 — late import for testability (tests can patch time.time)


# Lazy import for tests' patch() — pulled in at module level so
# ``patch("ekrs_rag.services.stale_cleanup.mark_process_dead")`` works.
try:
    from prometheus_client.multiprocess import mark_process_dead  # noqa: F 401
except ImportError:  # pragma: no cover - prometheus optional in test envs
    mark_process_dead = None  # type: ignore[assignment]


__all__ = [
    "STALE_MTIME_THRESHOLD_S",
    "async_cleanup_loop",
    "cleanup_stale_prometheus_files",
    "is_pid_alive",
]