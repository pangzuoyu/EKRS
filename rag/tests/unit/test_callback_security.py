"""Unit tests for the secure _send_callback path.

Covers:
- X-Parser-Token header injection (T6)
- 4xx does NOT retry (T7)
- 5xx retries up to PIPELINE_CALLBACK_MAX_ATTEMPTS (T7)
- Success after retry (T7)
"""
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ekrs_rag.ingestion.pipeline import IngestionPipeline
from ekrs_rag.security.callback_url import ParsedCallback


@pytest.mark.asyncio
async def test_send_callback_includes_x_parser_token(monkeypatch, tmp_path):
    """T6: _send_callback must set X-Parser-Token from build_callback_headers()."""
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")

    pipeline = IngestionPipeline(
        qdrant=MagicMock(),
        storage_path=tmp_path,
        parser_token="x" * 32,
    )

    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            return resp

    monkeypatch.setattr(
        "ekrs_rag.ingestion.pipeline.validate_callback_url",
        lambda url: ParsedCallback(scheme="https", host="parser.example.com", port=None, raw=url),
    )
    monkeypatch.setattr("ekrs_rag.ingestion.pipeline.httpx.AsyncClient", FakeClient)

    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.trace_id = "trace-1"
    notification.callback_url = "https://parser.example.com/cb"

    await pipeline._send_callback(notification, "success")

    assert captured["headers"]["X-Parser-Token"] == "x" * 32
    assert captured["json"]["rag_status"] == "success"
    assert captured["json"]["doc_hash"] == "abc"


class CountingClient:
    """Mock httpx.AsyncClient that yields a programmed sequence of status codes.

    Mirrors real httpx.Response.raise_for_status() semantics: raises
    HTTPStatusError when status_code >= 400 so the pipeline's
    `raise_for_status()` path is exercised in tests.
    """

    def __init__(self, status_sequence):
        self.sequence = list(status_sequence)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        resp = MagicMock()
        resp.status_code = self.sequence.pop(0)

        def _raise():
            if resp.status_code >= 400:
                err = httpx.HTTPStatusError(
                    f"{resp.status_code}",
                    request=MagicMock(),
                    response=resp,
                )
                raise err

        resp.raise_for_status = _raise
        return resp


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        "ekrs_rag.ingestion.pipeline.httpx.AsyncClient",
        lambda *a, **kw: client,
    )
    monkeypatch.setattr(
        "ekrs_rag.ingestion.pipeline.validate_callback_url",
        lambda url: ParsedCallback(scheme="https", host="parser.example.com", port=None, raw=url),
    )


@pytest.mark.asyncio
async def test_callback_does_not_retry_4xx(monkeypatch, tmp_path):
    """T7: 4xx is non-retryable; expect exactly 1 POST then surface as CallbackNonRetryableError.

    The retry decorator only matches CallbackRetryableError, so 4xx raises
    immediately to the caller (T9 wraps this in _send_callback_safely).
    """
    from ekrs_rag.ingestion.pipeline import CallbackNonRetryableError

    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MIN_SEC", 0.01)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MAX_SEC", 0.02)

    pipeline = IngestionPipeline(
        qdrant=MagicMock(), storage_path=tmp_path, parser_token="x" * 32,
    )
    client = CountingClient([403, 403, 403])
    _patch_client(monkeypatch, client)

    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.trace_id = "t"
    notification.callback_url = "https://parser.example.com/cb"

    with pytest.raises(CallbackNonRetryableError):
        await pipeline._send_callback(notification, "success")

    assert len(client.calls) == 1, (
        f"4xx should not retry; got {len(client.calls)} calls"
    )


@pytest.mark.asyncio
async def test_callback_retries_5xx(monkeypatch, tmp_path):
    """T7: 5xx is retryable; expect exactly PIPELINE_CALLBACK_MAX_ATTEMPTS POSTs."""
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MIN_SEC", 0.01)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MAX_SEC", 0.02)

    pipeline = IngestionPipeline(
        qdrant=MagicMock(), storage_path=tmp_path, parser_token="x" * 32,
    )
    client = CountingClient([500, 500, 500])
    _patch_client(monkeypatch, client)

    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.trace_id = "t"
    notification.callback_url = "https://parser.example.com/cb"

    with pytest.raises(Exception):  # CallbackRetryableError after exhaustion
        await pipeline._send_callback(notification, "success")

    assert len(client.calls) == 3, (
        f"5xx should retry 3 times; got {len(client.calls)} calls"
    )


@pytest.mark.asyncio
async def test_callback_succeeds_after_retry(monkeypatch, tmp_path):
    """T7: Success after transient 5xx should not raise."""
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MIN_SEC", 0.01)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MAX_SEC", 0.02)

    pipeline = IngestionPipeline(
        qdrant=MagicMock(), storage_path=tmp_path, parser_token="x" * 32,
    )
    client = CountingClient([500, 502, 200])
    _patch_client(monkeypatch, client)

    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.trace_id = "t"
    notification.callback_url = "https://parser.example.com/cb"

    await pipeline._send_callback(notification, "success")

    assert len(client.calls) == 3  # retried twice then succeeded