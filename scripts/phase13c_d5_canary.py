#!/usr/bin/env python3
"""Phase 13c-C13 D5 canary: 50-bundle 灰度 ingest against v1.1 corpus.

Reads deployment/phase13c-d5-canary-50.json (selection manifest),
POSTs /v1/ingestion/notify (v=3) for each doc_hash against the
D5 RAG service (default :8002), polls status until terminal,
aggregates per-category + writes report to
deployment/phase13c-d5-canary-report.json.

Per §4.2 acceptance threshold:
- single_table_monolith (61): 100% expected
- tiny_content_fragmented (11): >= 81%
- mixed_type_large (13): >= 85%
- other (11): >= 82%
- Overall: >= 93.7% (>= 90/96)
- R4/R7/R8/R12 = 0 (validator-contract signal; not enforced here, EKRS-side)

Usage:
  RAG_URL=http://localhost:8002 python3 scripts/phase13c_d5_canary.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

RAG_URL = os.environ.get("RAG_URL", "http://localhost:8002")
SHARED_STORAGE_PATH = os.environ.get("SHARED_STORAGE_PATH", "/mnt/disk/text/v1.1")
SELECTION_PATH = Path(os.environ.get(
    "SELECTION_PATH", "deployment/phase13c-d5-canary-50.json"))
REPORT_PATH = Path(os.environ.get(
    "REPORT_PATH", "deployment/phase13c-d5-canary-report.json"))
TOKEN = os.environ["PARSER_TOKEN"]
NOTIFY_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 60.0  # Histogram shows 24/25 success <=300s; 60s per-bundle catches the slow tail
PARALLEL = int(os.environ.get("PARALLEL", "4"))


def _http(method: str, url: str, *, headers: dict | None = None,
          body: bytes | None = None, timeout: float = 10.0) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except urllib.error.URLError as e:
        return 0, f"URLError: {e.reason}"


def send_notify(doc_hash: str, version: int, run_tag: str) -> tuple[int, dict | str]:
    body = json.dumps({
        "doc_hash": doc_hash,
        "version": version,
        "output_path": f"{SHARED_STORAGE_PATH}/{doc_hash}",
        "callback_url": f"{RAG_URL}/v1/callback",
        # Unique trace_id per run → request_id_from_trace produces a fresh
        # request_id, avoiding idempotency "duplicate" that would skip
        # re-ingestion and leave stale task_state rows visible.
        "trace_id": f"c13d5_{run_tag}_{doc_hash[:8]}",
    }).encode("utf-8")
    # Honor rate limit (Phase 8: 60 req/min/IP). Caller catches HTTP 429 and retries.
    return _http(
        "POST", f"{RAG_URL}/v1/ingestion/notify",
        headers={"Content-Type": "application/json", "X-Parser-Token": TOKEN},
        body=body, timeout=NOTIFY_TIMEOUT_S,
    )


def ingest_one(doc_hash: str, category: str, version: int,
               run_tag: str, max_attempts: int = 4) -> dict:
    t0 = time.monotonic()
    # Retry notify on HTTP 429 (rate limit) per retry_after_sec
    for attempt in range(max_attempts):
        code, payload = send_notify(doc_hash, version, run_tag)
        if code == 429 and isinstance(payload, dict):
            wait = payload.get("retry_after_sec", 30) + 2
            print(f"  [retry {attempt+1}/{max_attempts}] {doc_hash[:12]}... "
                  f"rate-limited, sleep {wait}s", flush=True)
            time.sleep(wait)
            continue
        break
    else:
        return {
            "doc_hash": doc_hash, "category": category,
            "notify_code": 429, "notify_error": payload,
            "status": "notify_failed", "chunks_indexed": 0,
            "latency_s": round(time.monotonic() - t0, 2),
        }
    if code not in (200, 202):
        return {
            "doc_hash": doc_hash, "category": category,
            "notify_code": code, "notify_error": payload,
            "status": "notify_failed", "chunks_indexed": 0,
            "latency_s": round(time.monotonic() - t0, 2),
        }
    status, status_payload = poll_status(doc_hash)
    chunks = 0
    if isinstance(status_payload, dict):
        chunks = int(status_payload.get("chunks_indexed", 0) or 0)
    return {
        "doc_hash": doc_hash, "category": category,
        "notify_code": code, "status": status,
        "chunks_indexed": chunks,
        "error": status_payload.get("error") if isinstance(status_payload, dict) else None,
        "latency_s": round(time.monotonic() - t0, 2),
    }


def poll_status(doc_hash: str) -> tuple[str, dict | str]:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    last_payload: dict | str = {}
    while time.monotonic() < deadline:
        code, payload = _http(
            "GET", f"{RAG_URL}/v1/ingestion/status/{doc_hash}",
            headers={"X-Parser-Token": TOKEN}, timeout=10.0,
        )
        if code != 200:
            time.sleep(POLL_INTERVAL_S)
            continue
        last_payload = payload if isinstance(payload, dict) else {"raw": payload}
        # success/no_chunks/failed/rejected/queued are terminal-ish
        st = last_payload.get("status", "")
        if st in ("success", "no_chunks", "failed", "rejected"):
            return st, last_payload
        if st in ("queued", "in_flight") or st == "":
            time.sleep(POLL_INTERVAL_S)
            continue
        time.sleep(POLL_INTERVAL_S)
    return "timeout", last_payload if isinstance(last_payload, dict) else {"raw": last_payload}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=PARALLEL)
    args = ap.parse_args()

    if not SELECTION_PATH.exists():
        print(f"FATAL: selection manifest not found: {SELECTION_PATH}", file=sys.stderr)
        return 2

    with SELECTION_PATH.open() as f:
        selection = json.load(f)
    by_category: dict[str, list[str]] = selection["doc_hashes"]
    all_hashes = [(h, c) for c, hs in by_category.items() for h in hs]
    print(f"=== Phase 13c-C13 D5 canary ingest ===")
    print(f"  selection: {SELECTION_PATH}")
    print(f"  total: {len(all_hashes)} ({selection.get('by_category', {})})")
    print(f"  RAG_URL: {RAG_URL}")
    print(f"  SHARED_STORAGE_PATH: {SHARED_STORAGE_PATH}")
    print(f"  version: {args.version}")
    print(f"  parallel: {args.parallel}")
    print()

    results: list[dict] = []
    # Unique run_tag per invocation so notify idempotency doesn't suppress
    # re-ingestion across multiple D5 runs against the same doc_hash.
    run_tag = time.strftime("%Y%m%d%H%M%S")
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {ex.submit(ingest_one, h, c, args.version, run_tag): (h, c) for h, c in all_hashes}
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            done += 1
            print(f"  [{done}/{len(all_hashes)}] {r['doc_hash'][:12]}... "
                  f"cat={r['category'][:24]:24s} status={r['status']:12s} "
                  f"chunks={r['chunks_indexed']:4d} t={r['latency_s']:.1f}s",
                  flush=True)

    # Aggregate per-category
    by_cat = defaultdict(lambda: {"total": 0, "success": 0, "no_chunks": 0,
                                   "failed": 0, "rejected": 0, "timeout": 0,
                                   "notify_failed": 0, "chunks": 0})
    for r in results:
        cat = r["category"]
        b = by_cat[cat]
        b["total"] += 1
        b[r["status"]] = b.get(r["status"], 0) + 1
        b["chunks"] += r["chunks_indexed"]
    overall = {"total": len(results), "chunks": 0, "success": 0,
               "no_chunks": 0, "failed": 0, "rejected": 0, "timeout": 0,
               "notify_failed": 0}
    for r in results:
        overall["chunks"] += r["chunks_indexed"]
        overall[r["status"]] = overall.get(r["status"], 0) + 1

    # Acceptance thresholds from §4.2
    THRESH = {
        "single_table_monolith": 1.00,
        "tiny_content_fragmented": 0.81,
        "mixed_type_large": 0.85,
        "single_block_small": 0.82,
        "few_blocks_any": 0.82,
        "oversized_image_block": 0.82,
        "overall": 0.937,
    }

    print()
    print("=== Per-category report ===")
    print(f"  {'category':30s} {'pass':>4s}/{'tot':>4s}  {'rate':>6s}  {'target':>7s}  {'chunks':>7s}")
    all_pass = True
    for cat in sorted(by_cat.keys()):
        b = by_cat[cat]
        passed = b["success"]
        target = THRESH.get(cat, 1.0)
        rate = passed / b["total"] if b["total"] else 0.0
        ok = "✓" if rate >= target else "✗"
        print(f"  {cat:30s} {passed:>4d}/{b['total']:>4d}  {rate*100:>5.1f}%  {target*100:>6.1f}%  {b['chunks']:>7d}  {ok}")
        if rate < target:
            all_pass = False
    overall_rate = overall["success"] / overall["total"]
    overall_target = THRESH["overall"]
    overall_ok = "✓" if overall_rate >= overall_target else "✗"
    print(f"  {'OVERALL':30s} {overall['success']:>4d}/{overall['total']:>4d}  "
          f"{overall_rate*100:>5.1f}%  {overall_target*100:>6.1f}%  {overall['chunks']:>7d}  {overall_ok}")

    # Failed details
    print()
    print("=== Failed doc_hashes ===")
    failed = [r for r in results if r["status"] not in ("success",)]
    for r in failed:
        err = r.get("error") or r.get("notify_error") or ""
        print(f"  {r['doc_hash']} [{r['category']}] {r['status']}: {err}")

    # Write report
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rag_url": RAG_URL,
        "shared_storage_path": SHARED_STORAGE_PATH,
        "version": args.version,
        "selection": str(SELECTION_PATH),
        "per_category": dict(by_cat),
        "overall": overall,
        "acceptance": {
                "all_pass": all_pass and overall_rate >= overall_target,
                "thresholds": THRESH,
            },
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print()
    print(f"=== Report written to {REPORT_PATH} ===")
    print(f"=== Acceptance: {'PASS' if all_pass and overall_rate >= overall_target else 'FAIL'} ===")
    return 0 if (all_pass and overall_rate >= overall_target) else 1


if __name__ == "__main__":
    sys.exit(main())