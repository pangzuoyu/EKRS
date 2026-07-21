# EKRS 集成对接修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four EKRS-side gaps blocking real round-trip with doc-to-md: (P0.1) callback auth + URL allowlist + 4xx retry filter; (P0.2) `SHARED_STORAGE_PATH` boundary enforcement; (P1.1) `pipeline.ingest()` state machine + doc cleanup; (P2) safe old-version deletion.

**Architecture:** Layered defense — config-validated startup → route-layer HTTP gate → pipeline-layer depth defense. State machine replaced by explicit `IngestionOutcome` dataclass instead of exception-based signaling. SSRF protection via `urllib.parse.urlsplit` + DNS resolution + private/loopback/link-local IP rejection. Old-version deletion reuses existing per-doc Redis lock and Qdrant `Range(lt=)` filter.

**Tech Stack:** Python 3.11+, FastAPI 0.115, Pydantic 2.8, pydantic-settings 2.0, httpx, tenacity, redis (aioredis), SQLite (aiosqlite), pytest, pytest-asyncio, bandit.

**Codebase baseline:** `master` @ `97a7c63`.

---

## Global Constraints

- Python 3.11+ (PEP 604 `X | None` syntax already used; `str | None` permitted)
- Type annotations on every public function signature (existing convention; enforce)
- Pydantic v2 models for shared IR (`shared/ekrs_shared/models.py`)
- pydantic-settings for env vars (`rag/ekrs_rag/core/config.py:BaseSettings`)
- All logs via `python-json-logger` (existing convention, see `core/logging.py`)
- Test layout: `rag/tests/unit/` (no external services) + `rag/tests/integration/` (FastAPI TestClient + fakeredis)
- Commit message format: `<type>: <description>` (no attribution; matches `common/git-workflow.md`)
- New files must not introduce new top-level dependencies; reuse `httpx`, `tenacity`, `pyyaml`, `pydantic`, `pydantic-settings`, `redis`
- HTTP callback safety: never log `PARSER_TOKEN` value; never include it in tracebacks or audit event payloads
- Test markers: `@pytest.mark.unit` for unit, `@pytest.mark.integration` for integration

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `rag/ekrs_rag/security/__init__.py` | Public exports for security helpers |
| `rag/ekrs_rag/security/callback_url.py` | `validate_callback_url()`, `CallbackURLBlockedError`, `ParsedCallback` |
| `rag/ekrs_rag/security/parser_token.py` | `build_callback_headers()`, `CallbackAuthMissingError`, `safe_compare()` |
| `rag/ekrs_rag/ingestion/outcome.py` | `IngestionOutcome` frozen dataclass |
| `rag/tests/unit/test_callback_url.py` | URL allowlist unit tests |
| `rag/tests/unit/test_parser_token.py` | Token helper unit tests |
| `rag/tests/unit/test_callback_security.py` | `_send_callback` retry + header unit tests |
| `rag/tests/unit/test_outcome.py` | `IngestionOutcome` dataclass tests |

### Modified files

| Path | Touched by tasks |
|---|---|
| `rag/ekrs_rag/core/config.py` | T1, T3, T6 |
| `rag/ekrs_rag/main.py` (lifespan) | T1, T3 |
| `rag/ekrs_rag/api/routes/ingestion.py` | T2, T6, T7, T10 |
| `rag/ekrs_rag/ingestion/pipeline.py` | T2, T6, T7, T9, T12 |
| `rag/ekrs_rag/retrieval/qdrant_client.py` | T11 |
| `rag/tests/unit/test_qdrant_client.py` | T11 |
| `rag/tests/integration/test_ingestion.py` | T2, T6, T7, T9, T10, T12 |
| `rag/tests/integration/test_ingestion_phase4.py` | T10 |
| `rag/tests/integration/test_ingestion_replay.py` | T2 |
| `EKRS-RAG-AI_intergration.md` | T13 |
| `docs/USAGE.md` | T13 |
| `CHANGELOG.md` | T13 |
| `.env.example` | T6, T7 |

### Files NOT touched

- `shared/ekrs_shared/models.py` — `IngestionNotification` schema unchanged
- `rag/ekrs_rag/concurrency/redis_lock.py` — reuse as-is
- `rag/ekrs_rag/storage/task_repo.py` — reuse `mark_failed_with_error` as-is

---

### Task 1: `SHARED_STORAGE_PATH` config-validator + lifespan startup check

**Files:**
- Modify: `rag/ekrs_rag/core/config.py:67-72` (add new `field_validator`)
- Modify: `rag/ekrs_rag/main.py:154-156` (add lifespan check)
- Modify: `.env.example` (clarify comment)
- Test: existing `tests/integration/test_ingestion.py` (already exercises `SHARED_STORAGE_PATH` via `settings`)

**Interfaces:**
- Consumes: existing `Settings.SHARED_STORAGE_PATH: Path`
- Produces: `Settings` rejects relative paths via `ValueError`; lifespan raises `RuntimeError` if root missing

- [ ] **Step 1: Write failing test for config validator**

Add to a new file `rag/tests/unit/test_config.py` (the file may not exist; create it):

```python
import pytest
from pydantic import ValidationError

from ekrs_rag.core.config import Settings


def test_shared_storage_path_must_be_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_STORAGE_PATH", "relative/parsed")
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "SHARED_STORAGE_PATH must be an absolute path" in str(exc_info.value)


def test_shared_storage_path_absolute_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    s = Settings()
    assert s.SHARED_STORAGE_PATH == tmp_path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ekrs_rag.core.config'` if path wrong, or with `ImportError` if pytest can't import. The relative-path assertion should fail because the validator does not exist yet.

- [ ] **Step 3: Implement the validator in `core/config.py`**

Add a second `field_validator` after the existing `token_min_length` (line 67-72):

```python
    @field_validator("SHARED_STORAGE_PATH")
    @classmethod
    def storage_must_be_absolute(cls, v: Path) -> Path:
        if not v.is_absolute():
            raise ValueError("SHARED_STORAGE_PATH must be an absolute path")
        return v
```

- [ ] **Step 4: Write failing test for lifespan startup check**

Add to `rag/tests/unit/test_config.py`:

```python
import pytest
from pathlib import Path


def test_lifespan_rejects_missing_storage_path(monkeypatch, tmp_path):
    """Settings allows non-existent absolute path; lifespan must reject."""
    monkeypatch.setenv("SHARED_STORAGE_PATH", "/nonexistent/parsed_lib_xyz")
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    s = Settings()
    # Validator passes (absolute), but the dir doesn't exist
    assert s.SHARED_STORAGE_PATH == Path("/nonexistent/parsed_lib_xyz")
    assert not s.SHARED_STORAGE_PATH.is_dir()
```

This test exercises the validator only; the lifespan check itself is covered in T2 step test.

- [ ] **Step 5: Implement lifespan startup check in `main.py:154-156`**

After the `logger.info("Starting EKRS RAG service (debug=...)")` line, insert:

```python
        storage_root = settings.SHARED_STORAGE_PATH
        if not storage_root.is_dir():
            raise RuntimeError(
                f"SHARED_STORAGE_PATH={storage_root} does not exist; "
                "create the directory or fix the config before starting."
            )
        app.state.shared_storage_root = storage_root.resolve()
```

- [ ] **Step 6: Run all tests for this task**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_config.py tests/integration/test_ingestion.py -v`
Expected: PASS. If any integration test relied on a missing default root, it will surface here — fix by ensuring fixtures set `SHARED_STORAGE_PATH` to a tmpdir.

- [ ] **Step 7: Update `.env.example` comment for `SHARED_STORAGE_PATH`**

Change the existing comment line:

```
SHARED_STORAGE_PATH=/parsed_lib
```

to:

```bash
# Shared storage root (parser writes JSONL here, RAG reads).
# MUST be an absolute path AND must exist at startup; service refuses to boot otherwise.
SHARED_STORAGE_PATH=/parsed_lib
```

- [ ] **Step 8: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/core/config.py rag/ekrs_rag/main.py rag/tests/unit/test_config.py .env.example
git commit -m "feat(config): fail-fast on missing or relative SHARED_STORAGE_PATH"
```

---

### Task 2: `SHARED_STORAGE_PATH` path-boundary enforcement (route + pipeline)

**Files:**
- Modify: `rag/ekrs_rag/api/routes/ingestion.py:62-90` (add path check before lock acquire)
- Modify: `rag/ekrs_rag/ingestion/pipeline.py:26-28,30-46` (`__init__` accepts `_shared_storage_root`; `ingest()` rejects out-of-root `output_path`)
- Modify: `rag/ekrs_rag/main.py:199` (pass resolved root into `IngestionPipeline`)
- Test: `rag/tests/integration/test_ingestion.py`

**Interfaces:**
- Consumes: `app.state.shared_storage_root: Path` (from T1)
- Produces: HTTP 400 on out-of-root `output_path`; pipeline raises `ValueError` on same condition

- [ ] **Step 1: Write failing integration test for out-of-root output_path**

Append to `rag/tests/integration/test_ingestion.py`:

```python
def test_notify_rejects_output_path_outside_storage_root(client, tmp_path):
    """An output_path that escapes SHARED_STORAGE_PATH must 400."""
    outside = tmp_path.parent / "evil.txt"
    resp = client.post(
        "/v1/ingestion/notify",
        headers={"X-Parser-Token": PARSER_TOKEN},
        json={
            "doc_hash": "abc123",
            "version": 1,
            "output_path": str(outside),
            "callback_url": "",
        },
    )
    assert resp.status_code == 400
    assert "SHARED_STORAGE_PATH" in resp.json()["detail"]


def test_notify_rejects_relative_traversal(client, tmp_path):
    """output_path with .. segments must 400."""
    base = tmp_path.resolve()
    rel = f"{base}/../../../etc"
    resp = client.post(
        "/v1/ingestion/notify",
        headers={"X-Parser-Token": PARSER_TOKEN},
        json={
            "doc_hash": "abc123",
            "version": 1,
            "output_path": rel,
            "callback_url": "",
        },
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/integration/test_ingestion.py -v -k "outside_storage_root or relative_traversal"`
Expected: FAIL — current route accepts any path.

- [ ] **Step 3: Implement route-level check**

In `rag/ekrs_rag/api/routes/ingestion.py`, modify `notify()` to add a check after `notification` parameter binding, before lock acquire:

```python
    doc_hash = notification.doc_hash
    version = notification.version
    request_id = request_id_from_trace(
        notification.trace_id or "", doc_hash, version
    )

    # P0.2: reject output_path that escapes SHARED_STORAGE_PATH
    storage_root: Path = request.app.state.shared_storage_root
    try:
        candidate = Path(notification.output_path).resolve(strict=False)
        candidate.relative_to(storage_root)
    except (ValueError, OSError):
        raise HTTPException(
            status_code=400,
            detail="output_path must be an absolute subdirectory of SHARED_STORAGE_PATH",
        )
```

Add at top of file (if not present):

```python
from pathlib import Path
from fastapi import HTTPException
```

- [ ] **Step 4: Run tests; expect route tests to pass**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/integration/test_ingestion.py -v -k "outside_storage_root or relative_traversal"`
Expected: PASS.

- [ ] **Step 5: Write failing pipeline-level test**

Append to `rag/tests/unit/test_pipeline_path.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from ekrs_rag.ingestion.pipeline import IngestionPipeline


@pytest.mark.asyncio
async def test_pipeline_ingest_rejects_output_outside_root(tmp_path):
    storage_root = tmp_path / "root"
    storage_root.mkdir()
    pipeline = IngestionPipeline(
        qdrant=MagicMock(),
        storage_path=storage_root,
        parser_token="x" * 32,
    )
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.output_path = str(outside)
    notification.callback_url = ""

    with pytest.raises(ValueError, match="SHARED_STORAGE_PATH"):
        await pipeline.ingest(notification)
```

- [ ] **Step 6: Update `IngestionPipeline.__init__` and `ingest()` to accept and enforce the root**

Modify `rag/ekrs_rag/ingestion/pipeline.py:26-28`:

```python
    def __init__(
        self,
        qdrant,
        storage_path: Path,
        parser_token: str,
    ) -> None:
        self._qdrant = qdrant
        self._shared_storage_root = Path(storage_path).resolve()
        self._parser_token = parser_token
```

Modify `ingest()` at line 30-46 — after `output_path = Path(notification.output_path)` (line 43), insert:

```python
        # P0.2: defense-in-depth check (route already enforces this; pipeline re-checks)
        try:
            output_path.resolve(strict=False).relative_to(self._shared_storage_root)
        except (ValueError, OSError) as e:
            logger.error(
                "output_path_out_of_scope: doc=%s v=%d path=%s root=%s",
                doc_hash, version, output_path, self._shared_storage_root,
            )
            raise ValueError(
                f"output_path {output_path} is outside SHARED_STORAGE_PATH "
                f"root {self._shared_storage_root}"
            ) from e
```

- [ ] **Step 7: Wire `parser_token` and `storage_path` in `main.py:199`**

Modify the lifespan line that constructs `IngestionPipeline`:

```python
        _pipeline = IngestionPipeline(
            _qdrant,
            storage_path=app.state.shared_storage_root,
            parser_token=settings.PARSER_TOKEN,
        )
```

- [ ] **Step 8: Update existing `IngestionPipeline` call sites in tests**

Search for `IngestionPipeline(` and update each call to add `parser_token="x" * 32`. Files most likely affected:
- `rag/tests/integration/test_ingestion.py`
- `rag/tests/integration/test_ingestion_replay.py`
- `rag/tests/integration/test_ingestion_phase4.py`

If any existing test instantiates `IngestionPipeline(mock, settings.SHARED_STORAGE_PATH)` (2-arg), add `parser_token="x"*32` as third arg.

- [ ] **Step 9: Run unit + integration tests for this task**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_pipeline_path.py tests/integration/test_ingestion.py -v`
Expected: PASS. If existing tests break due to `parser_token` arg, fix in Step 8.

- [ ] **Step 10: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/api/routes/ingestion.py rag/ekrs_rag/ingestion/pipeline.py rag/ekrs_rag/main.py rag/tests/
git commit -m "feat(security): enforce SHARED_STORAGE_PATH boundary on output_path"
```

---

### Task 3: `PARSER_TOKEN` startup fail-fast (already 32-char, reject placeholder)

**Files:**
- Modify: `rag/ekrs_rag/core/config.py:67-72` (extend validator)
- Modify: `rag/ekrs_rag/main.py` lifespan (add token presence check after T1 block)
- Test: `rag/tests/unit/test_config.py`

**Interfaces:**
- Consumes: `Settings.PARSER_TOKEN`
- Produces: `Settings` raises if token is the example default or empty

- [ ] **Step 1: Write failing test for placeholder rejection**

Append to `rag/tests/unit/test_config.py`:

```python
def test_parser_token_rejects_default_placeholder(monkeypatch):
    monkeypatch.setenv(
        "SHARED_STORAGE_PATH", "/tmp"
    )
    # The default literal in Settings is the placeholder
    monkeypatch.delenv("PARSER_TOKEN", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "PARSER_TOKEN" in str(exc_info.value)


def test_parser_token_rejects_empty(monkeypatch):
    monkeypatch.setenv("SHARED_STORAGE_PATH", "/tmp")
    monkeypatch.setenv("PARSER_TOKEN", "")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "PARSER_TOKEN" in str(exc_info.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_config.py -v -k "placeholder or empty"`
Expected: FAIL — current validator only checks length.

- [ ] **Step 3: Extend the `token_min_length` validator**

Replace `core/config.py:67-72`:

```python
    @field_validator("PARSER_TOKEN")
    @classmethod
    def token_min_length(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "PARSER_TOKEN is empty; set a 32+ character secret in .env"
            )
        if v == "change-me-to-a-secure-random-string-32chars":
            raise ValueError(
                "PARSER_TOKEN is the example default; replace with a real secret"
            )
        if len(v) < 32:
            raise ValueError("PARSER_TOKEN must be >= 32 characters")
        return v
```

- [ ] **Step 4: Add lifespan startup assertion (defense in depth)**

After the `SHARED_STORAGE_PATH` block added in T1, add:

```python
        if not settings.PARSER_TOKEN or len(settings.PARSER_TOKEN) < 32:
            raise RuntimeError(
                "PARSER_TOKEN is missing or shorter than 32 chars; "
                "set PARSER_TOKEN in .env before starting."
            )
```

- [ ] **Step 5: Run all config + ingestion tests**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_config.py tests/integration/test_ingestion.py tests/integration/test_ingestion_phase4.py -v`
Expected: PASS. Tests that set `PARSER_TOKEN=""` (e.g., `test_ingestion_replay.py:46`) will break — update them to use `os.environ.setdefault("PARSER_TOKEN", "x"*32)` instead.

- [ ] **Step 6: Update tests that disable auth via empty PARSER_TOKEN**

Search: `rg "PARSER_TOKEN.*=\s*['\"]" rag/tests/`
For each match where value is `""`, change to `"x" * 32` if the test depends on auth-disabled behavior, OR keep `""` and add `@pytest.mark.skip(reason="requires auth-disabled mode")` if the empty-token behavior is what's being tested.

- [ ] **Step 7: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/core/config.py rag/ekrs_rag/main.py rag/tests/
git commit -m "feat(config): reject empty and default PARSER_TOKEN at startup"
```

---

### Task 4: Callback URL validation helper

**Files:**
- Create: `rag/ekrs_rag/security/__init__.py`
- Create: `rag/ekrs_rag/security/callback_url.py`
- Create: `rag/tests/unit/test_callback_url.py`
- Modify: `rag/ekrs_rag/core/config.py` (add `CALLBACK_ALLOWED_SCHEMES`)

**Interfaces:**
- Consumes: `settings.CALLBACK_ALLOWED_SCHEMES: set[str]` (default `{"https"}`)
- Produces: `validate_callback_url(url) -> ParsedCallback`; raises `CallbackURLBlockedError`

- [ ] **Step 1: Write failing tests**

Create `rag/tests/unit/test_callback_url.py`:

```python
import pytest

from ekrs_rag.security.callback_url import (
    CallbackURLBlockedError,
    validate_callback_url,
)


def test_allows_https_public_domain(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    parsed = validate_callback_url("https://parser.example.com/cb")
    assert parsed.scheme == "https"
    assert parsed.host == "parser.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/cb",
        "http://[::1]/cb",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/cb",
        "http://192.168.1.1/cb",
        "ftp://parser.example.com/cb",
        "file:///etc/passwd",
        "gopher://parser.example.com/",
    ],
)
def test_blocks_dangerous_urls(monkeypatch, url):
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https,http")
    with pytest.raises(CallbackURLBlockedError):
        validate_callback_url(url)


def test_dns_resolution_to_private_ip_blocks(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https,http")
    # localhost typically resolves to 127.0.0.1
    with pytest.raises(CallbackURLBlockedError):
        validate_callback_url("http://localhost:9001/cb")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_callback_url.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Add `CALLBACK_ALLOWED_SCHEMES` to config**

Append to `core/config.py` Settings (after `OLD_VERSION_DELETE_ENABLED` if you pre-add; otherwise after `LOCK_TTL_SEC`):

```python
    # Callback security
    CALLBACK_ALLOWED_SCHEMES: str = "https"  # comma-separated
```

Add a validator:

```python
    @field_validator("CALLBACK_ALLOWED_SCHEMES")
    @classmethod
    def parse_callback_schemes(cls, v: str) -> frozenset[str]:
        schemes = frozenset(s.strip().lower() for s in v.split(",") if s.strip())
        if not schemes:
            raise ValueError("CALLBACK_ALLOWED_SCHEMES must contain at least one scheme")
        if not schemes.issubset({"http", "https"}):
            raise ValueError("CALLBACK_ALLOWED_SCHEMES only supports http and https")
        return schemes
```

Wait — `field_validator` should return the parsed type used by Pydantic. Since `CALLBACK_ALLOWED_SCHEMES` is declared as `str`, the validator must return `str`. Restructure:

```python
    CALLBACK_ALLOWED_SCHEMES: str = "https"

    @field_validator("CALLBACK_ALLOWED_SCHEMES")
    @classmethod
    def validate_callback_schemes(cls, v: str) -> str:
        schemes = {s.strip().lower() for s in v.split(",") if s.strip()}
        if not schemes:
            raise ValueError("CALLBACK_ALLOWED_SCHEMES must contain at least one scheme")
        if not schemes.issubset({"http", "https"}):
            raise ValueError("CALLBACK_ALLOWED_SCHEMES only supports http and https")
        return v
```

Use the env value directly inside `validate_callback_url` via `os.environ.get("CALLBACK_ALLOWED_SCHEMES", "https")` — or add a helper `get_callback_allowed_schemes()` in the security module.

- [ ] **Step 4: Create `rag/ekrs_rag/security/__init__.py`**

Empty file with docstring:

```python
"""Security helpers for outbound callbacks and token handling."""
```

- [ ] **Step 5: Implement `rag/ekrs_rag/security/callback_url.py`**

```python
"""Callback URL allowlist with SSRF mitigation.

- Rejects non-allowlisted schemes.
- Rejects IP literals (IPv4 + IPv6).
- Resolves DNS and rejects any address that is private, loopback,
  link-local, multicast, or reserved.
"""
from __future__ import annotations

import ipaddress
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
    import os
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


def validate_callback_url(url: str, allowed_schemes: Iterable[str] | None = None) -> ParsedCallback:
    if not url:
        raise CallbackURLBlockedError("empty url")

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    schemes = frozenset(s.lower() for s in (allowed_schemes or _allowed_schemes()))
    if scheme not in schemes:
        raise CallbackURLBlockedError(
            f"scheme '{scheme}' not in {sorted(schemes)}"
        )

    host = parts.hostname  # already lowercased; strips brackets for IPv6
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

    return ParsedCallback(
        scheme=scheme,
        host=host,
        port=parts.port,
        raw=url,
    )
```

- [ ] **Step 6: Run tests; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_callback_url.py -v`
Expected: PASS.

- [ ] **Step 7: Add a quick smoke test that DNS-rebinding note is documented**

Append a single test asserting the helper does not pin DNS results (mark as known limitation):

```python
def test_documents_dns_rebinding_known_risk():
    # Caller must resolve and use IP for actual connection; this helper
    # checks at validation time only.
    import inspect
    from ekrs_rag.security import callback_url
    src = inspect.getsource(callback_url)
    assert "TODO(P3)" in src or "DNS rebinding" in src or "DNS" in src
```

(Loosen to just `assert "socket" in src` if the comment note is removed.)

- [ ] **Step 8: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/security/ rag/tests/unit/test_callback_url.py rag/ekrs_rag/core/config.py
git commit -m "feat(security): callback URL allowlist with SSRF mitigation"
```

---

### Task 5: Parser token helper (`build_callback_headers`)

**Files:**
- Create: `rag/ekrs_rag/security/parser_token.py`
- Modify: `rag/ekrs_rag/security/__init__.py` (export)
- Create: `rag/tests/unit/test_parser_token.py`

**Interfaces:**
- Consumes: `settings.PARSER_TOKEN`
- Produces: `build_callback_headers() -> dict[str, str]` with `X-Parser-Token` set

- [ ] **Step 1: Write failing tests**

Create `rag/tests/unit/test_parser_token.py`:

```python
import pytest

from ekrs_rag.security.parser_token import (
    CallbackAuthMissingError,
    build_callback_headers,
    safe_compare,
)


def test_safe_compare_equal_returns_true():
    assert safe_compare("a" * 32, "a" * 32) is True


def test_safe_compare_different_length_returns_false():
    assert safe_compare("a" * 31, "a" * 32) is False


def test_safe_compare_equal_length_different_value_returns_false():
    assert safe_compare("a" * 32, "b" * 32) is False


def test_safe_compare_empty_inputs():
    assert safe_compare("", "") is True  # both empty is technically equal
    assert safe_compare("", "a") is False
    assert safe_compare("a", "") is False


def test_build_callback_headers_returns_token(monkeypatch):
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    headers = build_callback_headers()
    assert headers["X-Parser-Token"] == "x" * 32
    assert "X-EKRS-Version" in headers


def test_build_callback_headers_raises_on_empty(monkeypatch):
    monkeypatch.setenv("PARSER_TOKEN", "")
    with pytest.raises(CallbackAuthMissingError):
        build_callback_headers()


def test_build_callback_headers_raises_on_short(monkeypatch):
    monkeypatch.setenv("PARSER_TOKEN", "short")
    with pytest.raises(CallbackAuthMissingError):
        build_callback_headers()
```

- [ ] **Step 2: Run tests; verify fail**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_parser_token.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `rag/ekrs_rag/security/parser_token.py`**

```python
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
```

- [ ] **Step 4: Update `rag/ekrs_rag/security/__init__.py`**

```python
"""Security helpers for outbound callbacks and token handling."""

from ekrs_rag.security.callback_url import (
    CallbackURLBlockedError,
    ParsedCallback,
    validate_callback_url,
)
from ekrs_rag.security.parser_token import (
    CallbackAuthMissingError,
    build_callback_headers,
    safe_compare,
)

__all__ = [
    "CallbackURLBlockedError",
    "CallbackAuthMissingError",
    "ParsedCallback",
    "build_callback_headers",
    "safe_compare",
    "validate_callback_url",
]
```

- [ ] **Step 5: Run tests; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_parser_token.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/security/ rag/tests/unit/test_parser_token.py
git commit -m "feat(security): parser token helper with timing-safe compare"
```

---

### Task 6: Wire `X-Parser-Token` into `_send_callback`

**Files:**
- Modify: `rag/ekrs_rag/ingestion/pipeline.py:130-163` (call `build_callback_headers` + send headers)
- Modify: `rag/ekrs_rag/core/config.py` (add `PIPELINE_CALLBACK_TIMEOUT_SEC`)
- Test: `rag/tests/unit/test_callback_security.py` (new — happy path only; retry in T7)

**Interfaces:**
- Consumes: `validate_callback_url`, `build_callback_headers`
- Produces: `_send_callback` issues `client.post(url, json=payload, headers=headers)` with `X-Parser-Token`

- [ ] **Step 1: Write failing test**

Create `rag/tests/unit/test_callback_security.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ekrs_rag.ingestion.pipeline import IngestionPipeline
from ekrs_rag.security.callback_url import ParsedCallback


@pytest.mark.asyncio
async def test_send_callback_includes_x_parser_token(monkeypatch, tmp_path):
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")

    pipeline = IngestionPipeline(
        qdrant=MagicMock(),
        storage_path=tmp_path,
        parser_token="x" * 32,
    )

    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
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
```

- [ ] **Step 2: Run test; verify fail**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_callback_security.py -v -k "x_parser_token"`
Expected: FAIL — current `_send_callback` sends no headers.

- [ ] **Step 3: Add `PIPELINE_CALLBACK_TIMEOUT_SEC` to config**

Append to `core/config.py`:

```python
    # Pipeline / callback tuning
    PIPELINE_CALLBACK_MAX_ATTEMPTS: int = 3
    PIPELINE_RETRY_MIN_SEC: float = 2.0
    PIPELINE_RETRY_MAX_SEC: float = 10.0
    PIPELINE_CALLBACK_TIMEOUT_SEC: float = 30.0
    OLD_VERSION_DELETE_ENABLED: bool = True
```

- [ ] **Step 4: Rewrite `_send_callback` body (keep retry decorator from T7)**

Replace `rag/ekrs_rag/ingestion/pipeline.py:130-163`:

```python
    @retry(
        reraise=True,
        retry=retry_if_exception_type(CallbackRetryableError),
        stop=stop_after_attempt(settings.PIPELINE_CALLBACK_MAX_ATTEMPTS),
        wait=wait_exponential(
            min=settings.PIPELINE_RETRY_MIN_SEC,
            max=settings.PIPELINE_RETRY_MAX_SEC,
        ),
    )
    async def _send_callback(
        self,
        notification: IngestionNotification,
        rag_status: str,
        error: str | None = None,
    ) -> None:
        """Send callback to parser with ingestion result."""
        if not notification.callback_url:
            logger.warning(
                "No callback_url, skipping callback for %s",
                notification.doc_hash,
            )
            return

        try:
            parsed = validate_callback_url(notification.callback_url)
        except CallbackURLBlockedError as e:
            logger.warning(
                "callback_url_blocked: doc=%s reason=%s",
                notification.doc_hash, e,
            )
            return  # best-effort; don't block ingestion

        try:
            headers = build_callback_headers()
        except CallbackAuthMissingError as e:
            logger.error("callback_auth_missing: %s", e)
            return

        payload = {
            "doc_hash": notification.doc_hash,
            "version": notification.version,
            "rag_status": rag_status,
            "trace_id": notification.trace_id,
        }
        if error:
            payload["error"] = error

        try:
            async with httpx.AsyncClient(timeout=settings.PIPELINE_CALLBACK_TIMEOUT_SEC) as client:
                resp = await client.post(parsed.raw, json=payload, headers=headers)
                if 400 <= resp.status_code < 500:
                    raise CallbackNonRetryable(resp.status_code)
                resp.raise_for_status()
                logger.info(
                    "Callback sent: doc=%s status=%s",
                    notification.doc_hash, rag_status,
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            raise CallbackRetryableError(str(e)) from e
        except httpx.HTTPStatusError as e:
            if 400 <= e.response.status_code < 500:
                raise CallbackNonRetryable(e.response.status_code) from e
            raise CallbackRetryableError(str(e)) from e
```

Add exception types at top of `pipeline.py`:

```python
class CallbackRetryableError(Exception):
    """Network or 5xx error — should be retried."""


class CallbackNonRetryableError(Exception):
    """4xx error — should NOT be retried."""
```

Add imports at top:

```python
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ekrs_rag.core.config import settings
from ekrs_rag.security import (
    CallbackAuthMissingError,
    CallbackURLBlockedError,
    build_callback_headers,
    validate_callback_url,
)
```

- [ ] **Step 5: Run test; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_callback_security.py -v -k "x_parser_token"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/ingestion/pipeline.py rag/ekrs_rag/core/config.py rag/tests/unit/test_callback_security.py
git commit -m "feat(callback): send X-Parser-Token with URL-validated callback"
```

---

### Task 7: 4xx-not-retryable in `_send_callback`

**Files:**
- Modify: `rag/ekrs_rag/ingestion/pipeline.py` (already covered by T6 retry decorator; add tests here)
- Test: extend `rag/tests/unit/test_callback_security.py`

**Interfaces:**
- Consumes: `CallbackNonRetryableError` defined in T6
- Produces: 4xx response → exactly 1 POST; 5xx → exactly 3 POSTs

- [ ] **Step 1: Write failing tests for retry behavior**

Append to `rag/tests/unit/test_callback_security.py`:

```python
class CountingClient:
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
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MIN_SEC", 0.01)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MAX_SEC", 0.02)

    pipeline = IngestionPipeline(qdrant=MagicMock(), storage_path=tmp_path, parser_token="x" * 32)
    client = CountingClient([403, 403, 403])
    _patch_client(monkeypatch, client)

    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.trace_id = "t"
    notification.callback_url = "https://parser.example.com/cb"

    await pipeline._send_callback(notification, "success")

    assert len(client.calls) == 1, f"4xx should not retry; got {len(client.calls)} calls"


@pytest.mark.asyncio
async def test_callback_retries_5xx(monkeypatch, tmp_path):
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MIN_SEC", 0.01)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MAX_SEC", 0.02)

    pipeline = IngestionPipeline(qdrant=MagicMock(), storage_path=tmp_path, parser_token="x" * 32)
    client = CountingClient([500, 500, 500])
    _patch_client(monkeypatch, client)

    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.trace_id = "t"
    notification.callback_url = "https://parser.example.com/cb"

    with pytest.raises(Exception):  # CallbackRetryableError after exhaustion
        await pipeline._send_callback(notification, "success")

    assert len(client.calls) == 3, f"5xx should retry 3 times; got {len(client.calls)} calls"


@pytest.mark.asyncio
async def test_callback_succeeds_after_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    monkeypatch.setenv("CALLBACK_ALLOWED_SCHEMES", "https")
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MIN_SEC", 0.01)
    monkeypatch.setattr("ekrs_rag.core.config.settings.PIPELINE_RETRY_MAX_SEC", 0.02)

    pipeline = IngestionPipeline(qdrant=MagicMock(), storage_path=tmp_path, parser_token="x" * 32)
    client = CountingClient([500, 502, 200])
    _patch_client(monkeypatch, client)

    notification = MagicMock()
    notification.doc_hash = "abc"
    notification.version = 1
    notification.trace_id = "t"
    notification.callback_url = "https://parser.example.com/cb"

    await pipeline._send_callback(notification, "success")

    assert len(client.calls) == 3  # retried twice then succeeded
```

- [ ] **Step 2: Run tests; expect FAIL on first (4xx retry)**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_callback_security.py -v`
Expected: 4xx test FAILS with 3 calls; 5xx and success tests may pass depending on implementation.

- [ ] **Step 3: Confirm the decorator from T6 already filters 4xx correctly**

The T6 decorator uses `retry=retry_if_exception_type(CallbackRetryableError)` and 4xx raises `CallbackNonRetryableError`. Verify by running the tests again. If 4xx test still retries, ensure the exception types are properly imported into `pipeline.py`.

- [ ] **Step 4: Run all callback_security tests; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_callback_security.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/tests/unit/test_callback_security.py
git commit -m "test(callback): cover 4xx no-retry, 5xx retry, success-after-retry"
```

---

### Task 8: `IngestionOutcome` frozen dataclass

**Files:**
- Create: `rag/ekrs_rag/ingestion/outcome.py`
- Test: `rag/tests/unit/test_outcome.py`

**Interfaces:**
- Produces: `IngestionOutcome(rag_status: Literal["success","failed"], error: str|None, error_code: str|None, chunks_indexed: int)`

- [ ] **Step 1: Write failing test**

Create `rag/tests/unit/test_outcome.py`:

```python
from dataclasses import FrozenInstanceError

import pytest

from ekrs_rag.ingestion.outcome import IngestionOutcome


def test_outcome_success_default_chunks_zero():
    o = IngestionOutcome(rag_status="success")
    assert o.rag_status == "success"
    assert o.error is None
    assert o.error_code is None
    assert o.chunks_indexed == 0


def test_outcome_failed_with_error_and_code():
    o = IngestionOutcome(
        rag_status="failed",
        error="JSONL not found",
        error_code="jsonl_missing",
    )
    assert o.rag_status == "failed"
    assert o.error == "JSONL not found"
    assert o.error_code == "jsonl_missing"


def test_outcome_is_immutable():
    o = IngestionOutcome(rag_status="success", chunks_indexed=5)
    with pytest.raises(FrozenInstanceError):
        o.rag_status = "failed"


def test_outcome_rejects_unknown_status():
    with pytest.raises(ValueError):
        IngestionOutcome(rag_status="bogus")
```

- [ ] **Step 2: Run tests; expect FAIL (module not found)**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_outcome.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `rag/ekrs_rag/ingestion/outcome.py`**

```python
"""Ingestion outcome dataclass.

Replaces exception-based signaling of business failures
(JSONL missing / IR parse error / Qdrant failure) so the route
wrapper can map the outcome to TaskRepo status directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


_RAG_STATUS = Literal["success", "failed"]


@dataclass(frozen=True)
class IngestionOutcome:
    rag_status: _RAG_STATUS
    error: str | None = None
    error_code: str | None = None
    chunks_indexed: int = 0

    def __post_init__(self) -> None:
        if self.rag_status not in ("success", "failed"):
            raise ValueError(
                f"IngestionOutcome.rag_status must be 'success' or 'failed'; "
                f"got {self.rag_status!r}"
            )
        if self.chunks_indexed < 0:
            raise ValueError(
                f"IngestionOutcome.chunks_indexed must be >= 0; got {self.chunks_indexed}"
            )
```

- [ ] **Step 4: Run tests; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_outcome.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/ingestion/outcome.py rag/tests/unit/test_outcome.py
git commit -m "feat(ingestion): IngestionOutcome frozen dataclass"
```

---

### Task 9: `pipeline.ingest()` returns `IngestionOutcome`

**Files:**
- Modify: `rag/ekrs_rag/ingestion/pipeline.py:30-127` (`ingest()` returns `IngestionOutcome`)
- Test: extend `rag/tests/integration/test_ingestion.py`

**Interfaces:**
- Consumes: `IngestionOutcome` from T8
- Produces: `ingest()` returns `IngestionOutcome`; callbacks still fire as before

- [ ] **Step 1: Write failing integration tests**

Append to `rag/tests/integration/test_ingestion.py`:

```python
def test_ingest_returns_outcome_on_success(client, tmp_path, monkeypatch):
    """Happy path returns success outcome."""
    # Existing fixture creates JSONL; this test reuses it
    ...
```

Or write a unit-style test in `rag/tests/unit/test_pipeline_outcome.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ekrs_rag.ingestion.outcome import IngestionOutcome
from ekrs_rag.ingestion.pipeline import IngestionPipeline


@pytest.mark.asyncio
async def test_ingest_returns_outcome_success(tmp_path):
    storage = tmp_path / "root"
    storage.mkdir()
    doc_dir = storage / "doc1" / "v1"
    doc_dir.mkdir(parents=True)
    (doc_dir / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text","content":"hello"}\n'
    )

    qdrant = MagicMock()
    qdrant.get_ingestion_status = MagicMock(return_value=None)
    qdrant.upsert_chunks = MagicMock(return_value=1)
    qdrant.delete_old_versions = MagicMock(return_value=0)

    pipeline = IngestionPipeline(
        qdrant=qdrant,
        storage_path=storage,
        parser_token="x" * 32,
    )
    notification = MagicMock()
    notification.doc_hash = "d1"
    notification.version = 1
    notification.output_path = str(doc_dir)
    notification.callback_url = ""  # skip callback

    outcome = await pipeline.ingest(notification)
    assert outcome.rag_status == "success"
    assert outcome.chunks_indexed == 1


@pytest.mark.asyncio
async def test_ingest_returns_outcome_failed_on_missing_jsonl(tmp_path):
    storage = tmp_path / "root"
    storage.mkdir()
    doc_dir = storage / "doc1" / "v1"
    doc_dir.mkdir(parents=True)
    # NO data.jsonl

    pipeline = IngestionPipeline(
        qdrant=MagicMock(),
        storage_path=storage,
        parser_token="x" * 32,
    )
    notification = MagicMock()
    notification.doc_hash = "d1"
    notification.version = 1
    notification.output_path = str(doc_dir)
    notification.callback_url = ""

    outcome = await pipeline.ingest(notification)
    assert outcome.rag_status == "failed"
    assert outcome.error_code == "jsonl_missing"
```

- [ ] **Step 2: Run tests; expect FAIL (ingest returns None today)**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_pipeline_outcome.py -v`
Expected: FAIL with `AttributeError: 'NoneType' object has no attribute 'rag_status'`.

- [ ] **Step 3: Refactor `pipeline.ingest()` to return `IngestionOutcome`**

Replace `pipeline.py:30-95` body so each branch returns an `IngestionOutcome` and `_send_callback` is still invoked before the return. Pattern:

```python
    async def ingest(self, notification: IngestionNotification) -> IngestionOutcome:
        doc_hash = notification.doc_hash
        version = notification.version
        output_path = Path(notification.output_path)

        # P0.2 boundary check
        try:
            output_path.resolve(strict=False).relative_to(self._shared_storage_root)
        except (ValueError, OSError) as e:
            logger.error(
                "output_path_out_of_scope: doc=%s v=%d path=%s root=%s",
                doc_hash, version, output_path, self._shared_storage_root,
            )
            outcome = IngestionOutcome(
                rag_status="failed",
                error=f"output_path outside SHARED_STORAGE_PATH: {output_path}",
                error_code="output_path_out_of_scope",
            )
            await self._send_callback(notification, outcome.rag_status, error=outcome.error)
            return outcome

        logger.info("Starting ingestion: doc=%s v=%d path=%s",
                    doc_hash, version, output_path)

        # Step 1: idempotency
        existing = self._qdrant.get_ingestion_status(doc_hash)
        if existing and existing.status == "success" and existing.version == version:
            outcome = IngestionOutcome(
                rag_status="success",
                chunks_indexed=existing.chunks_indexed,
            )
            await self._send_callback(notification, outcome.rag_status)
            return outcome

        # Step 2: JSONL missing
        jsonl_path = output_path / "data.jsonl"
        if not jsonl_path.exists():
            outcome = IngestionOutcome(
                rag_status="failed",
                error=f"File not found: {jsonl_path}",
                error_code="jsonl_missing",
            )
            await self._send_callback(notification, outcome.rag_status, error=outcome.error)
            return outcome

        # Step 3-4: parse + chunk
        try:
            blocks = parse_jsonl_file(str(jsonl_path))
            if not blocks:
                outcome = IngestionOutcome(
                    rag_status="failed", error="Empty JSONL file",
                    error_code="jsonl_empty",
                )
                await self._send_callback(notification, outcome.rag_status, error=outcome.error)
                return outcome
            chunks = chunk_blocks(blocks, doc_hash, version)
            if not chunks:
                outcome = IngestionOutcome(
                    rag_status="failed", error="No chunks produced",
                    error_code="no_chunks",
                )
                await self._send_callback(notification, outcome.rag_status, error=outcome.error)
                return outcome
        except IRParseError as e:
            outcome = IngestionOutcome(
                rag_status="failed", error=str(e),
                error_code="ir_parse_error",
            )
            await self._send_callback(notification, outcome.rag_status, error=outcome.error)
            return outcome

        # Step 5: upsert
        try:
            count = self._qdrant.upsert_chunks(chunks)
        except Exception as e:
            outcome = IngestionOutcome(
                rag_status="failed", error=str(e),
                error_code="qdrant_upsert_failed",
            )
            await self._send_callback(notification, outcome.rag_status, error=outcome.error)
            return outcome

        # Step 5.5: optional old-version cleanup (P2)
        if settings.OLD_VERSION_DELETE_ENABLED:
            try:
                self._qdrant.delete_old_versions(doc_hash, keep_version=version)
            except Exception as e:
                logger.warning(
                    "delete_old_versions_failed: doc=%s v=%d err=%s",
                    doc_hash, version, e,
                )

        # Step 6: success
        outcome = IngestionOutcome(rag_status="success", chunks_indexed=count)
        logger.info(
            "Ingested %d chunks for doc=%s v=%d", count, doc_hash, version,
        )
        await self._send_callback(notification, outcome.rag_status)
        return outcome
```

Add import at top:

```python
from ekrs_rag.ingestion.outcome import IngestionOutcome
```

Note: also wrapped the boundary check + idempotency check so all paths funnel through the same outcome return shape.

- [ ] **Step 4: Run new outcome tests; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_pipeline_outcome.py -v`
Expected: PASS.

- [ ] **Step 5: Run full integration suite (existing tests may need updates)**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/integration/ -v`
Expected: tests that called `await pipeline.ingest(...)` and ignored the return value still pass; tests that asserted on exception behavior may need updates (those are not yet written).

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/ingestion/pipeline.py rag/tests/unit/test_pipeline_outcome.py
git commit -m "refactor(ingestion): pipeline.ingest returns IngestionOutcome"
```

---

### Task 10: `_locked_ingest` uses outcome → TaskRepo status

**Files:**
- Modify: `rag/ekrs_rag/api/routes/ingestion.py:134-142`
- Test: extend `rag/tests/integration/test_ingestion_phase4.py`

**Interfaces:**
- Consumes: `IngestionOutcome` from T9
- Produces: `repo.mark_status(COMPLETED)` for success; `repo.mark_failed_with_error(...)` for failed

- [ ] **Step 1: Write failing tests**

Append to `rag/tests/integration/test_ingestion_phase4.py`:

```python
@pytest.mark.asyncio
async def test_locked_ingest_marks_completed_on_success_outcome(...):
    ...
```

Or, since `_locked_ingest` is nested inside `notify`, write a test that asserts through the public API:

```python
def test_notify_failure_path_marks_task_repo_failed(client, tmp_path, monkeypatch):
    """When pipeline.ingest returns failed outcome, TaskRepo status is FAILED."""
    # Use real TaskRepo + mocked pipeline.ingest returning a failed outcome.
    ...
```

A simpler approach: write a unit test that constructs `_locked_ingest` indirectly. Since `_locked_ingest` is a closure, refactor it slightly: extract into a top-level helper `_run_locked_ingest(pipeline, repo, lock, key, token, notification, request_id)`.

Replace the closure in `routes/ingestion.py`:

```python
    async def _locked_ingest() -> None:
        await _run_locked_ingest(
            pipeline=pipeline,
            repo=repo,
            lock=lock,
            lock_key=lock_key,
            lock_token=token,
            notification=notification,
            request_id=request_id,
        )

    background_tasks.add_task(_locked_ingest)
```

And add at module top:

```python
async def _run_locked_ingest(
    pipeline,
    repo,
    lock,
    lock_key: str,
    lock_token: str,
    notification,
    request_id: str,
) -> None:
    try:
        outcome = await pipeline.ingest(notification)
        if outcome.rag_status == "success":
            repo.mark_status(request_id, "COMPLETED")
        else:
            repo.mark_failed_with_error(request_id, outcome.error or "unknown")
    except Exception as e:
        repo.mark_failed_with_error(request_id, f"unhandled: {e}")
        raise
    finally:
        await lock.release(lock_key, lock_token)
```

- [ ] **Step 2: Write unit test for `_run_locked_ingest`**

Create `rag/tests/unit/test_run_locked_ingest.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from ekrs_rag.api.routes.ingestion import _run_locked_ingest
from ekrs_rag.ingestion.outcome import IngestionOutcome


@pytest.mark.asyncio
async def test_run_locked_ingest_marks_completed_on_success():
    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(return_value=IngestionOutcome(rag_status="success", chunks_indexed=5))
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()

    await _run_locked_ingest(
        pipeline=pipeline,
        repo=repo,
        lock=lock,
        lock_key="k",
        lock_token="t",
        notification=notification,
        request_id="r1",
    )

    repo.mark_status.assert_called_once_with("r1", "COMPLETED")
    repo.mark_failed_with_error.assert_not_called()
    lock.release.assert_called_once_with("k", "t")


@pytest.mark.asyncio
async def test_run_locked_ingest_marks_failed_on_failed_outcome():
    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(
        return_value=IngestionOutcome(
            rag_status="failed", error="JSONL missing", error_code="jsonl_missing",
        )
    )
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()

    await _run_locked_ingest(
        pipeline=pipeline,
        repo=repo,
        lock=lock,
        lock_key="k",
        lock_token="t",
        notification=notification,
        request_id="r1",
    )

    repo.mark_status.assert_not_called()
    repo.mark_failed_with_error.assert_called_once_with("r1", "JSONL missing")
    lock.release.assert_called_once_with("k", "t")


@pytest.mark.asyncio
async def test_run_locked_ingest_marks_failed_on_unhandled_exception():
    pipeline = MagicMock()
    pipeline.ingest = AsyncMock(side_effect=RuntimeError("boom"))
    repo = MagicMock()
    lock = MagicMock()
    lock.release = AsyncMock()
    notification = MagicMock()

    with pytest.raises(RuntimeError):
        await _run_locked_ingest(
            pipeline=pipeline, repo=repo, lock=lock,
            lock_key="k", lock_token="t", notification=notification, request_id="r1",
        )

    repo.mark_failed_with_error.assert_called_once()
    lock.release.assert_called_once_with("k", "t")
```

- [ ] **Step 3: Run tests; expect FAIL (helper doesn't exist)**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_run_locked_ingest.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Refactor `_locked_ingest` closure in `routes/ingestion.py`**

Follow Step 1's refactor exactly. Replace `async def _locked_ingest():` with a one-line wrapper that calls `_run_locked_ingest(...)`.

- [ ] **Step 5: Run tests; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_run_locked_ingest.py tests/integration/test_ingestion_phase4.py -v`
Expected: PASS.

- [ ] **Step 6: Run full integration suite**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/integration/ -v`
Expected: PASS (existing tests use mocked `pipeline.ingest` returning None or raising; the helper handles both).

- [ ] **Step 7: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/api/routes/ingestion.py rag/tests/unit/test_run_locked_ingest.py
git commit -m "feat(state-machine): _run_locked_ingest maps outcome to TaskRepo status"
```

---

### Task 11: `delete_old_versions` `Range(lt=keep_version)` + test rewrite

**Files:**
- Modify: `rag/ekrs_rag/retrieval/qdrant_client.py:317-348`
- Modify: `rag/tests/unit/test_qdrant_client.py:329-372` (rewrite two tests)

**Interfaces:**
- Produces: `delete_old_versions(doc_hash, keep_version)` deletes points where `version < keep_version`

- [ ] **Step 1: Rewrite the two failing tests**

Replace `rag/tests/unit/test_qdrant_client.py:329-372`:

```python
def test_delete_old_versions_calls_delete_with_range_lt(qdrant_manager_with_mock):
    mgr, mock_client = qdrant_manager_with_mock
    mgr.delete_old_versions("doc_hash_abc", keep_version=5)
    args, kwargs = mock_client.delete.call_args
    fs = kwargs["points_selector"].filter
    must = fs.must
    assert any(
        getattr(c, "key", None) == "doc_hash"
        and c.match.value == "doc_hash_abc"
        for c in must
    )
    version_cond = next(c for c in must if getattr(c, "key", None) == "version")
    assert version_cond.range.lt == 5


def test_delete_old_versions_does_not_touch_keep_or_future(qdrant_manager_with_mock):
    """Range(lt=5) excludes v5 and any v>5."""
    mgr, mock_client = qdrant_manager_with_mock
    mgr.delete_old_versions("doc_hash_abc", keep_version=3)
    args, kwargs = mock_client.delete.call_args
    must = kwargs["points_selector"].filter.must
    version_cond = next(c for c in must if getattr(c, "key", None) == "version")
    assert version_cond.range.lt == 3
    # No must_not clause
    fs = kwargs["points_selector"].filter
    assert not fs.must_not
```

(The `qdrant_manager_with_mock` fixture is whatever the file already uses; verify it exists or adapt.)

- [ ] **Step 2: Run tests; expect FAIL**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_qdrant_client.py -v -k "delete_old_versions"`
Expected: FAIL — current implementation uses `must_not`.

- [ ] **Step 3: Rewrite `delete_old_versions` body**

Replace `qdrant_client.py:317-348`:

```python
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
    )
    def delete_old_versions(self, doc_hash: str, keep_version: int) -> int:
        """Delete Qdrant points for versions STRICTLY OLDER than keep_version.

        Uses Range(lt=keep_version) so future versions (>=keep_version) survive
        concurrent out-of-order ingestion. Must be called inside the same
        per-doc Redis lock as the upsert to prevent races.
        """
        try:
            result = self._client.delete(
                collection_name=self._collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="doc_hash",
                                match=models.MatchValue(value=doc_hash),
                            ),
                            models.FieldCondition(
                                key="version",
                                range=models.Range(lt=keep_version),
                            ),
                        ],
                    ),
                ),
                wait=True,
            )
            deleted = getattr(result, "deleted", None) or 0
            logger.info(
                "Deleted %d old-version points for %s keeping v%d",
                deleted, doc_hash, keep_version,
            )
            return int(deleted)
        except Exception as exc:
            _emit_qdrant_failure("delete", self._collection_name, exc)
            raise
```

- [ ] **Step 4: Run tests; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_qdrant_client.py -v -k "delete_old_versions"`
Expected: PASS.

- [ ] **Step 5: Verify the audit-emission test (`:496-516`) still passes**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/unit/test_qdrant_client.py::test_delete_old_versions_emits_audit_event_on_failure -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/retrieval/qdrant_client.py rag/tests/unit/test_qdrant_client.py
git commit -m "fix(qdrant): delete_old_versions uses Range(lt=keep_version)"
```

---

### Task 12: `pipeline.ingest()` calls `delete_old_versions` after success

**Files:**
- Modify: `rag/ekrs_rag/ingestion/pipeline.py` (T9 already added the call; this task adds integration test)
- Test: `rag/tests/integration/test_ingestion.py` (new test)

**Interfaces:**
- Produces: After successful upsert, `delete_old_versions` is invoked exactly once with `keep_version=notification.version`

- [ ] **Step 1: Write failing integration test**

Append to `rag/tests/integration/test_ingestion.py`:

```python
def test_ingest_calls_delete_old_versions_after_upsert(
    client, tmp_path, monkeypatch,
):
    """P2: Successful ingestion must clean up old Qdrant versions."""
    storage_root = tmp_path / "root"
    storage_root.mkdir()
    doc_dir = storage_root / "doc1" / "v2"
    doc_dir.mkdir(parents=True)
    (doc_dir / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text","content":"v2 content"}\n'
    )

    # Patch the qdrant_manager dependency to expose delete_old_versions call
    ...
```

A simpler approach: rely on existing `mock_qdrant` fixture; assert mock was called.

```python
def test_ingest_cleans_old_versions(client, mock_qdrant, sample_jsonl):
    """Successful upsert triggers delete_old_versions."""
    resp = client.post(
        "/v1/ingestion/notify",
        headers={"X-Parser-Token": PARSER_TOKEN},
        json={
            "doc_hash": "d1",
            "version": 2,
            "output_path": sample_jsonl.parent.as_posix(),
            "callback_url": "",
        },
    )
    assert resp.status_code == 202
    # Wait for BackgroundTasks to complete
    import time
    for _ in range(50):
        if mock_qdrant.delete_old_versions.called:
            break
        time.sleep(0.05)
    mock_qdrant.delete_old_versions.assert_called_once_with("d1", keep_version=2)
```

(Adapt `sample_jsonl` and `mock_qdrant` to actual fixture names.)

- [ ] **Step 2: Run test; expect FAIL**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/integration/test_ingestion.py -v -k "delete_old_versions"`
Expected: FAIL — old code never called `delete_old_versions`.

- [ ] **Step 3: Confirm T9's pipeline.ingest body contains the `delete_old_versions` call**

Verify `pipeline.py:140-150` (post-upsert block) already calls:

```python
        if settings.OLD_VERSION_DELETE_ENABLED:
            try:
                self._qdrant.delete_old_versions(doc_hash, keep_version=version)
            except Exception as e:
                logger.warning(
                    "delete_old_versions_failed: doc=%s v=%d err=%s",
                    doc_hash, version, e,
                )
```

If T9's body already includes it, this task is just adding the test. Otherwise, add the call.

- [ ] **Step 4: Run test; expect PASS**

Run: `cd /home/pangzy/code_project/EKRS/rag && pytest tests/integration/test_ingestion.py -v -k "delete_old_versions"`
Expected: PASS.

- [ ] **Step 5: Add a second test for `OLD_VERSION_DELETE_ENABLED=False`**

Append to same file:

```python
def test_ingest_skips_cleanup_when_disabled(
    client, mock_qdrant, sample_jsonl, monkeypatch,
):
    monkeypatch.setattr("ekrs_rag.core.config.settings.OLD_VERSION_DELETE_ENABLED", False)
    resp = client.post(
        "/v1/ingestion/notify",
        headers={"X-Parser-Token": PARSER_TOKEN},
        json={
            "doc_hash": "d2",
            "version": 1,
            "output_path": sample_jsonl.parent.as_posix(),
            "callback_url": "",
        },
    )
    assert resp.status_code == 202
    import time
    for _ in range(50):
        if mock_qdrant.upsert_chunks.called:
            break
        time.sleep(0.05)
    mock_qdrant.delete_old_versions.assert_not_called()
```

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/tests/integration/test_ingestion.py
git commit -m "test(ingestion): cover old-version cleanup trigger and disable switch"
```

---

### Task 13: Documentation cleanup (RAG does not read `.ready`)

**Files:**
- Modify: `EKRS-RAG-AI_intergration.md:58,123,127`
- Modify: `docs/USAGE.md` (add `.ready` clause)
- Modify: `CHANGELOG.md` (append entry)

- [ ] **Step 1: Read the false claims**

```bash
sed -n '55,65p;120,130p' EKRS-RAG-AI_intergration.md
```

- [ ] **Step 2: Replace the three false passages**

In `EKRS-RAG-AI_intergration.md`:

Around line 58, replace text claiming "RAG 服务开始处理的唯一信号" with:

> 备注：`.ready` 文件由 parser 原子创建，作为 parser 侧的发布完成信号。RAG 服务**不读取** `.ready`；parser 必须在 JSONL 完整落盘、fsync 完成后才发送 notify。

Around line 123, replace "扫描 /parsed_lib/*/{timestamp}/.ready，对每个 .ready 文件..." with:

> 备注：RAG 没有 `.ready` 轮询扫描器；本说明遗留自早期设计，与当前实现不符。RAG 完全依赖 notify HTTP 触发。

Around line 127, remove the `.processed` rename instructions.

- [ ] **Step 3: Add `.ready` clause to `docs/USAGE.md`**

Find the Ingestion Flow section and add:

> **RAG 不依赖 `.ready` 文件**：parser 必须在 JSONL 完整落盘、`fsync()` 完成后才发送 `POST /v1/ingestion/notify`；RAG 不做 `.ready` 轮询，也不读取 `.processed` 命名约定。

- [ ] **Step 4: Append `CHANGELOG.md` entry**

Add at top:

```markdown
## Unreleased

### Security
- Callback sender now sends `X-Parser-Token` and validates callback URL against
  `CALLBACK_ALLOWED_SCHEMES` (default `https`). Loopback/metadata IP literals
  and DNS-resolved private addresses are rejected.
- `notify` rejects `output_path` that escapes `SHARED_STORAGE_PATH`.
- `PARSER_TOKEN` defaults (placeholder / empty / <32 chars) fail at startup.

### Fixed
- `pipeline.ingest()` now returns `IngestionOutcome`; route maps outcome to
  TaskRepo status. Previous behavior marked all business failures as COMPLETED.
- `delete_old_versions` uses `Range(lt=keep_version)`; old versions are now
  cleaned after successful upsert under the per-doc Redis lock.

### Docs
- Removed false `.ready` detection claims from `EKRS-RAG-AI_intergration.md`.
```

- [ ] **Step 5: Verify doc consistency**

```bash
grep -n ".ready" EKRS-RAG-AI_intergration.md
grep -n ".ready" docs/USAGE.md
```

Expected: only the new "RAG does not read" notes remain.

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add EKRS-RAG-AI_intergration.md docs/USAGE.md CHANGELOG.md
git commit -m "docs: remove false RAG-side .ready claims; add IngestionOutcome changelog"
```

---

### Task 14: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Run full test suite**

```bash
cd /home/pangzy/code_project/EKRS/rag
pytest tests/unit/ tests/integration/ -v --tb=short
```

Expected: all tests PASS. Note any pre-existing failures not caused by this work; do not fix them unless trivial.

- [ ] **Step 2: Run bandit on changed files**

```bash
cd /home/pangzy/code_project/EKRS/rag
bandit -r ekrs_rag/security/ ekrs_rag/ingestion/ ekrs_rag/api/routes/ingestion.py -f json
```

Expected: no HIGH severity findings. Low/Medium findings (e.g., `assert`-in-prod patterns) are pre-existing.

- [ ] **Step 3: Run mypy on changed files**

```bash
cd /home/pangzy/code_project/EKRS/rag
mypy ekrs_rag/security/ ekrs_rag/ingestion/ ekrs_rag/api/routes/ingestion.py
```

Expected: no new errors.

- [ ] **Step 4: Manual round-trip smoke test**

Start EKRS:

```bash
cd /home/pangzy/code_project/EKRS/rag
mkdir -p /tmp/parsed_lib
PARSER_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))") \
SHARED_STORAGE_PATH=/tmp/parsed_lib \
  uvicorn ekrs_rag.main:app --host 127.0.0.1 --port 8000
```

Start a mock doc-to-md callback receiver in another terminal:

```bash
python - <<'PY'
import http.server, hmac, sys
EXPECTED = open("/tmp/token.txt").read().strip() if False else sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        tok = self.headers.get("X-Parser-Token", "")
        ok = hmac.compare_digest(tok, EXPECTED)
        body = self.rfile.read(int(self.headers.get("Content-Length","0")))
        sys.stdout.write(f"recv: status={'OK' if ok else '401'} body={body!r}\n")
        sys.stdout.flush()
        self.send_response(200 if ok else 401)
        self.end_headers()
http.server.HTTPServer(("127.0.0.1", 9001), H).serve_forever()
PY
"$PARSER_TOKEN"
```

Send a notify (call it from a third shell with the same PARSER_TOKEN):

```bash
mkdir -p /tmp/parsed_lib/doc1/v1
echo '{"doc_id":"d1","block_id":"b1","type":"text","content":"hi"}' \
  > /tmp/parsed_lib/doc1/v1/data.jsonl
curl -X POST http://127.0.0.1:8000/v1/ingestion/notify \
  -H "X-Parser-Token: $PARSER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"doc_hash":"doc1","version":1,"output_path":"/tmp/parsed_lib/doc1/v1","callback_url":"http://127.0.0.1:9001/cb"}'
```

Verify the mock receiver prints `recv: status=OK body=...`.

- [ ] **Step 5: Verify TaskRepo final state**

```bash
sqlite3 /var/lib/ekrs/tasks.db "SELECT request_id, status, last_error FROM tasks ORDER BY updated_at DESC LIMIT 5;"
```

Expected: status is `COMPLETED` (success) or `FAILED` (business failure). No `COMPLETED` rows for failed ingests.

- [ ] **Step 6: Verify Qdrant old-version cleanup (P2)**

Send notify v1, then v2, observe Qdrant:

```bash
# After both notifies
curl -X POST "http://127.0.0.1:6333/collections/rag_documents/points/scroll" \
  -H "Content-Type: application/json" \
  -d '{"filter":{"must":[{"key":"doc_hash","match":{"value":"doc1"}}]},"limit":10,"with_payload":true}'
```

Expected: only v2 points remain (v1 was deleted).

- [ ] **Step 7: Update OpenWolf memory**

Append to `.wolf/memory.md` current session table:

```
| HH:MM | End-to-end round-trip verified | manual | — | ~tokens |
| HH:MM | bandit/mypy clean | manual | — | ~tokens |
```

Append to `.wolf/cerebrum.md` Key Learnings:

- **`IngestionOutcome` separates business failure from system exception**: route wrapper can map outcome to TaskRepo status without relying on exception flow.
- **`delete_old_versions` MUST run inside `lock:ingest:{doc_hash}`**: separate lock would not serialize with the upsert that just succeeded.

- [ ] **Step 8: Commit verification artifacts**

```bash
cd /home/pangzy/code_project/EKRS
git add .wolf/memory.md .wolf/cerebrum.md
git commit -m "chore: log end-to-end verification + learnings"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Task |
|---|---|
| Send `X-Parser-Token` | T6 |
| URL allowlist (scheme/host) | T4 |
| Reject loopback/metadata IPs | T4 |
| 4xx no-retry, 5xx retry | T7 |
| Reject empty/short/default `PARSER_TOKEN` | T3 |
| Reject relative/missing `SHARED_STORAGE_PATH` | T1 |
| Reject `output_path` outside `SHARED_STORAGE_PATH` | T2 |
| `pipeline.ingest()` returns explicit outcome | T9 |
| Route maps outcome → TaskRepo | T10 |
| `delete_old_versions` `Range(lt=keep_version)` | T11 |
| `delete_old_versions` invoked after upsert | T12 |
| `.ready` doc cleanup | T13 |
| End-to-end verification | T14 |

### Placeholder scan

- No "TODO" / "TBD" / "implement later" markers.
- All `Step 3: Implement ...` blocks contain complete code.
- All `Step 1: Write failing test` blocks contain complete test code.
- No "Similar to Task N" — each task's test is self-contained.

### Type consistency

- `IngestionOutcome.rag_status` is `Literal["success","failed"]` everywhere (T8 dataclass + T9 returns + T10 wrapper).
- `delete_old_versions(doc_hash: str, keep_version: int) -> int` — signature stable across T11 and T12.
- `_run_locked_ingest(pipeline, repo, lock, lock_key, lock_token, notification, request_id)` — signature used in both T10 unit test and T10 refactor of `routes/ingestion.py`.
- `settings.CALLBACK_ALLOWED_SCHEMES` — read as string env var in T4; consumed via `os.environ.get` inside `validate_callback_url`. Document this convention.
- `OLD_VERSION_DELETE_ENABLED` — set in T6 config, read in T9 and T12.

### Risks

- DNS rebinding (T4): not fully mitigated; documented in code comment.
- `_send_callback` refactor (T6) touches every existing pipeline test that mocks HTTP; verified by Step 5 in T9.
- `mark_failed_with_error` appends `last_error` over time; no rotation (P3 risk).