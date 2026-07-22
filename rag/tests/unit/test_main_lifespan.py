"""Async unit test for the lifespan's fail-fast storage check.

The lifespan startup guard (ekrs_rag/main.py) raises RuntimeError if
SHARED_STORAGE_PATH does not exist on disk. This test drives the
app lifespan directly via `async with app.router.lifespan_context(app):`
and asserts the RuntimeError propagates — i.e., that a later `except`
in the same try block does NOT silently swallow it.

The test must be async because Starlette's lifespan_context is
async-only; a sync `with app.router.lifespan_context(app):` would
return a coroutine and fail to enter the context.
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest


def test_lifespan_passes_rest_port_to_qdrant_manager() -> None:
    """The REST-backed QdrantClient must receive QDRANT_PORT, not gRPC port."""
    source = Path(__file__).parents[2].joinpath("ekrs_rag", "main.py").read_text()

    assert "port=settings.QDRANT_PORT" in source
    assert "port=settings.QDRANT_GRPC_PORT" not in source


def _free_port() -> int:
    """Allocate an ephemeral port for the metrics exporter.

    Mirrors the helper in tests/integration/test_metrics_exporter.py:
    bind SO_REUSEADDR to avoid TIME_WAIT bind errors on repeated runs.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_lifespan_aborts_when_shared_storage_path_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Lifespan startup must raise RuntimeError when SHARED_STORAGE_PATH
    is an absolute path that does not exist on disk.

    Verifies the raise is not silently swallowed by a later `except`
    in the same try block (per reviewer Important finding).
    """
    # Point SHARED_STORAGE_PATH at an absolute path that does NOT exist.
    missing = tmp_path / "nonexistent-shared-storage"
    monkeypatch.setattr(
        "ekrs_rag.main.settings.SHARED_STORAGE_PATH", missing
    )
    # Bind the metrics sidecar to a free port so the lifespan does not
    # collide with prior test runs (mirrors test_metrics_exporter.py).
    port = _free_port()
    monkeypatch.setenv("METRICS_HOST", "127.0.0.1")
    monkeypatch.setenv("METRICS_PORT", str(port))
    # The lifespan's Qdrant/Redis init (after the storage check) is
    # unreachable here — the storage guard runs first, so we don't need
    # to mock them.

    from ekrs_rag.main import app

    with pytest.raises(RuntimeError) as exc_info:
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover — should not reach

    assert str(exc_info.value) == (
        f"SHARED_STORAGE_PATH={missing} does not exist; "
        "create the directory or fix the config before starting."
    )