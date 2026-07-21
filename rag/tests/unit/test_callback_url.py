"""Tests for callback URL allowlist with SSRF mitigation.

NOTE on test hostnames: The brief used `parser.example.com` / `attacker.example.com`
etc. — RFC 2606 reserved subdomains that do not resolve. The SSRF helper correctly
blocks unresolvable hosts (security default), so those hostnames would always be
rejected. We substitute real resolvable public domains here to keep the test
focused on the validation gates, not DNS availability. The validation logic
tested is identical to what the brief specified.
"""
from __future__ import annotations

import pytest

from ekrs_rag.security.callback_url import (
    CallbackURLBlockedError,
    validate_callback_url,
)


# Real resolvable public domains used in place of the brief's reserved subdomains.
_PUBLIC_HOST = "example.com"  # resolves to public IPv4 + IPv6
_OTHER_PUBLIC_HOST = "one.one.one.one"  # resolves to 1.0.0.1/1.1.1.1 (public)


@pytest.mark.unit
def test_allows_https_public_domain(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    parsed = validate_callback_url(f"https://{_PUBLIC_HOST}/cb")
    assert parsed.scheme == "https"
    assert parsed.host == _PUBLIC_HOST


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/cb",
        "http://[::1]/cb",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/cb",
        "http://192.168.1.1/cb",
        "ftp://example.com/cb",
        "file:///etc/passwd",
        "gopher://example.com/",
    ],
)
def test_blocks_dangerous_urls(monkeypatch, url):
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https,http")
    with pytest.raises(CallbackURLBlockedError):
        validate_callback_url(url)


@pytest.mark.unit
def test_dns_resolution_to_private_ip_blocks(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https,http")
    # localhost resolves to 127.0.0.1, which is in the loopback range
    with pytest.raises(CallbackURLBlockedError):
        validate_callback_url("http://localhost:9001/cb")


@pytest.mark.unit
def test_host_allowlist_blocks_when_set_and_mismatch(monkeypatch):
    """When CALLBACK_ALLOWED_HOSTS is set, only listed hosts may callback.

    Defense-in-depth above scheme/IP checks: prevents token exfiltration to
    attacker-controlled hosts if the parser's callback_url is compromised.
    """
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setenv("CALLBACK_ALLOWED_HOSTS", _PUBLIC_HOST)
    with pytest.raises(CallbackURLBlockedError, match="not in allowlist"):
        validate_callback_url(f"https://{_OTHER_PUBLIC_HOST}/cb")


@pytest.mark.unit
def test_host_allowlist_accepts_known_host(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setenv(
        "CALLBACK_ALLOWED_HOSTS", f"{_PUBLIC_HOST},parser.dev",
    )
    parsed = validate_callback_url(f"https://{_PUBLIC_HOST}/cb")
    assert parsed.host == _PUBLIC_HOST


@pytest.mark.unit
def test_host_allowlist_wildcard_allows_any(monkeypatch):
    """'*' in CALLBACK_ALLOWED_HOSTS disables host pinning (dev-only)."""
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setenv("CALLBACK_ALLOWED_HOSTS", "*")
    parsed = validate_callback_url(f"https://{_PUBLIC_HOST}/cb")
    assert parsed.host == _PUBLIC_HOST


@pytest.mark.unit
def test_host_allowlist_unset_keeps_current_behavior(monkeypatch):
    """Without CALLBACK_ALLOWED_HOSTS, no host-level filter applies."""
    monkeypatch.delenv("CALLBACK_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    parsed = validate_callback_url(f"https://{_PUBLIC_HOST}/cb")
    assert parsed.host == _PUBLIC_HOST


@pytest.mark.unit
def test_trailing_dot_host_normalized(monkeypatch):
    """RFC 1035 root label (trailing .) should be stripped before allowlist match.

    D3 fix: `https://example.com./cb` host is "example.com."
    which would mismatch the allowlist "example.com" without this fix.
    """
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setenv(
        "CALLBACK_ALLOWED_HOSTS", _PUBLIC_HOST,
    )
    parsed = validate_callback_url(f"https://{_PUBLIC_HOST}./cb")
    assert parsed.host == _PUBLIC_HOST  # stripped


@pytest.mark.unit
def test_documents_dns_rebinding_known_risk():
    """Caller must resolve and use IP for actual connection; this helper
    checks at validation time only. The module docstring calls this out
    as a known P3 limitation.
    """
    import inspect
    from ekrs_rag.security import callback_url
    src = inspect.getsource(callback_url)
    assert "DNS rebinding" in src, "module must document DNS rebinding limitation"
