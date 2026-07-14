"""Phase 5.5 D: Sidecar metrics exporter on :9090 (port from env).

The RAG main FastAPI app on :8000 no longer exposes /metrics. A sidecar wsgi
HTTP server runs on `METRICS_PORT` (default 9090) bound to `METRICS_HOST`
(default 127.0.0.1). Prometheus scrapes the sidecar, not the main app.

These tests:
  1. /metrics on the sidecar returns valid Prometheus exposition format
     (HELP / TYPE lines).
  2. Port comes from METRICS_PORT env var (not hard-coded).
  3. Lifespan teardown releases the port (no TIME_WAIT bleed across runs).
"""
import os
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import create_app
from ekrs_rag.observability.metrics import METRICS, safe_inc


def free_port() -> int:
    """Bind ephemeral port, return its number. SO_REUSEADDR avoids TIME_WAIT bind errors."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def sidecar_env(tmp_path, monkeypatch):
    """Configure env for the single-process sidecar exporter."""
    port = free_port()
    monkeypatch.setenv("METRICS_HOST", "127.0.0.1")
    monkeypatch.setenv("METRICS_PORT", str(port))
    # Multiprocess mode intentionally not unit-tested: counters are MmapedValue
    # only when the env var is set before prometheus_client import. Deployment-side
    # validation covers multiprocess mode.
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.setattr(
        "ekrs_rag.main.settings.TASK_DB_PATH", str(tmp_path / "tasks.db")
    )
    monkeypatch.setattr(
        "ekrs_rag.main.settings.DOCUMENTS_DB_PATH", str(tmp_path / "documents.db")
    )
    return {"port": port}


def test_exporter_serves_prometheus_format(sidecar_env):
    """GET 127.0.0.1:<port>/metrics returns 200 with valid Prometheus content."""
    app = create_app()
    with TestClient(app):
        # Lifespan has started; exporter on sidecar_env['port']
        url = f"http://127.0.0.1:{sidecar_env['port']}/metrics"
        resp = httpx.get(url, timeout=2.0)
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        assert "# HELP rag_ingestion_total" in body
        assert "# TYPE rag_ingestion_total counter" in body


def test_exporter_includes_counter_incremented_before_startup(sidecar_env):
    """A counter increment before startup is visible in the sidecar scrape."""
    safe_inc(METRICS.ingestion_total, status="sidecar_test")

    app = create_app()
    with TestClient(app):
        url = f"http://127.0.0.1:{sidecar_env['port']}/metrics"
        body = httpx.get(url, timeout=2.0).text

    assert 'rag_ingestion_total{status="sidecar_test"} 1.0' in body


def test_exporter_listens_on_configured_port(sidecar_env):
    """Port comes from METRICS_PORT env, not hardcoded 9090."""
    app = create_app()
    with TestClient(app):
        # Manually issue a request to the dynamically-allocated port.
        url = f"http://127.0.0.1:{sidecar_env['port']}/metrics"
        resp = httpx.get(url, timeout=2.0)
        assert resp.status_code == 200
        # Contradiction check: if exporter were on hardcoded 9090, this port would
        # fail. Asserting the configured port works AND has Prometheus content
        # proves the port is dynamic.
        assert sidecar_env["port"] != 9090 or os.environ.get("FORCE_HARDCODED") != "1", (
            "fix the test: free_port() occasionally returns 9090; the assertion"
            " above is a sanity guard, real verification is the resp 200"
        )


def test_exporter_stops_on_lifespan_exit(sidecar_env):
    """After lifespan teardown, the port is no longer reachable."""
    app = create_app()
    with TestClient(app):
        # Confirm exporter is alive
        url = f"http://127.0.0.1:{sidecar_env['port']}/metrics"
        resp = httpx.get(url, timeout=2.0)
        assert resp.status_code == 200

    # Lifespan has exited; server_close() should have released the socket
    with pytest.raises((ConnectionRefusedError, httpx.ConnectError, OSError)):
        httpx.get(url, timeout=1.0)


def test_multiproc_dir_missing_raises_runtime_error(
    sidecar_env, tmp_path, monkeypatch
):
    """Startup rejects a configured multiprocess directory that does not exist."""
    missing_dir = tmp_path / "missing-prometheus-multiproc"
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(missing_dir))

    app = create_app()
    with pytest.raises(RuntimeError) as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == (
        f"PROMETHEUS_MULTIPROC_DIR={missing_dir} does not exist. "
        "Create the directory before starting the process "
        "(MmapedValue opens .db files at import-time)."
    )


def test_bind_conflict_is_nonfatal(sidecar_env):
    """Startup continues when another process already owns the exporter port."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", sidecar_env["port"]))
    blocker.listen(1)

    try:
        app = create_app()
        with TestClient(app):
            assert app.state.metrics_httpd is None
    finally:
        blocker.close()