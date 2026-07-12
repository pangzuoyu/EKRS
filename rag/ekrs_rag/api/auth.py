"""PARSER_TOKEN authentication dependency for FastAPI routes.

Spec §鉴权: parser↔RAG shared secret. We validate via the X-Parser-Token
header against the PARSER_TOKEN env var. Tests disable this by setting
PARSER_TOKEN="" (no-op).
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException


def require_parser_token(x_parser_token: str | None = Header(None)) -> None:
    """FastAPI dependency: validate X-Parser-Token against PARSER_TOKEN env.

    Disabled when PARSER_TOKEN is empty/missing (development/test mode).
    Production deployments MUST set PARSER_TOKEN to a 32+ char secret.
    """
    expected = os.environ.get("PARSER_TOKEN", "").strip()
    if not expected:
        # Auth disabled — no-op
        return

    if not x_parser_token or x_parser_token != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Parser-Token")
