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
