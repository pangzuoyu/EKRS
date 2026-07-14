"""Tests for X-Admin-Key authentication dependency (D1, spec §16)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from ekrs_rag.core.config import settings
from ekrs_rag.security import require_admin_key, verify_admin_key


@pytest.fixture
def admin_key_env(monkeypatch):
    # PF1: Pydantic Settings v2 models are mutable by default; setattr is the
    # idiomatic way to override Pydantic settings in tests without env churn.
    monkeypatch.setattr(settings, "ADMIN_KEY", "test-secret-32chars-abcdefghijklmno")


def test_verify_admin_key_returns_true_on_match():
    assert verify_admin_key("test-secret-32chars-abcdefghijklmno", expected="test-secret-32chars-abcdefghijklmno") is True


def test_verify_admin_key_returns_false_on_mismatch():
    assert verify_admin_key("wrong", expected="right") is False


def test_verify_admin_key_returns_false_on_none():
    assert verify_admin_key(None, expected="right") is False


def test_verify_admin_key_returns_false_on_empty_value():
    assert verify_admin_key("", expected="right") is False


def test_require_admin_key_missing_header_raises_401(admin_key_env):
    with pytest.raises(HTTPException) as exc:
        require_admin_key(x_admin_key=None)
    assert exc.value.status_code == 401


def test_require_admin_key_wrong_value_raises_401(admin_key_env):
    with pytest.raises(HTTPException) as exc:
        require_admin_key(x_admin_key="bad")
    assert exc.value.status_code == 401


def test_require_admin_key_correct_value_passes(admin_key_env):
    require_admin_key(x_admin_key="test-secret-32chars-abcdefghijklmno")  # no raise


def test_require_admin_key_admin_key_unset_raises_503(monkeypatch):
    # PF1: simulate unset config by clearing the Pydantic field.
    monkeypatch.setattr(settings, "ADMIN_KEY", "")
    with pytest.raises(HTTPException) as exc:
        require_admin_key(x_admin_key="anything")
    assert exc.value.status_code == 503
    assert "admin_key_not_configured" in exc.value.detail
