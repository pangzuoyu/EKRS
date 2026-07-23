"""Helper library for scripts/smoke_ingestion.sh (Phase 8 T8-3b).

Pure-Python helpers that the bash smoke script invokes via either
import (from Python) or `python3 scripts/lib_smoke.py <cmd> [args]`
from bash. Each public symbol is tested in
rag/tests/unit/test_lib_smoke.py.

Design rule: this module imports NOTHING from ekrs_rag. It must be
runnable on a host with just the Python 3.11 stdlib. Keeps the
script testable without booting the full RAG stack.

CLI surface (used by smoke_ingestion.sh):
    python3 lib_smoke.py build-payload --doc-hash ... --output-path ... --callback-url ... [--version N]
        -> emits a JSON object on stdout (use $(jq -r .trace_id) in bash)
    python3 lib_smoke.py check-audit --audit-path <path> --trace-id <id>
        -> emits matched failure JSON lines on stdout (empty = clean)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict

# ----- public exception hierarchy -----------------------------------


class SmokeError(Exception):
    """Base for all smoke-script errors. The bash wrapper treats any
    SmokeError as a smoke FAIL signal (non-zero exit + [STEP N] line)."""


class StatusNotTerminal(SmokeError):
    """Status response was 2xx but `status` field was not in the
    terminal-state set ({completed, failed}). Polling should continue."""


class StatusTimeout(SmokeError):
    """Polling exceeded its budget (or callback never arrived)."""


# ----- payload construction -----------------------------------------


class _NotificationPayload(TypedDict, total=False):
    doc_hash: str
    version: int
    output_path: str
    callback_url: str
    trace_id: str


def build_notification_payload(
    doc_hash: str,
    output_path: str,
    callback_url: str,
    *,
    version: int = 1,
) -> _NotificationPayload:
    """Build the JSON body for POST /v1/ingestion/notify.

    trace_id is generated fresh per call (uuid4) so concurrent smoke
    runs don't collide in audit.log. The bash wrapper uses this to
    correlate notify → status → audit → callback.
    """
    return {
        "doc_hash": doc_hash,
        "version": version,
        "output_path": output_path,
        "callback_url": callback_url,
        "trace_id": str(uuid.uuid4()),
    }


# ----- status validation --------------------------------------------


_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def validate_status_response(body: dict[str, Any]) -> str:
    """Return the `status` field if it is a known terminal value;
    raise StatusNotTerminal otherwise (caller should keep polling).

    A response with no `status` field or an unknown status is a smoke
    FAIL — the RAG service contract guarantees the field exists once
    the doc has been accepted (HTTP 2xx).
    """
    if not isinstance(body, dict):
        raise SmokeError(f"status body is not a dict: {type(body).__name__}")
    if "status" not in body:
        raise SmokeError("status response missing required `status` field")
    status = body["status"]
    if status not in _TERMINAL_STATUSES:
        raise StatusNotTerminal(
            f"status {status!r} is not terminal "
            f"(expected one of {sorted(_TERMINAL_STATUSES)})"
        )
    return status


# ----- audit log scanning -------------------------------------------


# Failure events that should never appear in a healthy smoke run.
_AUDIT_FAILURE_EVENTS = ("qdrant_write_failed",)


def check_audit_log_for_failures(audit_path: Path, trace_id: str) -> list[str]:
    """Scan audit.log for entries attributed to `trace_id` that
    indicate a write-side failure. Returns a list of the matched
    JSON lines (empty = healthy).

    Tolerant of torn writes (a partial JSON line at EOF is skipped,
    not raised). Missing audit.log returns [] (caller treats as
    inconclusive, not failure).
    """
    if not audit_path.exists():
        return []
    failures: list[str] = []
    for raw in audit_path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn write — skip
        if entry.get("trace_id") != trace_id:
            continue
        event = entry.get("event", "")
        if event in _AUDIT_FAILURE_EVENTS:
            failures.append(line)
    return failures


# ----- callback polling ---------------------------------------------


def wait_for_callback(
    received: list[dict[str, Any]],
    *,
    timeout: float,
    poll_interval: float = 0.1,
) -> dict[str, Any]:
    """Block until `received` is non-empty or `timeout` elapses.

    The bash wrapper runs a small HTTP server in a background process
    that appends incoming POST bodies to a shared list (the list is
    captured via a Python callback file the server writes to). This
    helper polls that list.

    Raises StatusTimeout if no callback arrives within `timeout`.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if received:
            return received[0]
        time.sleep(poll_interval)
    raise StatusTimeout(
        f"no callback received within {timeout:.1f}s "
        f"(received {len(received)} so far)"
    )


# ----- CLI dispatch --------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lib_smoke")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build-payload", help="emit JSON notification payload")
    p_build.add_argument("--doc-hash", required=True)
    p_build.add_argument("--output-path", required=True)
    p_build.add_argument("--callback-url", required=True)
    p_build.add_argument("--version", type=int, default=1)

    p_audit = sub.add_parser("check-audit", help="scan audit.log for failure events")
    p_audit.add_argument("--audit-path", required=True, type=Path)
    p_audit.add_argument("--trace-id", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "build-payload":
        payload = build_notification_payload(
            doc_hash=args.doc_hash,
            output_path=args.output_path,
            callback_url=args.callback_url,
            version=args.version,
        )
        print(json.dumps(payload))
        return 0
    if args.cmd == "check-audit":
        failures = check_audit_log_for_failures(args.audit_path, args.trace_id)
        for line in failures:
            print(line)
        return 0
    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())