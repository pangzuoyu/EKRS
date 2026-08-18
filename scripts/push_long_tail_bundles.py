#!/usr/bin/env python3
"""Phase 12 8/20 联调: push 15 LOT bundles from doc-to-md output into RAG.

Pure I/O shell — reads the doc-to-md ``recommended_first`` manifest, for each
bundle_id POSTs ``/v1/ingestion/notify`` with ``output_path=output_base/{id}/``,
``doc_hash={id}``, ``version=1`` so the RAG pipeline ingests the JSONL the
doc-to-md parser already wrote.

Unlocks the real-infra round of recall@10 baseline (per
[[phase12-recall-baseline-ground-truth]] commit 9ec3312). Without this, the
15 LOT bundles sit in ``/home/pangzy/code_project/doc-to-md/output/text/``
but never reach Qdrant → ground-truth extractor finds 0 chunks → baseline
measures nothing.

Usage examples:
    # Dry-run: preview what would be POSTed, no network
    python scripts/push_long_tail_bundles.py --dry-run

    # Smoke 1 bundle end-to-end (poll status until terminal)
    python scripts/push_long_tail_bundles.py \\
        --smoke-bundle-id 000151778ca35475 \\
        --output-base /home/pangzy/code_project/doc-to-md/output/text

    # Push all 15 sequential with 1s pacing
    python scripts/push_long_tail_bundles.py --limit 15

Pre-conditions (caller's responsibility):
    - RAG service running at ``--rag-url`` (default http://localhost:8000)
    - ``$PARSER_TOKEN`` matches RAG's configured ``PARSER_TOKEN``
    - ``$SHARED_STORAGE_PATH`` (or compose default ``/parsed_lib``) is
      bound to ``--output-base`` so the pipeline can read the JSONL
      (compose: bind-mount the doc-to-md dir; local: override env var)

Exit codes:
    0 — all notifications accepted (smoke: terminal success; bulk: ≥1 success)
    2 — RAG refused (network or 4xx auth)
    3 — manifest missing or output_base dir missing
    4 — smoke bundle reached terminal state == failed
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path(
    "/home/pangzy/code_project/doc-to-md/scripts/long_tail_lot_check_152.json"
)
DEFAULT_OUTPUT_BASE = Path("/home/pangzy/code_project/doc-to-md/output/text")
DEFAULT_RAG_URL = "http://localhost:8000"
DEFAULT_LIMIT = 15
DEFAULT_VERSION = 1
BUNDLE_PACING_SEC = 1.0
STATUS_POLL_TIMEOUT_SEC = 120
STATUS_POLL_INTERVAL_SEC = 2


# ---------------------------------------------------------------------------
# Manifest loader (mirrors scripts/recall_at_10_form_field_baseline.py:load_bundles)
# ---------------------------------------------------------------------------


def load_bundles(manifest_path: Path, limit: int) -> List[Dict[str, Any]]:
    """Read ``recommended_first`` from the doc-to-md bundle manifest.

    Falls back to ``full_list`` if ``recommended_first`` is missing.
    Returns up to ``limit`` raw bundle dicts (we re-read at call sites so
    the shape matches the original manifest, including any extra fields).
    """
    if not manifest_path.exists():
        logger.error("Bundle manifest missing: %s", manifest_path)
        sys.exit(3)
    with manifest_path.open() as f:
        data = json.load(f)
    raw = data.get("recommended_first") or data.get("full_list") or []
    bundles = raw[:limit]
    logger.info("Loaded %d bundles from %s", len(bundles), manifest_path)
    return bundles


# ---------------------------------------------------------------------------
# Notification POST + status poll
# ---------------------------------------------------------------------------


def post_notification(
    rag_url: str,
    parser_token: str,
    output_path: str,
    doc_hash: str,
    version: int,
) -> Dict[str, Any]:
    """POST /v1/ingestion/notify and return the parsed JSON response.

    Exits with code 2 on transport failure or 4xx auth (auth failure
    is fatal: no point retrying all 15 with bad token).
    """
    import httpx  # local import — keep script CLI-friendly without import-time cost

    payload = {
        "trace_id": f"push-long-tail-{int(time.time())}-{doc_hash[:8]}",
        "doc_hash": doc_hash,
        "version": version,
        "output_path": output_path,
        "callback_url": "",  # no parser-side callback for synthetic push
    }
    headers = {
        "Content-Type": "application/json",
        "X-Parser-Token": parser_token,
    }
    try:
        resp = httpx.post(
            f"{rag_url}/v1/ingestion/notify",
            json=payload,
            headers=headers,
            timeout=30,
        )
    except httpx.HTTPError as e:
        logger.error("POST failed for %s: %s", doc_hash, e)
        sys.exit(2)

    if resp.status_code == 401 or resp.status_code == 403:
        logger.error(
            "Auth refused (HTTP %d) — check $PARSER_TOKEN matches RAG config",
            resp.status_code,
        )
        sys.exit(2)
    if resp.status_code >= 400:
        logger.error(
            "POST failed for %s: HTTP %d %s",
            doc_hash, resp.status_code, resp.text[:200],
        )
        return {"error": f"HTTP {resp.status_code}", "body": resp.text[:200]}
    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"raw": resp.text[:200]}


def poll_status(
    rag_url: str,
    doc_hash: str,
    timeout_sec: int = STATUS_POLL_TIMEOUT_SEC,
    interval_sec: float = STATUS_POLL_INTERVAL_SEC,
) -> Dict[str, Any]:
    """Poll ``GET /v1/ingestion/status/{doc_hash}`` until terminal status.

    Returns the last status payload. Terminal states are success / failed.
    Raises TimeoutError if neither reached within ``timeout_sec``.
    """
    import httpx

    deadline = time.monotonic() + timeout_sec
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"{rag_url}/v1/ingestion/status/{doc_hash}",
                timeout=10,
            )
            if resp.status_code == 200:
                last = resp.json()
                status = last.get("status", "")
                if status in ("success", "failed"):
                    return last
        except httpx.HTTPError as e:
            logger.debug("status poll failed (will retry): %s", e)
        time.sleep(interval_sec)
    raise TimeoutError(
        f"status polling timed out after {timeout_sec}s for {doc_hash}; "
        f"last status: {last}"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 12 8/20 联调: push long-tail LOT bundles to RAG",
    )
    parser.add_argument(
        "--rag-url", default=DEFAULT_RAG_URL,
        help="RAG base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help="Path to bundle manifest JSON",
    )
    parser.add_argument(
        "--output-base", type=Path, default=DEFAULT_OUTPUT_BASE,
        help="doc-to-md output dir; pipeline reads output_base/{bundle_id}/data.jsonl",
    )
    parser.add_argument(
        "-n", "--limit", type=int, default=DEFAULT_LIMIT,
        help="Number of bundles to push (default 15 per Phase 12 §七 Item 3)",
    )
    parser.add_argument(
        "--version", type=int, default=DEFAULT_VERSION,
        help="Document version to assign (default 1 for first ingest)",
    )
    parser.add_argument(
        "--smoke-bundle-id", default=None,
        help="Push only this single bundle_id, poll status until terminal, "
        "exit non-zero if failed (smoke test before bulk)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print notifications that would be POSTed, no network",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser_token = os.environ.get("PARSER_TOKEN")
    if not parser_token and not args.dry_run:
        logger.error("$PARSER_TOKEN env var is required (set to RAG's PARSER_TOKEN)")
        return 2

    bundles = load_bundles(args.manifest, args.limit)
    if args.smoke_bundle_id:
        bundles = [b for b in bundles if b["bundle_id"] == args.smoke_bundle_id]
        if not bundles:
            logger.error(
                "smoke bundle %r not in manifest (recommend_first subset)",
                args.smoke_bundle_id,
            )
            return 3

    summary: Dict[str, Any] = {
        "rag_url": args.rag_url,
        "output_base": str(args.output_base),
        "manifest": str(args.manifest),
        "limit": len(bundles),
        "results": [],
    }

    for i, b in enumerate(bundles):
        bundle_id = b["bundle_id"]
        output_path = str(args.output_base / bundle_id)
        result: Dict[str, Any] = {
            "bundle_id": bundle_id,
            "output_path": output_path,
        }

        # Pre-check: output dir exists with data.jsonl
        data_jsonl = Path(output_path) / "data.jsonl"
        if not data_jsonl.exists():
            result["status"] = "skipped"
            result["reason"] = f"data.jsonl missing at {data_jsonl}"
            logger.warning("SKIP %s — %s", bundle_id, result["reason"])
            summary["results"].append(result)
            continue

        if args.dry_run:
            result["status"] = "dry_run"
            result["payload"] = {
                "doc_hash": bundle_id,
                "version": args.version,
                "output_path": output_path,
            }
            logger.info("DRY-RUN %s → POST %s", bundle_id, output_path)
            summary["results"].append(result)
            continue

        notify_resp = post_notification(
            rag_url=args.rag_url,
            parser_token=parser_token or "",
            output_path=output_path,
            doc_hash=bundle_id,
            version=args.version,
        )
        result["notify_response"] = notify_resp
        result["status"] = "notified"

        # Smoke mode: poll status until terminal
        if args.smoke_bundle_id:
            try:
                final_status = poll_status(args.rag_url, bundle_id)
            except TimeoutError as e:
                logger.error("SMOKE %s: %s", bundle_id, e)
                result["status"] = "timeout"
                summary["results"].append(result)
                print(json.dumps(summary, indent=2, ensure_ascii=False))
                return 4
            result["final_status"] = final_status
            if final_status.get("status") == "success":
                logger.info(
                    "SMOKE OK %s — chunks_indexed=%d",
                    bundle_id, final_status.get("chunks_indexed", 0),
                )
                result["status"] = "success"
            else:
                logger.error(
                    "SMOKE FAILED %s — %s",
                    bundle_id, final_status.get("error", "?"),
                )
                result["status"] = "failed"
                summary["results"].append(result)
                print(json.dumps(summary, indent=2, ensure_ascii=False))
                return 4

        summary["results"].append(result)

        # Pace between notifications (avoid callback storms)
        if i < len(bundles) - 1:
            time.sleep(BUNDLE_PACING_SEC)

    if args.dry_run:
        n = sum(1 for r in summary["results"] if r["status"] == "dry_run")
        n_skip = sum(1 for r in summary["results"] if r["status"] == "skipped")
        print(f"Dry-run complete: {n} would-post, {n_skip} skipped")
    else:
        n_ok = sum(1 for r in summary["results"] if r["status"] == "success")
        n_notify = sum(1 for r in summary["results"] if r["status"] == "notified")
        n_skip = sum(1 for r in summary["results"] if r["status"] == "skipped")
        n_fail = sum(1 for r in summary["results"]
                     if r["status"] in ("failed", "timeout"))
        print(
            f"Push complete: {n_ok} success, {n_notify} notified-no-poll, "
            f"{n_skip} skipped, {n_fail} failed"
        )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())