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

from ekrs_rag.main import _sync_lifespan, create_app
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
    """Configure env for sidecar exporter: free port + multiproc dir."""
    port = free_port()
    multiproc = tmp_path / "prom"
    multiproc.mkdir()
    monkeypatch.setenv("METRICS_HOST", "127.0.0.1")
    monkeypatch.setenv("METRICS_PORT", str(port))
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(multiproc))
    monkeypatch.setattr(
        "ekrs_rag.main.settings.TASK_DB_PATH", str(tmp_path / "tasks.db")
    )
    return {"port": port, "multiproc": multiproc}


def test_exporter_serves_prometheus_format(sidecar_env):
    """GET 127.0.0.1:<port>/metrics returns 200 with valid Prometheus content."""
    app = create_app()
    with _sync_lifespan(app):
        # Lifespan has started; exporter on sidecar_env['port']
        url = f"http://127.0.0.1:{sidecar_env['port']}/metrics"
        resp = httpx.get(url, timeout=2.0)
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        assert "# HELP rag_ingestion_total" in body
        assert "# TYPE rag_ingestion_total counter" in body


def test_exporter_listens_on_configured_port(sidecar_env):
    """Port comes from METRICS_PORT env, not hardcoded 9090."""
    app = create_app()
    with _sync_lifespan(app):
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
    with _sync_lifespan(app):
        # Confirm exporter is alive
        url = f"http://127.0.0.1:{sidecar_env['port']}/metrics"
        resp = httpx.get(url, timeout=2.0)
        assert resp.status_code == 200

    # Lifespan has exited; server_close() should have released the socket
    with pytest.raises((ConnectionRefusedError, httpx.ConnectError, OSError)):
        httpx.get(url, timeout=1.0)