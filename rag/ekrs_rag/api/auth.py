"""PARSER_TOKEN authentication dependency for FastAPI routes.

Spec §鉴权: parser↔RAG shared secret. We validate via the X-Parser-Token
header against the PARSER_TOKEN env var. Tests disable this by setting
PARSER_TOKEN="" (no-op).

Zero-downtime rotation: `PARSER_TOKEN` accepts a comma-separated list of
tokens (e.g. `"old-token,new-token"`). Any token in the list is accepted.
See `docs/DEPLOYMENT.md` §Token rotation procedure for the SOP.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException


def _parse_expected_tokens(raw: str) -> frozenset[str]:
    """Split a PARSER_TOKEN env value on commas; return the set of accepted
    tokens. Empty / whitespace-only entries are filtered out.
    """
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def require_parser_token(x_parser_token: str | None = Header(None)) -> None:
    """FastAPI dependency: validate X-Parser-Token against PARSER_TOKEN env.

    Disabled when PARSER_TOKEN is empty/missing (development/test mode).
    Production deployments MUST set PARSER_TOKEN to a 32+ char secret
    (the Pydantic validator in `core.config.Settings` enforces this on the
    full comma-separated value).

    Multiple accepted tokens (comma-separated) enable zero-downtime rotation
    without restarting the parser fleet.
    """
    expected = _parse_expected_tokens(os.environ.get("PARSER_TOKEN", ""))
    if not expected:
        # Auth disabled — no-op
        return

    if not x_parser_token or x_parser_token not in expected:
        raise HTTPException(
            status_code=403, detail="Invalid or missing X-Parser-Token"
        )
