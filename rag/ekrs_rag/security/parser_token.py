"""Token helpers for outgoing callbacks.

- Reads PARSER_TOKEN from env (single canonical token).
- Builds X-Parser-Token + X-EKRS-Version headers.
- Provides timing-safe comparison for any future self-check needs.
"""
from __future__ import annotations

import hmac
import os


MIN_TOKEN_LENGTH = 32


class CallbackAuthMissingError(RuntimeError):
    """Raised when PARSER_TOKEN is missing or too short."""


def safe_compare(a: str, b: str) -> bool:
    """Timing-safe equality check using hmac.compare_digest."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _read_token() -> str:
    raw = os.environ.get("PARSER_TOKEN", "")
    if not raw:
        raise CallbackAuthMissingError("PARSER_TOKEN is empty")
    if len(raw) < MIN_TOKEN_LENGTH:
        raise CallbackAuthMissingError(
            f"PARSER_TOKEN must be >= {MIN_TOKEN_LENGTH} characters "
            f"(got {len(raw)})"
        )
    return raw


def _ekrs_version() -> str:
    try:
        from importlib.metadata import version
        return version("ekrs-rag")
    except Exception:
        return "unknown"


def build_callback_headers() -> dict[str, str]:
    token = _read_token()
    return {
        "X-Parser-Token": token,
        "X-EKRS-Version": _ekrs_version(),
    }