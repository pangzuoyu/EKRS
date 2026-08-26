"""Phase 13b T2/T3 — EncodingRouter for GPU/CPU dispatch.

Plan: docs/superpowers/plans/2026-08-24-phase13b-gpu-encoder.md §T2 + §T3

State machine (review 🟢 #6 transition-only emit):
- ``current_channel`` ∈ {``"cpu"``, ``"gpu"``, ``"unknown"``}
- ``try_register_gpu()`` runs the self-check; sets state to "gpu" or "cpu".
- ``route(texts)`` dispatches based on current state.
- GPU raise → state machine transitions to "cpu" and emits
  ``channel_switched{from: gpu, to: cpu, reason}`` ONLY when state changes
  (no flap — three consecutive GPU errors still emit exactly one event).

The router is process-local (each pebble worker subprocess owns one
EncodingRouter instance). Cross-process state stays out — fault-tolerance
comes from per-process self-registration on _init_child (Phase 13a T4 +
T1.5 Item 5).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    # Importing embedding_service at runtime pulls onnxruntime + bge-m3
    # ONNX (~5s); we only need the type hints + exception class for the
    # encode_gpu raise path. Keep these behind TYPE_CHECKING so the module
    # is importable in milliseconds.
    from ..retrieval.embedding_service import EmbeddingUnavailableError, EncodedVector  # noqa: F401


# Runtime alias for EmbeddingUnavailableError — exposes the class without
# forcing the heavy ONNX import. Tests monkeypatch ``encoding_router.
# EmbeddingUnavailableError`` to control behavior. Loaded lazily on first
# attribute access via __getattr__ below.
_EmbeddingUnavailableError: type[Exception] | None = None  # filled by __getattr__


def __getattr__(name: str) -> Any:
    if name == "EmbeddingUnavailableError":
        global _EmbeddingUnavailableError
        if _EmbeddingUnavailableError is None:
            from ..retrieval import embedding_service
            _EmbeddingUnavailableError = embedding_service.EmbeddingUnavailableError
        return _EmbeddingUnavailableError
    if name == "EncodedVector":
        from ..retrieval import embedding_service
        return embedding_service.EncodedVector
    if name == "EmbeddingService":
        from ..retrieval import embedding_service
        return embedding_service.EmbeddingService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from . import torch_bge_m3

# NOTE: EmbeddingService is intentionally NOT imported at the top — pulling
# it in loads onnxruntime and the bge-m3 ONNX export (~5s cost). The CPU
# path (_encode_cpu) imports it lazily so unit tests that route via GPU or
# short-circuit before the CPU path can run without paying that cost.

logger = logging.getLogger(__name__)


@dataclass
class RouterState:
    """Mutable router state — guarded by Router._lock."""

    current_channel: str = "unknown"  # "unknown" | "cpu" | "gpu"
    registration_attempted: bool = False
    last_switch_ts: Optional[str] = None  # ISO-8601 UTC
    # Map reason → count, useful for ops dashboards.
    switch_count_by_reason: dict[str, int] = field(default_factory=dict)


class EncodingRouter:
    """Process-local GPU/CPU dispatch router with state machine.

    Single instance per pebble worker subprocess; thread-safe via ``_lock``.
    """

    def __init__(
        self,
        *,
        queue_depth_provider: Optional[Any] = None,
        max_queue_depth_for_gpu: int = 10,
    ) -> None:
        # RLock (reentrant) — try_register_gpu holds the lock and may call
        # _record_transition which also tries to acquire. Plain Lock would
        # deadlock on the same thread.
        self._lock = threading.RLock()
        self._state = RouterState()
        # Optional queue-depth provider — returns int. When depth > max,
        # routing defers to CPU (plan T3.1 overload protection).
        self._queue_depth_provider = queue_depth_provider
        self._max_queue_depth_for_gpu = max_queue_depth_for_gpu

    # ----- public read-only surface -----

    @property
    def current_channel(self) -> str:
        with self._lock:
            return self._state.current_channel

    @property
    def is_gpu_available(self) -> bool:
        with self._lock:
            return self._state.current_channel == "gpu"

    # ----- T2.2 / T2.4 — registration -----

    def try_register_gpu(
        self,
        *,
        model_dir: Path | None = None,
        probes: list[dict[str, str]] | None = None,
    ) -> bool:
        """Run self-check; set state to gpu or cpu based on outcome.

        Idempotent — calling twice is safe (self-check is the same
        operation; we keep the first outcome so it doesn't flap). Always
        records ``registration_attempted=True`` once called.

        Returns True iff GPU channel is registered.
        """
        with self._lock:
            if self._state.registration_attempted:
                return self._state.current_channel == "gpu"
            self._state.registration_attempted = True

        passed = torch_bge_m3._self_check(model_dir=model_dir, probes=probes)

        with self._lock:
            new_channel = "gpu" if passed else "cpu"
            old_channel = self._state.current_channel
            self._state.current_channel = new_channel
            if old_channel == "unknown" or old_channel != new_channel:
                self._record_transition(
                    from_channel=old_channel,
                    to_channel=new_channel,
                    reason="self_check_pass" if passed else "self_check_fail",
                )
            return passed

    # ----- T3.1 — dispatch -----

    def route(self, texts: list[str]) -> list[EncodedVector]:
        """Encode via GPU if registered and queue below threshold; else CPU.

        GPU failures transparently fall back to CPU and transition the
        state machine (transition-only audit emit; review 🟢 #6).
        """
        if not texts:
            return []

        # Snapshot state under lock; release before doing heavy work.
        with self._lock:
            channel = self._state.current_channel
            if self._queue_depth_provider is not None:
                try:
                    depth = int(self._queue_depth_provider())
                except Exception:  # pragma: no cover - defensive
                    depth = 0
            else:
                depth = 0

        # Overload protection — even with GPU registered, queue overflow
        # routes to CPU (plan T3.1).
        if channel == "gpu" and depth > self._max_queue_depth_for_gpu:
            return self._encode_cpu(texts)

        if channel == "gpu":
            # Lazy-resolve the exception class on first encode call (not
            # module load) so the heavy ONNX import stays out of unit-test
            # collection. Cached in a module global after first use.
            global _EmbeddingUnavailableError
            if _EmbeddingUnavailableError is None:
                from ..retrieval import embedding_service
                _EmbeddingUnavailableError = embedding_service.EmbeddingUnavailableError
            try:
                return torch_bge_m3.encode_gpu(texts)
            except _EmbeddingUnavailableError as e:
                logger.warning("router.route: GPU unavailable, falling back to CPU: %s", e)
                self._transition("gpu", "cpu", reason="unavailable")
                return self._encode_cpu(texts)
            except Exception as e:
                # Catch OOM + any other torch-side exception; logs full trace
                # so operators can diagnose, but does not crash the worker.
                logger.warning(
                    "router.route: GPU encode failed (%s), falling back to CPU", e,
                )
                self._transition("gpu", "cpu", reason="encode_error")
                return self._encode_cpu(texts)

        # channel is cpu or unknown → CPU fallback.
        return self._encode_cpu(texts)

    def force_re_register_gpu(self) -> bool:
        """Clear registration flag and re-run self-check.

        Used by the 30s health probe (plan T3.3) — only flips state when
        the new self-check outcome differs from current state, so we don't
        spam channel_switched audit events on healthy hosts.
        """
        with self._lock:
            self._state.registration_attempted = False
        return self.try_register_gpu()

    # ----- internal helpers -----

    def _encode_cpu(self, texts: list[str]) -> list[EncodedVector]:
        # Lazy import — keeps EncodingRouter importable in environments
        # without onnxruntime (rare but matches EmbeddingService pattern).
        # Tests monkeypatch ``encoding_router.EmbeddingService`` via the
        # module-level __getattr__ below.
        from ..retrieval import embedding_service
        EmbeddingService = embedding_service.EmbeddingService
        return EmbeddingService().encode(texts)

    def _transition(self, from_channel: str, to_channel: str, *, reason: str) -> None:
        """Move to a new channel; emit channel_switched only on actual change.

        Review 🟢 #6 mandate: three GPU errors in a row still produce ONE
        audit event because state stays at "cpu" after the first emit.
        """
        with self._lock:
            if self._state.current_channel != to_channel:
                self._record_transition(
                    from_channel=from_channel,
                    to_channel=to_channel,
                    reason=reason,
                    locked=True,
                )

    def _record_transition(
        self,
        *,
        from_channel: str,
        to_channel: str,
        reason: str,
        locked: bool = False,
    ) -> None:
        """Update state + emit best-effort audit event.

        Caller is responsible for the lock (or ``locked=True`` to take it).
        Audit emission failures are swallowed (parent §204) — never let an
        audit error cascade into the encode path.
        """
        if not locked:
            self._lock.acquire()
        try:
            self._state.current_channel = to_channel
            self._state.last_switch_ts = datetime.now(timezone.utc).isoformat()
            self._state.switch_count_by_reason[reason] = (
                self._state.switch_count_by_reason.get(reason, 0) + 1
            )
        finally:
            if not locked:
                self._lock.release()

        self._emit_channel_switched(
            from_channel=from_channel,
            to_channel=to_channel,
            reason=reason,
        )

    def _emit_channel_switched(
        self,
        *,
        from_channel: str,
        to_channel: str,
        reason: str,
    ) -> None:
        """Best-effort emit of channel_switched audit event.

        Swallows all errors per parent §204. Two paths:

        1. **Main process fast path**: ``AuditWriter`` available → call
           ``writer.write`` directly. Old behavior preserved for the
           parent process (Phase 5.5 audit).

        2. **Worker subprocess path** (Phase 13c T1): no in-process
           writer (worker doesn't init one), so we forward via the
           cross-process ``AuditEventBridge``. The bridge consumes the
           event on the main process and writes to the same audit.log
           the rest of the system uses.

        If neither path is available, the event is silently dropped
        — operators see the router state-transition log line instead.
        """
        # Main-process fast path
        try:
            from ..observability.audit import get_writer
            writer = get_writer()
            if writer is not None:
                writer.write(
                    "channel_switched",
                    from_channel=from_channel,
                    to_channel=to_channel,
                    reason=reason,
                )
                return
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("channel_switched main-path writer dropped: %s", e)
            # fall through to bridge path

        # Worker-process bridge path (Phase 13c T1)
        try:
            from ..observability.audit_bridge import get_worker_bridge
            worker_bridge = get_worker_bridge()
            if worker_bridge is not None:
                worker_bridge.put(
                    "channel_switched",
                    from_channel=from_channel,
                    to_channel=to_channel,
                    reason=reason,
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("channel_switched bridge-path dropped: %s", e)


# Module-level singleton — pebble _init_child is called once per worker
# subprocess, so each process owns one router instance. Workers never share
# state; cross-process fault recovery is handled by per-process self-check.
_router_singleton: Optional[EncodingRouter] = None


def get_router() -> EncodingRouter:
    """Return the process-local EncodingRouter singleton.

    Tests can inject a fresh instance via ``reset_router()`` to avoid
    cross-test state leakage. Production callers should always use this
    getter so all workers share the same per-process singleton contract.
    """
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = EncodingRouter()
    return _router_singleton


def reset_router() -> None:
    """Drop the singleton (test helper). Production code never calls this."""
    global _router_singleton
    _router_singleton = None


__all__ = ["EncodingRouter", "RouterState", "get_router", "reset_router"]