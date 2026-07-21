"""X-Admin-Key authentication dependency (spec §16, D1).

Distinguishes missing/bad keys (401) from unset `ADMIN_KEY` config (503).
Endpoints that need admin scope declare `Depends(require_admin_key)`.
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from ekrs_rag.core.config import settings


def verify_admin_key(value: str | None, expected: str) -> bool:
    """Pure helper: return True iff value matches expected (non-empty expected)."""
    if not expected:
        return False
    if not value:
        return False
    return value == expected


def require_admin_key(
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
) -> None:
    """FastAPI dependency. 401 for missing/bad, 503 if ADMIN_KEY is empty."""
    # D3: read from Pydantic Settings (already loaded at app import).
    expected = (settings.ADMIN_KEY or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="admin_key_not_configured: ADMIN_KEY config is empty",
        )
    if not x_admin_key or x_admin_key != expected:
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-Admin-Key"
        )
