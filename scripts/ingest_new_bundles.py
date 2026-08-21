#!/usr/bin/env python3
"""Ingest ONLY bundles listed in /tmp/new_valid.json (filtered new bundles).

Reuses task_d_mvp_reingest.py plumbing: docker cp → /v1/ingestion/notify (v=2)
→ poll → verify. Filters pick_bundles to a pre-computed allowlist of doc_hashes
(qdrant-not-yet-indexed + has_content + has_index.json).

Run in background — 2825 bundles × ~20s avg = ~14h wall clock. Progress every 10.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

RAG_URL = os.environ.get("RAG_URL", "http://localhost:8000")
DOCKER_TARGET = os.environ.get("DOCKER_TARGET", "deployment-rag-1")
SHARED_STORAGE_PATH = os.environ.get("SHARED_STORAGE_PATH", "/parsed_lib")
CORPUS_ROOT = Path("/home/pangzy/code_project/doc-to-md/output/text")
INCLUDE_LIST = Path(os.environ.get("INCLUDE_LIST", "/tmp/new_valid.json"))
DEFAULT_VERSION = 2
STATUS_TIMEOUT_S = 600.0
POLL_INTERVAL_S = 2.0
NOTIFY_TIMEOUT_S = 90.0
SEQUENTIAL_PACE_S = 1.0
# Phase 12 row-flush fix Patch 2: dynamic status timeout for pathological
# bundles (267 rows × ~1200 tokens wedge on bge-m3 encode). Estimate the
# number of chunks the chunker will produce from total raw chars; scale
# timeout proportionally with a 600s floor. ~0.8s/chunk is empirically
# observed wall-clock per chunk under normal load (97bc380d566b681b baseline).
ESTIMATED_CHUNK_RATIO = 500  # chars per chunk estimate (matches chunker.DEFAULT_MAX_CHUNK_TOKENS=768 ≈ 4 chars/token)
SECONDS_PER_ESTIMATED_CHUNK = 0.8


def _http(method, url, *, headers=None, body=None, timeout=10.0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, raw
    except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as e:
        # IncompleteRead: server closed connection before sending full
        # response body (typically transient under load). Treat as
        # retryable transient — send_notify will retry up to --retry times.
        return 0, f"{type(e).__name__}: {e}"


def _docker_cp(src: Path, dst_in_container: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["docker", "cp", str(src), f"{DOCKER_TARGET}:{dst_in_container}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout.strip()


def pick_bundles(include_names: list[str]) -> list[tuple[Path, int]]:
    """Return only bundles in include_names, smallest first, with total raw chars.

    Pre-filtered upstream: all names have content + index.json. Sort by n_blocks
    (ascending) so small docs finish first → fast early progress + max
    opportunities to detect pipeline wedges via rate-degradation.

    The returned `total_raw_chars` sums ``len(block.content.raw)`` across all
    blocks; used by ``estimate_status_timeout`` to scale the per-bundle poll
    timeout for pathological tables (Phase 12 row-flush fix Patch 2).
    """
    pairs = []
    for name in include_names:
        b = CORPUS_ROOT / name
        if not (b / "data.jsonl").exists():
            continue
        if not (b / "index.json").exists():
            continue
        total_raw_chars = 0
        n_blocks = 0
        for line in (b / "data.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            n_blocks += 1
            try:
                block = json.loads(line)
                raw = block.get("content", {}).get("raw", "")
                if isinstance(raw, str):
                    total_raw_chars += len(raw)
            except json.JSONDecodeError:
                # Malformed line — skip char accounting but count the block
                # so block-count sort order is stable.
                continue
        pairs.append((n_blocks, total_raw_chars, b))
    pairs.sort()
    return [(b, rc) for _, rc, b in pairs]


def estimate_status_timeout(total_raw_chars: int) -> float:
    """Compute a per-bundle status-poll timeout based on estimated chunk count.

    Phase 12 row-flush fix Patch 2: pathological tables (267 rows × ~1200
    tokens) produce hundreds of sub-chunks and wedge bge-m3 ONNX encode
    past the prior fixed 600s timeout. Scale timeout with chunk count;
    floor at STATUS_TIMEOUT_S=600 for normal bundles.

    Estimate: ``estimated_chunks = max(1, total_raw_chars // 500)`` (the
    chunker's default MAX_CHUNK_TOKENS=768 ≈ 3072 chars ≈ 500 chars gives
    a conservative per-chunk char budget for pathological-data overhead).
    Wall-clock per chunk is empirically ~0.8s under normal load.
    """
    estimated_chunks = max(1, total_raw_chars // ESTIMATED_CHUNK_RATIO)
    return max(STATUS_TIMEOUT_S, estimated_chunks * SECONDS_PER_ESTIMATED_CHUNK)


def send_notify(token: str, doc_hash: str, version: int, output_path: str, trace_id: str) -> tuple[int, dict | str]:
    body = json.dumps({
        "trace_id": trace_id,
        "doc_hash": doc_hash,
        "version": version,
        "output_path": output_path,
        "callback_url": f"{RAG_URL}/v1/callback",
    }).encode("utf-8")
    return _http("POST", f"{RAG_URL}/v1/ingestion/notify",
                 headers={"Content-Type": "application/json",
                          "X-Parser-Token": token},
                 body=body, timeout=NOTIFY_TIMEOUT_S)


def poll_status(token: str, doc_hash: str, timeout_s: float) -> tuple[str, dict]:
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        code, payload = _http("GET", f"{RAG_URL}/v1/ingestion/status/{doc_hash}",
                              headers={"X-Parser-Token": token},
                              timeout=10.0)
        if code == 200 and isinstance(payload, dict):
            last = payload
            status = payload.get("status", "unknown")
            if status in ("success", "failed", "rejected"):
                return status, payload
        time.sleep(POLL_INTERVAL_S)
    return "timeout", last


def load_checkpoint(path: Path) -> dict:
    if not path.exists():
        return {"completed": [], "failed": []}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"completed": [], "failed": []}


def save_checkpoint(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-env", default="PARSER_TOKEN")
    ap.add_argument("--include-list", type=Path, default=INCLUDE_LIST)
    ap.add_argument("--version", type=int, default=DEFAULT_VERSION)
    ap.add_argument("--retry", type=int, default=1)
    ap.add_argument("--pace", type=float, default=SEQUENTIAL_PACE_S)
    ap.add_argument("--status-timeout", type=float, default=STATUS_TIMEOUT_S)
    ap.add_argument("--notify-timeout", type=float, default=NOTIFY_TIMEOUT_S)
    ap.add_argument("--checkpoint", type=Path,
                    default=Path("/tmp/ingest_new_checkpoint.json"))
    ap.add_argument("--reset-checkpoint", action="store_true")
    args = ap.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        print(f"FATAL: env var {args.token_env} not set", file=sys.stderr)
        return 2

    include_names = json.loads(args.include_list.read_text())
    bundles = pick_bundles(include_names)
    print(f"Picked {len(bundles)} bundles from {args.include_list} (sorted by size)")
    if len(bundles) < len(include_names):
        print(f"  NOTE: {len(include_names) - len(bundles)} missing data.jsonl/index.json — skipped")

    if args.reset_checkpoint and args.checkpoint.exists():
        args.checkpoint.unlink()
    ckpt = load_checkpoint(args.checkpoint)
    completed = set(ckpt.get("completed", []))
    failed = ckpt.get("failed", [])
    if completed or failed:
        print(f"  Checkpoint: {len(completed)} completed, {len(failed)} failed (resume mode)")

    # Build a lookup of path → total_raw_chars so we can compute per-bundle
    # timeout in the notify loop without re-reading data.jsonl.
    raw_chars_by_name = {b.name: rc for b, rc in bundles}

    pending = [b for b, _ in bundles if b.name not in completed]
    print(f"  Pending after checkpoint filter: {len(pending)}")

    print(f"\n=== Step 1: docker cp {len(pending)} bundles into {DOCKER_TARGET}:{SHARED_STORAGE_PATH}/ ===")
    cp_failures = []
    for i, b in enumerate(pending, 1):
        ok, err = _docker_cp(b, SHARED_STORAGE_PATH)
        if not ok:
            print(f"  FAIL cp {b.name}: {err}", file=sys.stderr)
            cp_failures.append(b.name)
        if i % 100 == 0:
            print(f"  --- cp progress: {i}/{len(pending)} ---")
    if cp_failures:
        print(f"  {len(cp_failures)} cp failures; skipping those in notify phase", file=sys.stderr)
        pending = [b for b in pending if b.name not in cp_failures]
    print(f"  All cp done ({len(pending)} pending)")

    print(f"\n=== Step 2: notify (v={args.version}) + poll ===")
    new_outcomes = []
    t_run_start = time.monotonic()
    for i, b in enumerate(pending, 1):
        doc_hash = b.name
        output_path = f"{SHARED_STORAGE_PATH}/{doc_hash}"
        trace_id = f"ingest-new-{doc_hash[:8]}-{int(time.time())}"

        # Phase 12 row-flush fix Patch 2: scale status-poll timeout with the
        # estimated chunk count for pathological bundles (267-row tables).
        # Floor at args.status_timeout (default 600s) for normal bundles.
        total_raw_chars = raw_chars_by_name.get(doc_hash, 0)
        effective_timeout = estimate_status_timeout(total_raw_chars)
        if effective_timeout > args.status_timeout:
            print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... dynamic_timeout={effective_timeout:.0f}s "
                  f"(raw_chars={total_raw_chars}, estimated_chunks={max(1, total_raw_chars // ESTIMATED_CHUNK_RATIO)})")

        last_status = None
        last_body = None
        chunk_count = -1
        notify_code = 0
        for attempt in range(1, args.retry + 1):
            t0 = time.monotonic()
            print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... notify attempt {attempt}/{args.retry}", flush=True)
            notify_code, payload = send_notify(token, doc_hash, args.version, output_path, trace_id)
            print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... notify returned HTTP={notify_code}", flush=True)
            if notify_code not in (200, 202):
                print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... NOTIFY FAIL HTTP={notify_code} (attempt {attempt}/{args.retry})", file=sys.stderr)
                last_body = payload
                time.sleep(args.pace)
                continue
            status, body = poll_status(token, doc_hash, effective_timeout)
            dt = time.monotonic() - t0
            chunk_count = body.get("chunks_indexed", -1) if isinstance(body, dict) else -1
            last_status = status
            last_body = body
            if status == "success":
                print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → {status:9s} chunks={chunk_count}  ({dt:.1f}s)")
                break
            if status in ("failed", "rejected"):
                print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → {status:9s} (terminal) chunks={chunk_count}")
                break
            if attempt < args.retry:
                print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → timeout (attempt {attempt}/{args.retry}, retrying)")
                time.sleep(args.pace)
            else:
                print(f"  [{i:4d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → timeout (final)")

        new_outcomes.append((doc_hash, last_status or "rejected", chunk_count if isinstance(last_body, dict) else -1, last_body))

        if last_status == "success":
            completed.add(doc_hash)
        else:
            failed.append({"doc_hash": doc_hash, "status": last_status, "retries": args.retry})
        save_checkpoint(args.checkpoint, {
            "version": args.version,
            "started_at": ckpt.get("started_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            "completed": sorted(completed),
            "failed": failed,
        })

        if i % 10 == 0:
            elapsed = time.monotonic() - t_run_start
            rate = i / elapsed if elapsed > 0 else 0
            eta_s = (len(pending) - i) / rate if rate > 0 else 0
            print(f"  --- progress: {i}/{len(pending)} | elapsed {elapsed:.0f}s | rate {rate:.3f} docs/s | ETA {eta_s:.0f}s ({eta_s/3600:.1f}h) ---")

        time.sleep(args.pace)

    print(f"\n=== Summary ===")
    n_success = sum(1 for _, s, *_ in new_outcomes if s == "success")
    n_failed = sum(1 for _, s, *_ in new_outcomes if s in ("failed", "rejected", "timeout"))
    total_chunks = sum(c for _, _, c, _ in new_outcomes if isinstance(c, int) and c > 0)
    elapsed_total = time.monotonic() - t_run_start
    print(f"  This run: {len(new_outcomes)}  Success: {n_success}  Failed/timeout: {n_failed}  Chunks: {total_chunks}")
    print(f"  Cumulative: {len(completed)}/{len(bundles)} ingested (all-time)")
    print(f"  Elapsed: {elapsed_total:.0f}s ({elapsed_total/3600:.2f}h)")
    if n_failed:
        for h, s, c, body in new_outcomes:
            if s in ("failed", "rejected", "timeout"):
                print(f"    FAILED {h}: {s} body={body!r}")

    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())