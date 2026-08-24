"""Phase 13b T3.4 — GPU health probe daemon thread in _init_child.

Verifies the 30s probe loop that calls
``EncodingRouter.force_re_register_gpu()`` from a daemon thread spawned
during pebble worker initialization.

Plan §T3.4 / review 🟡 #3: transient GPU faults (CUDA OOM, driver reset,
NVLink drop) must be detected within ``BGE_M3_GPU_PROBE_INTERVAL_S`` so
the router transitions cpu↔gpu and emits channel_switched audit.

Three tests:
1. probe daemon thread is spawned with name ``ekrs_gpu_probe``
2. probe calls ``force_re_register_gpu`` periodically
3. probe survives ``force_re_register_gpu`` exceptions

Probe only spawns when ``BGE_M3_GPU_ENABLED`` AND
``BGE_M3_GPU_PROBE_ENABLED`` are both True. Tests monkeypatch settings +
patch encoding_router.get_router so we don't need a real GPU.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from ekrs_rag.services import encoding_router


@pytest.fixture
def _gpu_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings override: GPU + probe enabled, very short interval.

    Monkeypatches the module-level ``settings`` from ``ekrs_rag.core.config``
    (the same pattern used by ``test_phase13b_t4``) — creating a fresh
    ``Settings()`` here would re-run the PARSER_TOKEN validator and fail
    in the test environment.
    """
    from ekrs_rag.core import config as _config

    monkeypatch.setattr(_config.settings, "BGE_M3_GPU_ENABLED", True)
    monkeypatch.setattr(_config.settings, "BGE_M3_GPU_PROBE_ENABLED", True)
    monkeypatch.setattr(_config.settings, "BGE_M3_GPU_PROBE_INTERVAL_S", 1)


def _wait_for_daemon_thread(name: str, timeout: float = 1.0) -> list[threading.Thread]:
    """Poll threading.enumerate() for threads matching the given name.

    Returns the list (possibly empty) of matching threads. Daemon threads
    die with their parent; after _init_child returns, the thread may
    already be scheduled but not yet started (start() called but run()
    not yet entered) — we accept that case too via is_alive() == False.
    """
    deadline = time.time() + timeout
    seen: list[threading.Thread] = []
    while time.time() < deadline:
        for t in threading.enumerate():
            if t.name == name and t not in seen:
                seen.append(t)
        if seen:
            break
        time.sleep(0.02)
    return seen


def test_init_child_spawns_health_probe_daemon_thread(
    _gpu_settings: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 13b T3.4: _init_child spawns a daemon thread named ``ekrs_gpu_probe``
    when BOTH BGE_M3_GPU_ENABLED and BGE_M3_GPU_PROBE_ENABLED are True.

    We don't run a real pebble subprocess — _init_child is called directly
    on the test thread (mimicking pebble's behavior). After invocation, at
    least one thread named ``ekrs_gpu_probe`` must exist with daemon=True.
    """
    from ekrs_rag.services import encoding_pool

    # Stub the GPU registration so we don't try to import torch.
    fake_router = MagicMock()
    fake_router.try_register_gpu.return_value = False
    fake_router.current_channel = "cpu"
    monkeypatch.setattr(
        encoding_router, "get_router", lambda: fake_router,
    )

    encoding_pool._init_child()

    matches = _wait_for_daemon_thread("ekrs_gpu_probe", timeout=1.0)
    assert len(matches) >= 1, (
        f"Expected at least one ekrs_gpu_probe daemon thread to spawn; "
        f"all threads: {[t.name for t in threading.enumerate()]}"
    )
    t = matches[0]
    assert t.name == "ekrs_gpu_probe"
    assert t.daemon is True, "GPU probe thread must be daemon"


def test_health_probe_calls_force_re_register_gpu_periodically(
    _gpu_settings: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 13b T3.4: probe loop calls force_re_register_gpu() repeatedly.

    We patch threading.Event.wait so the loop fires fast (no real 1s
    sleep). After _init_child returns, force_re_register_gpu must have
    been called ≥3 times within a brief window.
    """
    from ekrs_rag.services import encoding_pool

    # Patch the Event used inside _init_child. encoding_pool imports
    # ``threading as _t`` inside the GPU/probe branch (lazy import); we
    # monkeypatch threading.Event globally so the lazy-imported alias
    # sees the same class.
    real_event = threading.Event

    class _FastEvent(real_event):  # type: ignore[valid-type,misc]
        def wait(self, timeout=None):  # type: ignore[override]
            return False  # never set; loop continues immediately

    monkeypatch.setattr(threading, "Event", _FastEvent)

    # Stub router — track force_re_register_gpu calls.
    fake_router = MagicMock()
    fake_router.try_register_gpu.return_value = False
    fake_router.current_channel = "cpu"
    call_count = {"n": 0}

    def _track_force():
        call_count["n"] += 1

    fake_router.force_re_register_gpu.side_effect = _track_force
    monkeypatch.setattr(
        encoding_router, "get_router", lambda: fake_router,
    )

    encoding_pool._init_child()

    # Give the daemon thread a brief window to iterate. Event.wait is a
    # no-op, but thread scheduling still needs scheduler ticks.
    deadline = time.time() + 2.0
    while call_count["n"] < 3 and time.time() < deadline:
        time.sleep(0.05)

    assert call_count["n"] >= 3, (
        f"Expected ≥3 force_re_register_gpu calls within 2s; got {call_count['n']}"
    )


def test_health_probe_swallows_exceptions(
    _gpu_settings: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 13b T3.4: probe loop catches force_re_register_gpu exceptions
    and keeps running (must not kill the daemon thread).

    We make force_re_register_gpu raise; the loop must continue iterating
    without dying. We verify by waiting a short window and checking the
    call count ≥ 3 (the loop didn't die on the first exception).
    """
    from ekrs_rag.services import encoding_pool

    real_event = threading.Event

    class _FastEvent(real_event):  # type: ignore[valid-type,misc]
        def wait(self, timeout=None):  # type: ignore[override]
            return False

    monkeypatch.setattr(threading, "Event", _FastEvent)

    fake_router = MagicMock()
    fake_router.try_register_gpu.return_value = False
    fake_router.current_channel = "cpu"
    call_count = {"n": 0}

    def _explode():
        call_count["n"] += 1
        raise RuntimeError("simulated GPU probe failure")

    fake_router.force_re_register_gpu.side_effect = _explode
    monkeypatch.setattr(
        encoding_router, "get_router", lambda: fake_router,
    )

    encoding_pool._init_child()

    deadline = time.time() + 2.0
    while call_count["n"] < 3 and time.time() < deadline:
        time.sleep(0.05)

    assert call_count["n"] >= 3, (
        f"Probe must keep iterating despite force_re_register_gpu raising; "
        f"got only {call_count['n']} calls (loop probably died)"
    )