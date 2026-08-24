"""Phase 13a T8 — query encode to_thread (P1-4) + callback failure reconciliation log (P1-5).

TDD red: this file lands before the implementations. Tests fail on:
- QdrantManager.search is async + encode goes through asyncio.to_thread
  (so query latency doesn't stall the event loop on bge-m3 ONNX run).
- CallbackRetryableError / network errors from httpx AsyncClient land a
  structured line in ``logs/callback_failures.log`` (ts / doc_hash /
  reason) for offline replay. Path is configurable via Settings, NOT
  /tmp — uses the existing RebuildingRotatingFileHandler rotation spec.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ekrs_rag.observability.callback_failure_log import (
    CallbackFailureLog,
    record_callback_failure,
)


# ---------------------------------------------------------------------------
# P1-4: QdrantManager.search is async + encode via to_thread
# ---------------------------------------------------------------------------


def test_qdrant_search_is_async() -> None:
    """``QdrantManager.search`` is `async def` — caller must await.

    Phase 13a T8 / P1-4: bge-m3 ONNX encode is a CPU-bound blocking call.
    If it ran on the event loop, query latency stalls. ``async def``
    lets the retriever ``await`` and the encode itself ride
    ``asyncio.to_thread`` (run in the default executor).
    """
    from ekrs_rag.retrieval.qdrant_client import QdrantManager

    assert inspect.iscoroutinefunction(QdrantManager.search), (
        "QdrantManager.search must be `async def` so encode runs in "
        "asyncio.to_thread (event loop unblocked)."
    )


@pytest.mark.asyncio
async def test_qdrant_search_runs_encode_in_to_thread() -> None:
    """Encode runs in ``asyncio.to_thread`` — verify by checking the
    thread id of the encode call differs from the test's coroutine
    thread id (always the event-loop thread in pytest-asyncio).

    Stub the EmbeddingService with a MagicMock that records its
    ``threading.get_ident()`` at call time. The test runs on the
    asyncio loop thread; to_thread dispatches to the default
    ThreadPoolExecutor, which uses different worker threads.
    """
    import threading
    from ekrs_rag.retrieval.qdrant_client import QdrantManager

    loop_thread_id = threading.get_ident()
    recorded: dict[str, int] = {}

    class _StubEmbedding:
        is_dummy = False

        def encode(self, texts: list[str]) -> Any:
            recorded["encode_thread"] = threading.get_ident()
            # Return a single fake encoded object (mimic bge-m3 output)
            return [
                MagicMock(
                    dense=[0.1] * 4,
                    sparse={"tok": 0.5},
                )
            ]

        def to_qdrant_sparse(self, sparse: Any) -> Any:
            return {"indices": [0], "values": [0.5]}

    stub_embed = _StubEmbedding()

    # Build a QdrantManager via __new__ to bypass __init__ (no real client)
    mgr = QdrantManager.__new__(QdrantManager)
    mgr._embedding_service = stub_embed
    mgr._collection_name = "rag_documents"

    # Stub the qdrant client so we don't need a real connection
    client = MagicMock()
    client.query_points = MagicMock(
        return_value=MagicMock(points=[])
    )
    mgr._client = client

    await mgr.search("test query", top_k=4)

    # The encode ran on a thread pool worker, NOT the loop thread
    assert "encode_thread" in recorded, "encode was never called"
    assert recorded["encode_thread"] != loop_thread_id, (
        f"encode ran on the event loop thread (id={loop_thread_id}); "
        f"expected to_thread off-loop. P1-4 fix is missing — the "
        f"blocking encode is still stalling query latency."
    )


# ---------------------------------------------------------------------------
# P1-5: Callback failure reconciliation log
# ---------------------------------------------------------------------------


def test_callback_failure_log_writes_structured_line(tmp_path: Path) -> None:
    """``record_callback_failure`` appends one JSON line per failure.

    Phase 13a T8 / P1-5: when the parser callback POST fails (network
    error, 5xx, timeout), the failure must be persisted for offline
    replay. Line shape: ``{"ts": "<ISO>", "doc_hash": "...", "reason": "..."}``.
    Path is configurable via Settings — defaults to ``logs/callback_failures.log``
    (NOT /tmp; mirrors audit.log rotation spec).
    """
    log_path = tmp_path / "callback_failures.log"
    log = CallbackFailureLog(log_path=str(log_path))

    record_callback_failure(log, doc_hash="abc123", reason="TimeoutError: connect timeout")
    record_callback_failure(log, doc_hash="def456", reason="500 Internal Server Error")

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, f"expected 2 lines; got {len(lines)}: {lines}"

    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["doc_hash"] == "abc123"
    assert parsed[0]["reason"] == "TimeoutError: connect timeout"
    assert "ts" in parsed[0] and len(parsed[0]["ts"]) >= 10  # ISO 8601 minimum
    assert parsed[1]["doc_hash"] == "def456"
    assert parsed[1]["reason"] == "500 Internal Server Error"


def test_callback_failure_log_rotation(tmp_path: Path) -> None:
    """``CallbackFailureLog`` uses ``RebuildingRotatingFileHandler`` (matches
    audit.log rotation spec: 100 MB × 5 gzip backups).

    Smoke check: the handler type is the rebuilding variant, NOT plain
    RotatingFileHandler. Plain handler lacks the on-rollover hook used
    elsewhere in the codebase.
    """
    from ekrs_rag.observability.audit_handler import RebuildingRotatingFileHandler

    log_path = tmp_path / "cb_fail.log"
    log = CallbackFailureLog(log_path=str(log_path))

    handlers = log._logger.handlers  # type: ignore[attr-defined]
    assert any(isinstance(h, RebuildingRotatingFileHandler) for h in handlers), (
        f"CallbackFailureLog must use RebuildingRotatingFileHandler for "
        f"on-rollover hook parity with audit.log; got handlers: "
        f"{[type(h).__name__ for h in handlers]}"
    )


def test_record_callback_failure_never_raises(tmp_path: Path) -> None:
    """Callback failure logging is best-effort: even when the file write
    raises (disk full, permission denied), the caller must NOT see the
    exception propagate — that would turn a non-critical observability
    failure into an ingestion failure.
    """
    log = CallbackFailureLog(log_path="/nonexistent/dir/cb_fail.log")
    # Should NOT raise even though the directory doesn't exist
    record_callback_failure(log, doc_hash="x", reason="anything")