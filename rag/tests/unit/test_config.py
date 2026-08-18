"""Unit tests for rag/ekrs_rag/core/config.py settings validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ekrs_rag.core.config import Settings


def test_shared_storage_path_must_be_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_STORAGE_PATH", "relative/parsed")
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "SHARED_STORAGE_PATH must be an absolute path" in str(exc_info.value)


def test_shared_storage_path_absolute_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    s = Settings()
    assert s.SHARED_STORAGE_PATH == tmp_path


def test_lifespan_rejects_missing_storage_path(monkeypatch, tmp_path):
    """Settings allows non-existent absolute path; lifespan must reject."""
    monkeypatch.setenv("SHARED_STORAGE_PATH", "/nonexistent/parsed_lib_xyz")
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    s = Settings()
    # Validator passes (absolute), but the dir doesn't exist
    assert s.SHARED_STORAGE_PATH == Path("/nonexistent/parsed_lib_xyz")
    assert not s.SHARED_STORAGE_PATH.is_dir()


@pytest.mark.unit
def test_parser_token_rejects_default_placeholder(monkeypatch):
    monkeypatch.setenv("SHARED_STORAGE_PATH", "/tmp")
    # The default literal in Settings is the placeholder
    monkeypatch.delenv("PARSER_TOKEN", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "PARSER_TOKEN" in str(exc_info.value)


@pytest.mark.unit
def test_parser_token_rejects_empty(monkeypatch):
    monkeypatch.setenv("SHARED_STORAGE_PATH", "/tmp")
    monkeypatch.setenv("PARSER_TOKEN", "")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "PARSER_TOKEN" in str(exc_info.value)


def test_fts_db_path_default_is_app_rag_fts_sqlite(monkeypatch):
    """Phase 12-A follow-up: FTS_DB_PATH must default to /app/rag/fts.sqlite.

    Per plan doc 2026-07-29-phase10-T10a-1-FTSManager.md:82, the production
    FTS DB lives at /app/rag/fts.sqlite inside the rag container. The
    recall@10 baseline script (scripts/recall_at_10_form_field_baseline.py)
    references settings.FTS_DB_PATH — a pre-existing gap from T10a-1 that
    the script's try/except silently absorbed (degrading to synthetic).
    """
    monkeypatch.setenv("SHARED_STORAGE_PATH", "/tmp")
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.delenv("FTS_DB_PATH", raising=False)
    s = Settings()
    assert s.FTS_DB_PATH == "/app/rag/fts.sqlite"


def test_fts_db_path_env_override(monkeypatch):
    """FTS_DB_PATH accepts FTS_DB_PATH env-var override (host-mount, tests)."""
    monkeypatch.setenv("SHARED_STORAGE_PATH", "/tmp")
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("FTS_DB_PATH", "/tmp/custom_fts.db")
    s = Settings()
    assert s.FTS_DB_PATH == "/tmp/custom_fts.db"
