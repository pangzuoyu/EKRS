"""Tests for POST /v1/calculate (spec §5, D3, D4 admin-required)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import app


@pytest.fixture
def client(monkeypatch):
    # PF1: Pydantic Settings v2 fields are mutable; setattr is the
    # idiomatic fixture pattern (matches test_admin_key.py).
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "ADMIN_KEY", "test-admin-key-32chars-xxxxxxxxx")
    monkeypatch.setattr(_settings, "PARSER_TOKEN", "")  # disable parser auth
    return TestClient(app)


def _admin_headers():
    return {"X-Admin-Key": "test-admin-key-32chars-xxxxxxxxx"}


def test_calculate_requires_admin_key(monkeypatch):
    """Unset ADMIN_KEY → 503 admin_key_not_configured."""
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "ADMIN_KEY", "")
    monkeypatch.setattr(_settings, "PARSER_TOKEN", "")
    client = TestClient(app)
    r = client.post(
        "/v1/calculate",
        json={"constraints": [], "scope_path": "industry/x", "strict": True},
    )
    assert r.status_code == 503


def test_calculate_missing_admin_header_returns_401(client):
    r = client.post(
        "/v1/calculate",
        json={"constraints": [], "scope_path": "industry/x", "strict": True},
    )
    assert r.status_code == 401


def test_calculate_missing_constraints_returns_422(client):
    r = client.post(
        "/v1/calculate",
        json={"scope_path": "industry/x"},
        headers=_admin_headers(),
    )
    assert r.status_code == 422


def test_calculate_strict_with_unsatisfiable_returns_400(client):
    r = client.post(
        "/v1/calculate",
        json={
            "constraints": [
                {"parameter": "temperature", "operator": "<=", "value": 50, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b1"}},
                {"parameter": "temperature", "operator": ">=", "value": 100, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b2"}},
            ],
            "scope_path": "industry/x",
            "strict": True,
            "allow_soft_fallback": True,  # strict wins, still 400
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 400
    assert "strict_violation" in r.text


def test_calculate_non_strict_with_soft_fallback_returns_200(client):
    r = client.post(
        "/v1/calculate",
        json={
            "constraints": [
                {"parameter": "temperature", "operator": "<=", "value": 50, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b1"}},
                {"parameter": "temperature", "operator": ">=", "value": 100, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b2"}},
                {"parameter": "temperature", "operator": "<=", "value": 200, "unit": "°C",
                 "priority": "REFERENCE", "confidence": 0.5, "source": {"block_id": "b3"}},
            ],
            "scope_path": "industry/x",
            "strict": False,
            "allow_soft_fallback": True,
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert "branches" in body.get("data", {})
    assert body["data"].get("conflict_details") is not None  # fallback flag
