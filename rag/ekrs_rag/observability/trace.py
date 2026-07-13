"""Trace ID propagation via contextvars.

A contextvar is set per HTTP request by the observability middleware.
All audit writes and metric increments within that request see the same
trace_id without explicit threading.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

_trace_id_var: ContextVar[str] = ContextVar("ekrs_trace_id", default="unknown")


def get_trace_id() -> str:
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> Token:
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: Token) -> None:
    _trace_id_var.reset(token)
