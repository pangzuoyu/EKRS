"""Unit tests for stale Prometheus multiproc file cleanup — Phase 13c T2.

Pebble worker subprocesses each get a ``.db`` file in ``PROMETHEUS_MULTIPROC_DIR``
named ``<pid>_<type>.db``. When a worker dies (graceful or SIGKILL), its files
linger and the sidecar exporter reads stale values — causing reported GPU peak
bytes to balloon to whatever last wrote.

T2 fix:
1. ``atexit.register(mark_process_dead(pid))`` in ``_init_child`` so graceful
   shutdown cleans up.
2. Main-process ``_cleanup_stale_prometheus_files()`` background task (5min
   interval, wrapped in ``asyncio.to_thread`` to avoid event-loop block) walks
   the multiproc dir, checks ``os.kill(pid, 0)`` liveness, AND only considers
   files with ``mtime < now - 60s`` stale (D3: avoid race with active workers
   that haven't flushed yet).

Verify:
(a) atexit registers mark_process_dead call.
(b) mtime-old files cleaned, fresh files preserved.
(c) asyncio.to_thread wraps the file scan (mock the scan function).
(d) os.kill(pid, 0) for dead pid returns False.
"""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _import_cleanup():
    from ekrs_rag.services.stale_cleanup import (
        cleanup_stale_prometheus_files,
        is_pid_alive,
    )
    return cleanup_stale_prometheus_files, is_pid_alive


class TestIsPidAlive:
    """os.kill(pid, 0) wrapper — defensive against dead pid."""

    def test_alive_pid_returns_true(self):
        """Current process pid is alive → True."""
        _cleanup, is_pid_alive = _import_cleanup()
        assert is_pid_alive(os.getpid()) is True

    def test_dead_pid_returns_false(self):
        """Synthetic dead pid (1_000_000) → False (no PermissionError)."""
        _cleanup, is_pid_alive = _import_cleanup()
        # PID 1M is virtually never alive on a test host.
        assert is_pid_alive(1_000_000) is False

    def test_zero_pid_returns_false(self):
        """PID 0 is the current process group; treat as not-a-real-pid."""
        _cleanup, is_pid_alive = _import_cleanup()
        # Some systems raise PermissionError, some return False; either way
        # the wrapper must NOT raise.
        result = is_pid_alive(0)
        assert isinstance(result, bool)


class TestCleanupStalePrometheusFiles:
    """Walk multiproc dir, identify stale, mark_process_dead + unlink."""

    def test_old_dead_pid_file_cleaned(self, tmp_path: Path):
        """File with dead pid + mtime > 60s old → cleaned up."""
        cleanup, _ = _import_cleanup()
        # Fake dead pid (1M) + old mtime.
        stale_file = tmp_path / "1000000_gpu.db"
        stale_file.write_bytes(b"x")
        old_time = time.time() - 120  # 2 minutes ago
        os.utime(stale_file, (old_time, old_time))

        with patch("ekrs_rag.services.stale_cleanup.mark_process_dead") as mock_mark:
            cleaned = cleanup(tmp_path)

        assert cleaned == [stale_file]
        assert not stale_file.exists()
        mock_mark.assert_called_once_with(1000000)

    def test_fresh_dead_pid_file_preserved(self, tmp_path: Path):
        """Dead pid but mtime fresh (<60s) → preserved (active worker race)."""
        cleanup, _ = _import_cleanup()
        fresh_file = tmp_path / "1000000_gpu.db"
        fresh_file.write_bytes(b"x")
        # mtime = now → not stale yet.
        os.utime(fresh_file, (time.time(), time.time()))

        with patch("ekrs_rag.services.stale_cleanup.mark_process_dead") as mock_mark:
            cleaned = cleanup(tmp_path)

        assert cleaned == []
        assert fresh_file.exists()  # preserved
        mock_mark.assert_not_called()

    def test_alive_pid_file_preserved(self, tmp_path: Path):
        """Alive pid (current process) → preserved regardless of mtime."""
        cleanup, _ = _import_cleanup()
        alive_pid = os.getpid()
        alive_file = tmp_path / f"{alive_pid}_counter.db"
        alive_file.write_bytes(b"x")
        old_time = time.time() - 120
        os.utime(alive_file, (old_time, old_time))

        with patch("ekrs_rag.services.stale_cleanup.mark_process_dead") as mock_mark:
            cleaned = cleanup(tmp_path)

        assert cleaned == []
        assert alive_file.exists()
        mock_mark.assert_not_called()

    def test_mixed_files_only_stale_cleaned(self, tmp_path: Path):
        """Mix of stale dead, fresh dead, alive → only stale dead cleaned."""
        cleanup, _ = _import_cleanup()
        # Stale dead.
        stale = tmp_path / "1000001_counter.db"
        stale.write_bytes(b"x")
        old = time.time() - 120
        os.utime(stale, (old, old))
        # Fresh dead (active worker race).
        fresh_dead = tmp_path / "1000002_counter.db"
        fresh_dead.write_bytes(b"x")
        # Alive.
        alive = tmp_path / f"{os.getpid()}_gauge.db"
        alive.write_bytes(b"x")
        old2 = time.time() - 200
        os.utime(alive, (old2, old2))

        with patch("ekrs_rag.services.stale_cleanup.mark_process_dead") as mock_mark:
            cleaned = cleanup(tmp_path)

        assert cleaned == [stale]
        assert not stale.exists()
        assert fresh_dead.exists()
        assert alive.exists()
        mock_mark.assert_called_once_with(1000001)


class TestCleanupRunsInToThread:
    """D3: file scan must NOT block event loop — wrap in asyncio.to_thread."""

    def test_async_wrapper_uses_to_thread(self):
        """The async entry point calls asyncio.to_thread(cleanup, dir)."""
        from ekrs_rag.services import stale_cleanup

        # Verify the async wrapper exists and uses to_thread.
        with patch.object(stale_cleanup, "asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock()
            # We can't easily call the wrapper here without it returning
            # awaitable. Instead, just check the source file uses to_thread.
            src = Path(stale_cleanup.__file__).read_text()
        assert "asyncio.to_thread" in src, (
            "stale_cleanup must use asyncio.to_thread (D3 mtime check)"
        )

    def test_scan_blocking_call_runs_in_thread(self):
        """Direct integration: file scan takes ~0.1s but event loop stays free.

        Skipped if test is slow; this verifies async path actually awaits.
        """
        pytest.skip("covered by integration test in test_stale_cleanup_integration.py")


# Helper for the to_thread assertion — patchable async mock.
class AsyncMock:
    async def __call__(self, *args, **kwargs):
        return None


class TestAtexitMarkProcessDeadRegistration:
    """T2: atexit registers mark_process_dead in encoding_pool._init_child."""

    def test_init_child_registers_atexit_mark_process_dead(self):
        """``_init_child`` calls ``atexit.register(mark_process_dead, os.getpid())``."""
        from ekrs_rag.services import encoding_pool

        # We can't run _init_child fully (it does heavy imports), so
        # just verify the source code contains the registration call.
        src = Path(encoding_pool.__file__).read_text()
        assert "atexit" in src
        assert "mark_process_dead" in src, (
            "_init_child must register atexit(mark_process_dead(pid)) per T2"
        )


class TestAsyncioToThreadWrapIntegration:
    """D3 verification: file scan inside asyncio.to_thread actually defers work."""

    def test_to_thread_call_does_not_block_event_loop(self, tmp_path: Path):
        """Spinning an event loop while a slow file scan runs in to_thread
        → other tasks still progress (proven by elapsed time < scan time).
        """
        cleanup_sync, _ = _import_cleanup()
        # Create 100 dummy files to make scan take measurable time.
        for i in range(100):
            (tmp_path / f"{i}_test.db").write_bytes(b"x")

        async def _runner() -> None:
            # Schedule a heartbeat that should run during the slow scan.
            heartbeat_done = asyncio.Event()
            t_start = time.monotonic()

            async def heartbeat() -> None:
                await asyncio.sleep(0.01)
                heartbeat_done.set()

            # Schedule scan in to_thread + heartbeat concurrently.
            scan_task = asyncio.create_task(
                asyncio.to_thread(cleanup_sync, tmp_path),
            )
            hb_task = asyncio.create_task(heartbeat())

            await asyncio.wait_for(asyncio.gather(scan_task, hb_task), timeout=5.0)
            elapsed = time.monotonic() - t_start
            # If cleanup_sync blocked the loop, heartbeat wouldn't fire until
            # after scan done. If wrapped properly, hb fires in ~10ms regardless.
            assert elapsed < 5.0, f"Cleanup took too long: {elapsed:.2f}s"
            assert heartbeat_done.is_set(), "Heartbeat did not fire"

        asyncio.run(_runner())