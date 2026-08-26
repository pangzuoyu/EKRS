"""Unit tests for AuditEventBridge — Phase 13c T1 cross-process AuditWriter.

Cross-process AuditWriter for pebble subprocess workers. Workers call
``bridge.put(event_name, **kwargs)`` → serializes to dict → writes to
``multiprocessing.Manager().Queue()`` → main-process consumer thread
drains → forwards to ``AuditWriter.write(event_name, **payload)``.

Layered fault tolerance (D2 decision):
- Manager startup failure → lifespan raises (fail-loud).
- bridge.put() queue.Full / serialization error → debug log + drop counter,
  NEVER raises (runtime silent drop).
- Consumer thread writer raises → exception isolated, consumer keeps running.

Verified behaviors:
1. round-trip: put + drain → writer.write called with correct event_name+kwargs
2. queue full → drop counter ++, put doesn't raise
3. writer raises → consumer stays alive, drains next event
4. bridge.stop() cleanly drains remaining + joins thread within 5s grace
"""

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest


# Phase 13c T1: AuditEventBridge doesn't exist yet. ImportError → RED.
def _import_bridge():
    from ekrs_rag.observability.audit_bridge import AuditEventBridge
    return AuditEventBridge


class TestAuditEventBridgeRoundTrip:
    """Bridge.put → consumer thread → writer.write round-trip works."""

    def test_put_event_writes_to_underlying_writer(self):
        """Single put(event_name, **kwargs) → writer.write sees same name + kwargs."""
        AuditEventBridge = _import_bridge()
        mock_writer = MagicMock()
        bridge = AuditEventBridge(writer=mock_writer, maxsize=100)
        bridge.start()

        try:
            bridge.put("channel_switched", from_channel="cpu", to_channel="gpu", reason="register")
            # Consumer polls queue.get(timeout=0.5) — give it room.
            time.sleep(0.5)
        finally:
            bridge.stop(timeout_s=2.0)

        mock_writer.write.assert_called_once_with(
            "channel_switched",
            from_channel="cpu",
            to_channel="gpu",
            reason="register",
        )

    def test_multiple_events_drain_in_order(self):
        """FIFO order preserved across consumer loop."""
        AuditEventBridge = _import_bridge()
        mock_writer = MagicMock()
        bridge = AuditEventBridge(writer=mock_writer, maxsize=100)
        bridge.start()

        try:
            for i in range(5):
                bridge.put("worker_uncaught", traceback=f"trace_{i}")
            time.sleep(0.5)
        finally:
            bridge.stop(timeout_s=2.0)

        # Drain captured calls — verify FIFO order.
        write_calls = [
            c for c in mock_writer.write.call_args_list
            if c[0][0] == "worker_uncaught"
        ]
        assert len(write_calls) == 5
        # Order preserved by queue.FIFO.
        for i, call in enumerate(write_calls):
            assert call.kwargs["traceback"] == f"trace_{i}"


class TestAuditEventBridgeOverflow:
    """Queue full path: drop counter, no exception (D2 runtime silent drop)."""

    def test_queue_full_increments_drop_counter(self):
        """Put when queue full → drop counter ++, NO exception raised."""
        AuditEventBridge = _import_bridge()
        # Slow writer → drain lags → fill the queue.
        mock_writer = MagicMock()
        mock_writer.write.side_effect = lambda *a, **kw: time.sleep(0.5)
        # maxsize=2 small enough to force overflow with 5 events.
        bridge = AuditEventBridge(writer=mock_writer, maxsize=2)
        bridge.start()

        try:
            # Burst 5 events — writer sleeps 0.5s each → queue fills fast.
            for i in range(5):
                bridge.put("channel_switched", from_channel="cpu", to_channel="gpu", reason=f"r{i}")
            # At least some drops expected. Counter is on the bridge.
            time.sleep(0.2)
            assert bridge.dropped_count >= 1, f"Expected drops, got {bridge.dropped_count}"
        finally:
            bridge.stop(timeout_s=2.0)

    def test_put_never_raises_on_serialization_error(self):
        """bridge.put with unserializable kwarg → drops + counter, doesn't raise."""
        AuditEventBridge = _import_bridge()
        mock_writer = MagicMock()
        bridge = AuditEventBridge(writer=mock_writer, maxsize=100)
        bridge.start()

        try:
            # Pass a non-picklable object (lambda) — Manager().Queue serialization fails.
            bridge.put("test_event", bad_kwarg=lambda x: x)  # type: ignore[arg-type]
            time.sleep(0.3)
        finally:
            bridge.stop(timeout_s=2.0)
        # If we got here without an exception → ✅ silent drop worked.


class TestAuditEventBridgeWriterExceptionIsolation:
    """Consumer thread survives writer.write raising."""

    def test_writer_exception_does_not_kill_consumer(self):
        """Writer.write raises once → consumer catches → next event still drains."""
        AuditEventBridge = _import_bridge()
        call_count = {"n": 0}

        def flaky_write(*args: Any, **kwargs: Any) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated writer fault")

        mock_writer = MagicMock(write=flaky_write)
        bridge = AuditEventBridge(writer=mock_writer, maxsize=100)
        bridge.start()

        consumer_thread_ref: list[threading.Thread | None] = [None]
        # Capture the consumer thread reference (it's bridge._thread).
        # We can't access private attrs cleanly here, so use public observable:
        # after writer raised, subsequent event still drains.
        try:
            bridge.put("event_a", x=1)
            bridge.put("event_b", x=2)
            time.sleep(0.5)
        finally:
            bridge.stop(timeout_s=2.0)

        # Both events attempted; the second one MUST have reached writer.
        assert call_count["n"] >= 1, "writer.write was never called"
        # Second event still drains (consumer alive).
        write_calls = mock_writer.write.call_args_list if hasattr(mock_writer.write, "call_args_list") else None
        # MagicMock.write.side_effect was reassigned to flaky_write, so call_args_list
        # is the MagicMock's, not flaky_write's. Use call_count instead.
        assert call_count["n"] >= 2, f"Consumer died after first exception; got {call_count['n']} calls"


class TestAuditEventBridgeLifecycle:
    """start() / stop() lifecycle + 5s grace (UQ-3)."""

    def test_stop_drains_remaining_events_before_joining(self):
        """stop(timeout_s=5) → queue drained → thread joined cleanly."""
        AuditEventBridge = _import_bridge()
        mock_writer = MagicMock()
        bridge = AuditEventBridge(writer=mock_writer, maxsize=100)
        bridge.start()

        for i in range(3):
            bridge.put("event_drain_test", i=i)

        # Stop with adequate grace → all 3 events drained.
        bridge.stop(timeout_s=5.0)

        assert mock_writer.write.call_count == 3

    def test_stop_returns_quickly_when_queue_empty(self):
        """Empty queue → stop joins within timeout (no spurious waits)."""
        AuditEventBridge = _import_bridge()
        mock_writer = MagicMock()
        bridge = AuditEventBridge(writer=mock_writer, maxsize=100)
        bridge.start()

        t0 = time.monotonic()
        bridge.stop(timeout_s=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"stop took {elapsed:.2f}s on empty queue"


class TestAuditEventBridgeDroppedCount:
    """dropped_count metric (counters via Prometheus or in-memory)."""

    def test_dropped_count_starts_at_zero(self):
        """Fresh bridge has dropped_count == 0."""
        AuditEventBridge = _import_bridge()
        bridge = AuditEventBridge(writer=MagicMock(), maxsize=100)
        assert bridge.dropped_count == 0

    def test_dropped_count_is_readonly_property(self):
        """dropped_count is exposed for metrics endpoint (read-only)."""
        AuditEventBridge = _import_bridge()
        bridge = AuditEventBridge(writer=MagicMock(), maxsize=100)
        # Should be accessible.
        _ = bridge.dropped_count
        # Setting should fail (frozen property or property descriptor).
        with pytest.raises((AttributeError, TypeError)):
            bridge.dropped_count = 5  # type: ignore[misc]