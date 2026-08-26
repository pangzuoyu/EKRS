"""Phase 13c T1 — cross-process AuditWriter bridge.

Pebble worker subprocesses cannot share Python state with the main
process. They need a way to emit audit events (``channel_switched``,
``worker_uncaught``, etc.) that ultimately land in the same
``audit.log`` file the main process writes to.

This module provides ``AuditEventBridge``:

    main process:
        bridge = AuditEventBridge(writer=_audit_writer, maxsize=10000)
        bridge.start()
        # ... lifespan ends:
        bridge.stop(timeout_s=5.0)

    worker process (cold-start in ``_init_child``):
        bridge = AuditEventBridge.from_addr(os.environ["EKRS_AUDIT_QUEUE_ADDR"])
        bridge.put("channel_switched", from_channel="cpu", to_channel="gpu", ...)

The bridge serializes events into a ``multiprocessing.Manager().Queue()``
that both processes share via proxy reference. A consumer thread on the
main process drains the queue and forwards to ``writer.write(event_name,
**kwargs)``.

Layered fault tolerance (D2):
- Manager startup failure → lifespan raises (fail-loud, application must
  not start without an audit pipeline).
- ``bridge.put()`` queue.Full / serialization error → debug log + drop
  counter, NEVER raises (runtime silent drop — encoding hot path must
  not be blocked by audit backpressure).
- Consumer thread writer raises → exception isolated, consumer keeps
  running so subsequent events still drain.

References:
- parent plan: docs/superpowers/plans/2026-08-26-phase13c-prod-readiness.md T1
- parent §204: audit must never crash the caller
"""
from __future__ import annotations

import logging
import multiprocessing
import multiprocessing.managers
import os
import pickle
import queue as _queue
import threading
import time
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


# Environment variable name through which child processes recover the
# shared Queue proxy address.
_QUEUE_ADDR_ENV = "EKRS_AUDIT_QUEUE_ADDR"


class _WriterLike(Protocol):
    """Minimal contract we need from an AuditWriter.

    Real writer is ``ekrs_rag.observability.audit.AuditWriter`` but we
    keep this Protocol so tests can pass a MagicMock without pulling in
    the heavy module.
    """

    def write(self, event_name: str, **kwargs: Any) -> None: ...


class AuditEventBridge:
    """Cross-process AuditWriter bridge (multiprocessing.Queue + drain thread).

    Args:
        writer: main-process writer the drain thread forwards events to.
            Optional — workers pass ``None`` and call ``put()`` only.
        queue: existing ``multiprocessing.Queue`` proxy shared with the
            main process. Pass either ``queue`` OR let the bridge create
            one with ``maxsize``.
        maxsize: queue capacity when bridge creates its own queue.
            Defaults to 10000 (UQ-1 decision).
    """

    def __init__(
        self,
        *,
        writer: _WriterLike | None = None,
        queue: Any | None = None,
        maxsize: int = 10000,
    ) -> None:
        self._writer = writer
        self._owns_queue = queue is None
        if queue is None:
            made = self._make_queue(maxsize)
            # _make_queue returns (queue, manager) so callers can keep the
            # manager alive (preventing the queue proxy from dying).
            if isinstance(made, tuple):
                self._queue, self._manager = made
            else:
                self._queue = made
                self._manager = None
        else:
            self._queue = queue
            self._manager = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Drop counter — incremented on queue.Full / serialization failure.
        # Exposed via ``dropped_count`` for /metrics endpoint consumption.
        self._dropped_count = 0
        self._drop_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the consumer drain thread.

        Idempotent — calling start() twice is a no-op (the second call
        just returns; the existing thread continues).
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._drain_loop,
            name="ekrs_audit_bridge_drain",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "audit_bridge: drain thread started (writer=%s)",
            self._writer is not None,
        )

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """Stop consumer + join thread within ``timeout_s`` grace.

        UQ-3 decision: 5s grace is the operational sweet spot — long
        enough to drain a bursty backlog, short enough that lifespan
        shutdown doesn't stall.

        Drain strategy: signal stop, then drain remaining queue items
        inline (cheaper than waiting for the consumer's 0.5s get() timeout
        to elapse — meaningful when 1000s of items are queued at shutdown).
        The consumer thread exits when both stop_event is set AND queue is
        empty, so it picks up any last items the inline drain misses.
        """
        self._stop_event.set()
        # Inline drain — pull any items the queue has so the consumer
        # doesn't have to wait for its 0.5s get() timeout to observe the
        # stop_event. Critical for fast graceful shutdown.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                payload = self._queue.get_nowait()
            except (_queue.Empty, EOFError, OSError):
                break
            try:
                if self._writer is not None:
                    self._writer.write(payload["event"], **payload["kwargs"])
            except Exception as e:  # parent §204
                logger.debug(
                    "audit_bridge.stop inline drain: writer.write(%s) failed: %s",
                    payload.get("event"), e,
                )
        if self._thread is not None:
            self._thread.join(timeout=1.0)  # short — most work already done
            if self._thread.is_alive():
                logger.warning(
                    "audit_bridge: drain thread did not exit within 1s grace",
                )
            self._thread = None
        if self._owns_queue and self._queue is not None:
            # Manager().Queue() has no close() — Manager handles lifecycle.
            pass

    # ------------------------------------------------------------------
    # Producer API (used by both main + worker)
    # ------------------------------------------------------------------

    def put(self, event_name: str, **kwargs: Any) -> None:
        """Enqueue an audit event for drain on the main process.

        Never raises (D2 silent-drop policy). On ``queue.Full`` or
        serialization error, increments ``dropped_count`` and logs at
        DEBUG level so production runs aren't polluted.
        """
        try:
            payload = {"event": event_name, "kwargs": kwargs}
            self._queue.put_nowait(payload)
        except (_queue.Full, pickle.PicklingError, TypeError, ValueError) as e:
            self._record_drop()
            logger.debug(
                "audit_bridge.put(%s) dropped: %s", event_name, e,
            )
        except Exception as e:  # pragma: no cover - defensive (manager proxy)
            self._record_drop()
            logger.debug(
                "audit_bridge.put(%s) unexpected drop: %s", event_name, e,
            )

    @property
    def dropped_count(self) -> int:
        """Number of events dropped due to queue-full / serialization."""
        with self._drop_lock:
            return self._dropped_count

    def _record_drop(self) -> None:
        with self._drop_lock:
            self._dropped_count += 1

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_queue(maxsize: int) -> Any:
        """Create a Manager().Queue() suitable for cross-process sharing.

        Manager() owns its own server process which proxies the queue to
        child processes. The proxy address is exported to children via
        ``EKRS_AUDIT_QUEUE_ADDR`` so they can rebuild the proxy.
        """
        manager = multiprocessing.Manager()
        return manager.Queue(maxsize=maxsize), manager

    @classmethod
    def from_addr(cls, addr: str | None) -> "AuditEventBridge | None":
        """Worker-side factory: rebuild a Queue proxy from a Manager address.

        Returns ``None`` (not raising) when ``addr`` is missing — the worker
        then takes the legacy silent-drop path. The Manager address round-
        trip is sensitive to Manager server lifecycle, so we treat a
        failure as "no audit bridge available" rather than crashing the
        worker.
        """
        if not addr:
            logger.debug(
                "audit_bridge.from_addr: %s not set; worker will silent-drop",
                _QUEUE_ADDR_ENV,
            )
            return None
        try:
            manager = multiprocessing.managers.SyncManager(address=addr)
            manager.connect()
            # Queue is registered as 'Queue' on the Manager server.
            # Use the public API: call it via the registered proxy.
            queue = manager.Queue(maxsize=10_000)
            return cls(queue=queue)
        except Exception as e:
            logger.debug(
                "audit_bridge.from_addr: failed to connect (%s); silent-drop",
                e,
            )
            return None

    @staticmethod
    def export_addr(manager: "multiprocessing.managers.SyncManager") -> str:
        """Export a Manager's address so child processes can connect.

        Lifespan calls this once and stores the result in the env var.
        """
        addr: object = manager.address
        # In Python 3.9+, address is a (authkey, host:port) tuple. We only
        # need the connection part — children use it for ``SyncManager(address=...)``.
        conn_addr: str
        if isinstance(addr, tuple):
            tail = addr[1] if len(addr) > 1 else addr[0]
            conn_addr = str(tail)
        else:
            conn_addr = str(addr)
        os.environ[_QUEUE_ADDR_ENV] = conn_addr
        return conn_addr

    # ------------------------------------------------------------------
    # Consumer (main-process only)
    # ------------------------------------------------------------------

    def _drain_loop(self) -> None:
        """Drain queue → writer.write; survives writer exceptions."""
        while not self._stop_event.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except _queue.Empty:
                continue
            except (EOFError, OSError):
                # Manager died → bridge effectively dead. Exit loop.
                logger.warning("audit_bridge.drain: queue EOF, exiting drain loop")
                return
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("audit_bridge.drain: get() error: %s", e)
                continue
            try:
                if self._writer is None:
                    # No writer (worker side shouldn't run this loop, but
                    # defensive: drop rather than raise).
                    self._record_drop()
                    continue
                self._writer.write(payload["event"], **payload["kwargs"])
            except Exception as e:  # parent §204: never crash caller
                # Writer raised — log + keep draining so next event still lands.
                logger.debug(
                    "audit_bridge.drain: writer.write(%s) failed: %s",
                    payload.get("event"),
                    e,
                )


# ----------------------------------------------------------------------
# Module-level singleton for child processes
# ----------------------------------------------------------------------

_worker_bridge: Optional[AuditEventBridge] = None


def get_worker_bridge() -> Optional[AuditEventBridge]:
    """Return (lazily constructing) the worker-side AuditEventBridge.

    Reads ``EKRS_AUDIT_QUEUE_ADDR`` once and caches the bridge instance
    for the worker's lifetime. Returns ``None`` if the env var is
    missing or the Manager proxy can't be rebuilt — callers then fall
    back to the legacy ``writer.write()`` path (no-op if writer is
    missing too).
    """
    global _worker_bridge
    if _worker_bridge is None:
        addr = os.environ.get(_QUEUE_ADDR_ENV)
        _worker_bridge = AuditEventBridge.from_addr(addr)
    return _worker_bridge


__all__ = [
    "AuditEventBridge",
    "get_worker_bridge",
    "_QUEUE_ADDR_ENV",
]