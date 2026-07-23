"""Unit tests for scripts/lib_smoke.py.

Phase 8 T8-3b: smoke_ingestion script's Python helpers. These tests
exercise the pure-Python logic (payload construction, status validation,
audit log scanning, callback capture) without spinning up a real RAG
stack. Integration with the running stack is covered by
`make smoke-ingestion` (manual, post-deploy).

lib_smoke.py is intentionally NOT under the ekrs_rag package — it ships
with scripts/ as a standalone helper. The test imports it by injecting
scripts/ onto sys.path so pytest's standard testpaths discovery works.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import lib_smoke  # noqa: E402  (intentional post-sys.path import)


# ---------- build_notification_payload ----------


class TestBuildNotificationPayload:
    def test_minimal_required_fields(self) -> None:
        payload = lib_smoke.build_notification_payload(
            doc_hash="abc123",
            output_path="/tmp/parser_out/abc123",
            callback_url="http://localhost:9000/cb",
        )
        assert payload["doc_hash"] == "abc123"
        assert payload["output_path"] == "/tmp/parser_out/abc123"
        assert payload["callback_url"] == "http://localhost:9000/cb"
        assert payload["version"] == 1
        assert "trace_id" in payload and payload["trace_id"]

    def test_trace_id_unique_per_call(self) -> None:
        a = lib_smoke.build_notification_payload("a", "/p", "http://x")
        b = lib_smoke.build_notification_payload("a", "/p", "http://x")
        assert a["trace_id"] != b["trace_id"]

    def test_version_override(self) -> None:
        p = lib_smoke.build_notification_payload("a", "/p", "http://x", version=3)
        assert p["version"] == 3

    def test_trace_id_is_uuid4(self) -> None:
        """Smoke trace IDs must be unique enough to grep audit.log."""
        p = lib_smoke.build_notification_payload("a", "/p", "http://x")
        # uuid4 format: 8-4-4-4-12 hex
        import re
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            p["trace_id"],
        ), f"trace_id {p['trace_id']!r} is not uuid4"

    def test_serializable_round_trip(self) -> None:
        """Must JSON-roundtrip without error (smoke script posts it
        to RAG via curl)."""
        p = lib_smoke.build_notification_payload("a", "/p", "http://x")
        round_tripped = json.loads(json.dumps(p))
        assert round_tripped == p


# ---------- validate_status_response ----------


class TestValidateStatusResponse:
    def test_completed_terminal(self) -> None:
        body = {
            "doc_hash": "abc",
            "status": "completed",
            "block_count": 6,
            "ingested_chunks": 12,
            "errors": [],
        }
        assert lib_smoke.validate_status_response(body) == "completed"

    def test_failed_terminal(self) -> None:
        body = {"doc_hash": "abc", "status": "failed", "errors": ["timeout"]}
        assert lib_smoke.validate_status_response(body) == "failed"

    def test_in_progress_not_terminal(self) -> None:
        body = {"doc_hash": "abc", "status": "in_progress"}
        with pytest.raises(lib_smoke.StatusNotTerminal):
            lib_smoke.validate_status_response(body)

    def test_missing_status_field_rejected(self) -> None:
        with pytest.raises(lib_smoke.SmokeError):
            lib_smoke.validate_status_response({"doc_hash": "abc"})

    def test_unknown_status_value_rejected(self) -> None:
        with pytest.raises(lib_smoke.SmokeError):
            lib_smoke.validate_status_response({"status": "wat"})

    def test_never_raises_on_completed(self) -> None:
        # Repeated completed responses should never raise — smoke
        # polling stops on first terminal state.
        body = {"doc_hash": "abc", "status": "completed"}
        for _ in range(3):
            assert lib_smoke.validate_status_response(body) == "completed"


# ---------- check_audit_log_for_failures ----------


class TestCheckAuditLogForFailures:
    def test_no_failures_returns_empty_list(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.log"
        log.write_text(
            '{"event":"ingestion_started","trace_id":"t1"}\n'
            '{"event":"ingestion_completed","trace_id":"t1"}\n'
        )
        assert lib_smoke.check_audit_log_for_failures(log, "t1") == []

    def test_qdrant_write_failed_detected(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.log"
        log.write_text(
            '{"event":"qdrant_write_failed","trace_id":"t1","reason":"port 6334 closed"}\n'
        )
        failures = lib_smoke.check_audit_log_for_failures(log, "t1")
        assert any("qdrant_write_failed" in f for f in failures)

    def test_other_trace_id_failures_not_attributed(self, tmp_path: Path) -> None:
        """We MUST filter by trace_id; otherwise concurrent smoke runs
        cross-contaminate and false-fail."""
        log = tmp_path / "audit.log"
        log.write_text(
            '{"event":"qdrant_write_failed","trace_id":"other"}\n'
            '{"event":"qdrant_write_failed","trace_id":"t1"}\n'
        )
        failures = lib_smoke.check_audit_log_for_failures(log, "t1")
        assert len(failures) == 1
        assert "t1" in failures[0]

    def test_corrupted_line_skipped(self, tmp_path: Path) -> None:
        """Audit log is appended concurrently; a torn write is
        possible. Don't crash — skip and report only parsed lines."""
        log = tmp_path / "audit.log"
        # Two complete failure lines, then a truncated line at EOF
        # (the realistic torn-write case — partial flush mid-write).
        log.write_text(
            '{"event":"qdrant_write_failed","trace_id":"t1"}\n'
            '{"event":"qdrant_write_failed","trace_id":"t1"}\n'
            '{"event":"qdrant_wri'
        )
        failures = lib_smoke.check_audit_log_for_failures(log, "t1")
        assert len(failures) == 2

    def test_missing_audit_log_returns_empty(self, tmp_path: Path) -> None:
        """No audit log = no failures to find. Caller treats this as
        inconclusive, not as a smoke failure."""
        log = tmp_path / "does-not-exist.log"
        assert lib_smoke.check_audit_log_for_failures(log, "t1") == []


# ---------- wait_for_callback (real local server) ----------


class _CallbackCapture(BaseHTTPRequestHandler):
    """Records the first POST it receives, then 200s."""
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        try:
            type(self).received.append(json.loads(body))
        except json.JSONDecodeError:
            type(self).received.append({"_raw": body})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # suppress stderr noise


@pytest.fixture()
def callback_server() -> Iterator[tuple[str, list[dict]]]:
    _CallbackCapture.received = []
    server = HTTPServer(("127.0.0.1", 0), _CallbackCapture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/callback", _CallbackCapture.received
    finally:
        server.shutdown()
        server.server_close()


class TestWaitForCallback:
    def test_returns_payload_when_posted_within_timeout(
        self, callback_server: tuple[str, list[dict]]
    ) -> None:
        url, received = callback_server
        # Simulate the parser-side callback dispatch.
        import urllib.request
        payload = json.dumps({"status": "completed", "doc_hash": "abc"}).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=2).read()
        # Now wait_for_callback should pick it up.
        result = lib_smoke.wait_for_callback(
            received, timeout=2.0, poll_interval=0.05
        )
        assert result["status"] == "completed"

    def test_timeout_raises_status_timeout(self) -> None:
        received: list[dict] = []
        with pytest.raises(lib_smoke.StatusTimeout):
            lib_smoke.wait_for_callback(received, timeout=0.2, poll_interval=0.05)


# ---------- import surface ----------


class TestModuleSurface:
    def test_public_symbols(self) -> None:
        """Lock down the helper's API surface — smoke_ingestion.sh
        imports these by name. If a refactor renames a symbol, this
        test catches it before the script silently breaks."""
        expected = {
            "SmokeError",
            "StatusNotTerminal",
            "StatusTimeout",
            "build_notification_payload",
            "validate_status_response",
            "check_audit_log_for_failures",
            "wait_for_callback",
        }
        actual = set(dir(lib_smoke))
        missing = expected - actual
        assert not missing, f"lib_smoke lost public symbols: {sorted(missing)}"