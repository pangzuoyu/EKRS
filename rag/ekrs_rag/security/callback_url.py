"""Callback URL allowlist with SSRF mitigation.

Validation gates (in order):
  1. Scheme allowlist (env: CALLBACK_ALLOWED_SCHEMES; default `{"https"}`).
  2. IP literal rejection (IPv4 + IPv6).
  3. DNS resolution: any resolved address in
     private / loopback / link-local / multicast / reserved / unspecified
     causes the URL to be blocked.
  4. Host allowlist (env: CALLBACK_ALLOWED_HOSTS; unset = no host filter,
     `*` = wildcard; otherwise exact-match comparison on lowercased host).

KNOWN LIMITATION — DNS rebinding (P3):
  Validation resolves DNS at call time. The subsequent HTTP call (in
  callers, e.g. T5 parser_token.py / T6 caller code) must re-resolve
  the host and use the resulting IP to make the connection, OR
  re-validate immediately before connect, to close the rebind window.
  This helper is best-effort at validation time; the caller owns the
  connect-time check.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit


class CallbackURLBlockedError(ValueError):
    """Raised when a callback URL fails allowlist checks."""


@dataclass(frozen=True)
class ParsedCallback:
    scheme: str
    host: str
    port: int | None
    raw: str


def _allowed_schemes() -> frozenset[str]:
    raw = os.environ.get("CALLBACK_ALLOWED_SCHEMES", "https")
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


def _resolve_is_dangerous(host: str) -> tuple[bool, str]:
    """Resolve host and return (is_dangerous, reason)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return True, "dns_unresolvable"
    for family, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True, f"resolved_to_{ip}"
    return False, ""


def _allowed_hosts() -> tuple[frozenset[str], bool]:
    """Read CALLBACK_ALLOWED_HOSTS env var.

    Returns (allowed_hosts, wildcard_enabled). When wildcard is enabled,
    no host-level filter applies (allowed_hosts is irrelevant). When the
    var is unset, allowed_hosts is empty and wildcard is False — which
    the caller treats as "no host filter".
    """
    raw = os.environ.get("CALLBACK_ALLOWED_HOSTS", "").strip()
    if not raw:
        return frozenset(), False
    parts = frozenset(h.strip().lower() for h in raw.split(",") if h.strip())
    if "*" in parts:
        return parts, True
    return parts, False


def validate_callback_url(
    url: str,
    allowed_schemes: Iterable[str] | None = None,
) -> ParsedCallback:
    """Validate a callback URL against the SSRF mitigation allowlist.

    Raises CallbackURLBlockedError on any failed gate. Returns a
    ParsedCallback snapshot on success.
    """
    if not url:
        raise CallbackURLBlockedError("empty url")

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    schemes = frozenset(s.lower() for s in (allowed_schemes or _allowed_schemes()))
    if scheme not in schemes:
        raise CallbackURLBlockedError(
            f"scheme '{scheme}' not in {sorted(schemes)}"
        )

    host = parts.hostname.rstrip(".") if parts.hostname else ""
    # Strip trailing-dot (RFC 1035 root label) so DNS allows normalization
    # matches allowlist without parser-side cooperation.
    if not host:
        raise CallbackURLBlockedError("missing host")

    # Reject IP literals explicitly
    try:
        ip = ipaddress.ip_address(host)
        raise CallbackURLBlockedError(f"ip literal rejected: {ip}")
    except ValueError:
        pass  # not an IP literal — proceed to DNS resolution

    dangerous, reason = _resolve_is_dangerous(host)
    if dangerous:
        raise CallbackURLBlockedError(f"host {host} blocked: {reason}")

    # Optional host allowlist (CALLBACK_ALLOWED_HOSTS). When unset, no
    # host-level filter applies; when set and '*' included, any host passes.
    allowed_hosts, wildcard = _allowed_hosts()
    if allowed_hosts and not wildcard and host not in allowed_hosts:
        raise CallbackURLBlockedError(
            f"host {host} not in allowlist {sorted(allowed_hosts)}"
        )

    return ParsedCallback(
        scheme=scheme,
        host=host,
        port=parts.port,
        raw=url,
    )
