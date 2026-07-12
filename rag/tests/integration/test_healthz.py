"""Integration test for GET /healthz — exposes audit_index status.

AUDIT_LOG_PATH and TASK_DB_PATH must be set BEFORE create_app() runs
because Settings reads env at module load time.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import create_app


def test_healthz_returns_audit_index_status(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

    app = create_app()
    client = TestClient(app)
    resp = client.get("/healthz")
    body = resp.json()

    assert "audit_index_loaded" in body
    assert "audit_index_size" in body
    assert "audit_index_load_seconds" in body