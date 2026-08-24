"""Phase 13a T4 — EncodingPool (pebble subprocess pool).

Pebble.ProcessPool wraps subprocess workers for the Step5 encode path.
Each worker subprocess initializes once via ``_init_child`` (five explicit
items per plan T4.3 / eng-review Issue 3 / 13b T1.5):

1. ``PROMETHEUS_MULTIPROC_DIR`` — Prometheus child process must write to
   the shared multiproc dir; without this, child counters are silently
   lost.
2. ``EmbeddingService.warm_up(settings)`` — pre-load bge-m3 ONNX once
   per worker (saves ~1-2s per task). When ONNX is missing (dummy mode),
   warm_up logs a warning and continues — pool still serves tasks.
3. ``logging.getLogger("httpx").setLevel(WARNING)`` — silences noisy
   httpx trace logs that otherwise dominate the debug log under load.
4. ``sys.excepthook`` — reports uncaught traceback via audit so a pebble
   subprocess crash doesn't disappear silently.
5. Phase 13b T1.5 — CUDA context pre-warm: ``torch.cuda.init()`` + a
   one-shot ``torch.tensor([0], device="cuda:N")`` so the first GPU call
   from inside ``encode_gpu`` doesn't pay the ~1s CUDA context bring-up
   cost during a hot task. No-op when torch / CUDA unavailable.

Public API:
- ``EncodingPool(settings)`` — wraps pebble.ProcessPool, registers tasks.
- ``async submit(fn, **kwargs) -> task_id`` — non-blocking dispatch.
- ``async wait(task_id) -> dict`` — blocks for outcome; converts
  ``ProcessExpired`` (timeout kill) to structured ``task_timeout`` dict.
- ``stop()`` — idempotent close.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import traceback
import uuid
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable

from pebble import ProcessExpired, ProcessPool

from ..core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker subprocess initializer (called once per worker by pebble)
# ---------------------------------------------------------------------------


def _init_child() -> None:
    """pebble worker subprocess initializer.

    Five explicit items per plan T4.3 (eng-review Issue 3) + 13b T1.5:

    1. PROMETHEUS_MULTIPROC_DIR — Prometheus child process must write to
       shared multiproc dir; without this, child counters are lost.
    2. EmbeddingService pre-warm — loads bge-m3 ONNX once per worker.
       When ONNX is missing (dummy mode), the constructor falls back
       to dummy and logs a warning — pool still serves tasks.
    3. httpx logger WARNING — silences noisy httpx trace logs.
    4. sys.excepthook — reports uncaught traceback via audit.
    5. CUDA context pre-warm (13b T1.5 review 🟡 #5) — ``torch.cuda.init()``
       + one-shot ``torch.tensor([0], device=f"cuda:{device_id}")`` so the
       first GPU call from ``encode_gpu`` doesn't pay the ~1s CUDA context
       bring-up cost during a hot task. No-op when torch / CUDA unavailable.

    Called by pebble once per worker. MUST NOT raise — if it does,
    the worker subprocess dies and pebble spawns another one. We
    catch + log everything so a transient init error does not wedge
    the worker.
    """
    # Item 1: Prometheus multiproc dir env var
    multiproc_dir = settings.PROMETHEUS_MULTIPROC_DIR
    if multiproc_dir:
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = multiproc_dir

    # Item 2: Pre-load bge-m3 ONNX. When the model is missing (dummy mode),
    # the constructor logs a warning and continues. We wrap in try/except
    # as belt-and-suspenders so a transient path issue doesn't kill the worker.
    try:
        from ..retrieval.embedding_service import EmbeddingService

        _ = EmbeddingService()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "init_child: EmbeddingService pre-warm failed (will use dummy mode): %s",
            e,
        )

    # Item 3: Silence httpx trace logs (bge-m3 client uses httpx internally).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Item 5 (Phase 13b T1.5 review 🟡 #5): CUDA context pre-warm.
    # torch.cuda.init() forces CUDA driver bring-up now (when the worker
    # is still cold and timing matters less) instead of inside the first
    # encode_gpu() call. No-op when BGE_M3_GPU_ENABLED is False or when
    # torch / CUDA aren't available — the CPU path keeps working.
    if settings.BGE_M3_GPU_ENABLED:
        try:
            import torch  # noqa: WPS433 — lazy import (heavy)

            if torch.cuda.is_available():
                torch.cuda.init()
                _ = torch.tensor(
                    [0],
                    device=f"cuda:{settings.BGE_M3_GPU_DEVICE_ID}",
                )
                logger.info(
                    "init_child: CUDA context pre-warmed on cuda:%d",
                    settings.BGE_M3_GPU_DEVICE_ID,
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "init_child: CUDA pre-warm failed (will fall back to CPU at encode time): %s",
                e,
            )

    # Phase 13b T2.4 (plan §T2.4): run the GPU self-check and register
    # the GPU channel on the per-process EncodingRouter singleton. Failure
    # to register is non-fatal — the worker silently falls back to CPU.
    # Wrapped in try/except because self-check imports lazy heavy modules
    # (EmbeddingService + onnxruntime) and a cold-start OOM must not kill
    # the worker.
    if settings.BGE_M3_GPU_ENABLED:
        try:
            from . import encoding_router as _er
            _er.reset_router()  # ensure fresh registration per worker
            _er.get_router().try_register_gpu()
            logger.info(
                "init_child: GPU channel registered (current_channel=%s)",
                _er.get_router().current_channel,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "init_child: GPU registration failed (will use CPU): %s",
                e,
            )

        # Phase 13b T3.4 (plan §T3.4): GPU health probe daemon thread.
        # Periodically calls ``force_re_register_gpu()`` so a transient GPU
        # fault (CUDA OOM, driver reset, NVLink drop) is detected within
        # ``BGE_M3_GPU_PROBE_INTERVAL_S`` and the router state machine
        # transitions to cpu via the existing channel_switched audit.
        # Daemon thread dies naturally with the worker subprocess; we
        # don't track it (worker lifetime == thread lifetime).
        if settings.BGE_M3_GPU_PROBE_ENABLED:
            try:
                import threading as _t  # noqa: WPS433 — lazy import

                _stop_event = _t.Event()

                def _probe_loop() -> None:
                    while not _stop_event.is_set():
                        _stop_event.wait(settings.BGE_M3_GPU_PROBE_INTERVAL_S)
                        if _stop_event.is_set():
                            break
                        try:
                            _er.get_router().force_re_register_gpu()
                        except Exception as _probe_err:  # pragma: no cover
                            logger.debug(
                                "init_child: GPU probe force_re_register failed: %s",
                                _probe_err,
                            )

                _t.Thread(
                    target=_probe_loop,
                    name="ekrs_gpu_probe",
                    daemon=True,
                ).start()
                logger.info(
                    "init_child: GPU health probe spawned (interval=%ds)",
                    settings.BGE_M3_GPU_PROBE_INTERVAL_S,
                )
            except Exception as _probe_spawn_err:  # pragma: no cover
                logger.warning(
                    "init_child: GPU health probe spawn failed: %s",
                    _probe_spawn_err,
                )

    # Item 4: sys.excepthook — report uncaught traceback via audit so a
    # pebble subprocess crash doesn't disappear silently. Best-effort;
    # if AuditWriter isn't initialized (worker spawn before lifespan),
    # we just log + call the default excepthook.
    def _child_excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: Any,
    ) -> None:
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("init_child: uncaught exception in worker: %s", tb_text)
        try:
            from ..observability.audit import get_writer

            writer = get_writer()
            if writer is not None:
                writer.write(
                    "worker_uncaught",
                    traceback=tb_text[:4096],  # bound payload size
                )
        except Exception:  # pragma: no cover - defensive
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _child_excepthook


# ---------------------------------------------------------------------------
# EncodingPool
# ---------------------------------------------------------------------------


class EncodingPool:
    """pebble.ProcessPool wrapper for Step5 worker subprocess dispatch.

    The pool owns a single ``pebble.ProcessPool`` with up to
    ``settings.EKRS_ENCODING_MAX_WORKERS`` worker subprocesses (default 2,
    plan T4.3 / eng-review Issue 2). Each worker subprocess initializes
    once via ``_init_child`` (bge-m3 ONNX pre-warm, etc.).

    submit() schedules a call on the pool and returns a task_id immediately
    (non-blocking — the spawn happens in background). wait() blocks for
    the task's outcome, converting ``ProcessExpired`` (pebble subprocess
    killed by timeout) to a structured ``task_timeout`` dict so callers
    never see an exception escape.

    Timeout semantics: ``pebble.schedule(timeout=1800.0)`` fires when a
    worker exceeds 30 minutes; pebble kills the subprocess and
    ``future.result()`` raises ``ProcessExpired``. We catch it and return
    a dict so the calling code (T5 notify handler) doesn't have to
    handle pebble-specific exceptions.
    """

    def __init__(self, settings_obj: Any) -> None:
        self._settings = settings_obj
        self._max_workers = int(getattr(settings_obj, "EKRS_ENCODING_MAX_WORKERS", 2))
        self._task_timeout_s = 1800.0  # 30 min — T4 default
        self._pool: ProcessPool = ProcessPool(
            max_workers=self._max_workers,
            initializer=_init_child,
        )
        self._tasks: dict[str, Any] = {}

    async def submit(self, fn: Callable[..., Any], **kwargs: Any) -> str:
        """Schedule the worker fn on the pool. Returns task_id (non-blocking).

        ``fn`` must be a module-level callable (pebble pickles it). ``kwargs``
        must be picklable — no live async objects, no connections. See
        ``Step5Payload`` in ``step5_worker.py`` for the canonical picklable
        shape.

        The returned task_id is a UUID hex; pass it to ``wait(task_id)`` to
        block for the outcome.
        """
        fut = self._pool.schedule(
            fn, kwargs=kwargs, timeout=self._task_timeout_s,
        )
        task_id = uuid.uuid4().hex
        self._tasks[task_id] = fut
        return task_id

    async def wait(self, task_id: str, *, doc_hash: str = "") -> dict[str, Any]:
        """Block until task completes; return outcome dict.

        Handles ``ProcessExpired`` (subprocess killed by timeout) →
        returns ``{"rag_status": "failed", "error_code": "task_timeout", ...}``
        instead of raising. Also catches any other exception (worker
        crashed before submit returned, etc.) and reports as
        ``task_failed`` so callers never see a bare exception.

        Phase 13a T6: when a timeout fires, emit ``task_timeout_killed``
        audit event (doc_hash + task_id + timeout_s) before returning the
        structured failure dict. Best-effort — audit failure is swallowed
        so it doesn't compound the underlying worker crash.

        Pops the task from ``self._tasks`` on completion (success or fail).
        """
        fut = self._tasks.get(task_id)
        if fut is None:
            return {
                "rag_status": "failed",
                "error": "unknown task_id",
                "error_code": "unknown_task_id",
            }
        try:
            # Run blocking fut.result() in a thread to avoid blocking event loop
            result = await asyncio.to_thread(fut.result)
        except (ProcessExpired, FuturesTimeoutError) as e:
            # pebble 5.x translates timeout kills to one of:
            # - ProcessExpired (subprocess forcibly killed)
            # - concurrent.futures.TimeoutError (fut.result timed out)
            # Both mean "task did not complete within timeout"; map to
            # the structured task_timeout outcome so callers don't see
            # pebble-specific exceptions.
            logger.warning(
                "encoding_pool: task_timeout task_id=%s err=%s", task_id, e,
            )
            # Phase 13a T6: emit audit event before returning the
            # structured failure dict. doc_hash is supplied by caller
            # (notify handler) so the operator sees which document was
            # killed; falls back to task_id-only when caller omits it.
            try:
                from ..observability.audit import get_writer

                writer = get_writer()
                if writer is not None:
                    writer.write(
                        "task_timeout_killed",
                        doc_hash=doc_hash or "",
                        task_id=task_id,
                        timeout_s=self._task_timeout_s,
                    )
            except Exception:
                pass
            return {
                "rag_status": "failed",
                "error": f"task_timeout after {self._task_timeout_s}s: {e}",
                "error_code": "task_timeout",
            }
        except Exception as e:
            logger.error(
                "encoding_pool: task failed task_id=%s err=%s\n%s",
                task_id, e, traceback.format_exc(),
            )
            return {
                "rag_status": "failed",
                "error": str(e),
                "error_code": "task_failed",
            }
        finally:
            self._tasks.pop(task_id, None)
        return result

    def stop(self) -> None:
        """Drain + close pool (idempotent).

        Pebble's ``ProcessPool.close()`` + ``join()`` raise if called twice
        (the underlying concurrent.futures.Executor raises ``RuntimeError``).
        We catch + log so the caller can call ``stop()`` repeatedly without
        worrying about lifecycle ordering (FastAPI shutdown + test teardown
        + signal handler, etc.).
        """
        try:
            self._pool.close()
        except Exception as e:
            logger.debug("encoding_pool.stop: close raised (likely already closed): %s", e)
        try:
            self._pool.join()
        except Exception as e:
            logger.debug("encoding_pool.stop: join raised (likely already joined): %s", e)


# Quiet down the unused-import warning for `time` (kept for future debug timing).
_ = time