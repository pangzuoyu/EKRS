"""Trace ID propagation via contextvars.

A contextvar is set per HTTP request by the observability middleware.
All audit writes and metric increments within that request see the same
trace_id without explicit threading.

A second contextvar `_skip_audit` gates audit writes (used by /healthz
to suppress lifecycle events that would otherwise dominate audit volume).
"""
from __future__ import annotations

from contextvars import ContextVar, Token

_trace_id_var: ContextVar[str] = ContextVar("ekrs_trace_id", default="unknown")
_skip_audit: ContextVar[bool] = ContextVar("ekrs_skip_audit", default=False)


def get_trace_id() -> str:
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> Token:
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: Token) -> None:
    _trace_id_var.reset(token)


def set_skip_audit(skip: bool) -> Token:
    """Set the skip-audit flag. Returns a token for reset()."""
    return _skip_audit.set(skip)


def reset_skip_audit(token: Token) -> None:
    _skip_audit.reset(token)


def get_skip_audit() -> bool:
    """True when audit writes should be suppressed (e.g. /healthz)."""
    return _skip_audit.get()
