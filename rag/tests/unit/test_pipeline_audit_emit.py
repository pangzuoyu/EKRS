"""Regression tests for the audit-emit paths in IngestionPipeline callbacks.

These paths were introduced by T6/T9 and call the injected audit writer when
a callback branch fails (URL blocked / auth missing / best-effort transport
failure). The rest of the suite injects `audit_writer=None`, so the emit calls
were never exercised with a real writer — a call to a nonexistent method would
pass every existing test yet crash in production (main.py injects a real
`AuditWriter`).

Each test injects a REAL `AuditWriter` and asserts the event lands in the
audit log without raising. This pins the writer contract: the pipeline must
call `AuditWriter.write(...)`, the method that actually exists.
"""
import json

import pytest
from unittest.mock import MagicMock

from ekrs_rag.ingestion.pipeline import (
    CallbackNonRetryableError,
    IngestionPipeline,
)
from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.security.callback_url import CallbackURLBlockedError, ParsedCallback
from ekrs_rag.security.parser_token import CallbackAuthMissingError


def _read_events(audit_path):
    lines = [ln for ln in audit_path.read_text().splitlines() if ln.strip()]
    return [json.loads(ln)["event"] for ln in lines]


def _pipeline_with_writer(tmp_path):
    audit_path = tmp_path / "audit.log"
    writer = AuditWriter(str(audit_path))
    pipeline = IngestionPipeline(
        qdrant=MagicMock(),
        storage_path=tmp_path,
        parser_token="x" * 32,
        audit_writer=writer,
    )
    return pipeline, audit_path


def _notification():
    n = MagicMock()
    n.doc_hash = "abc"
    n.version = 1
    n.trace_id = "trace-1"
    n.callback_url = "https://parser.example.com/cb"
    return n


@pytest.mark.asyncio
async def test_url_blocked_emits_audit_event(monkeypatch, tmp_path):
    """callback_url_blocked branch must write to a real AuditWriter."""
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    pipeline, audit_path = _pipeline_with_writer(tmp_path)

    monkeypatch.setattr(
        "ekrs_rag.ingestion.pipeline.validate_callback_url",
        lambda url: (_ for _ in ()).throw(CallbackURLBlockedError("blocked")),
    )

    await pipeline._send_callback(_notification(), "success")

    assert "callback_url_blocked" in _read_events(audit_path)


@pytest.mark.asyncio
async def test_auth_missing_emits_audit_event(monkeypatch, tmp_path):
    """callback_auth_missing branch must write to a real AuditWriter."""
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    pipeline, audit_path = _pipeline_with_writer(tmp_path)

    monkeypatch.setattr(
        "ekrs_rag.ingestion.pipeline.validate_callback_url",
        lambda url: ParsedCallback(scheme="https", host="parser.example.com", port=None, raw=url),
    )
    monkeypatch.setattr(
        "ekrs_rag.ingestion.pipeline.build_callback_headers",
        lambda: (_ for _ in ()).throw(CallbackAuthMissingError("missing")),
    )

    await pipeline._send_callback(_notification(), "success")

    assert "callback_auth_missing" in _read_events(audit_path)


@pytest.mark.asyncio
async def test_best_effort_failure_emits_audit_event(monkeypatch, tmp_path):
    """callback_best_effort_failed branch must write to a real AuditWriter."""
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    pipeline, audit_path = _pipeline_with_writer(tmp_path)

    async def _boom(*a, **kw):
        raise CallbackNonRetryableError("403")

    monkeypatch.setattr(pipeline, "_send_callback", _boom)

    outcome = MagicMock()
    outcome.rag_status = "failed"
    outcome.error = "boom"

    await pipeline._send_callback_safely(_notification(), outcome)

    assert "callback_best_effort_failed" in _read_events(audit_path)
