"""Security helpers for outbound callbacks and token handling.

Re-exports the legacy X-Admin-Key authentication symbols from
`ekrs_rag.security_legacy` and the new SSRF-mitigation callback URL
validation from `ekrs_rag.security.callback_url`. Consumers should
import from `ekrs_rag.security` only — the package directory shadows
the flat `security_legacy` module at import time, so the symbols
must be re-exported here to remain reachable.
"""
from __future__ import annotations

from ekrs_rag.security.callback_url import (
    CallbackURLBlockedError,
    ParsedCallback,
    validate_callback_url,
)
from ekrs_rag.security_legacy import (
    require_admin_key,
    verify_admin_key,
)

__all__ = [
    "CallbackURLBlockedError",
    "ParsedCallback",
    "require_admin_key",
    "validate_callback_url",
    "verify_admin_key",
]
