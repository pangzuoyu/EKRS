#!/usr/bin/env python3
"""Phase 12 Task D: bulk re-ingest doc-to-md bundles through live RAG.

Originally an MVP harness for 15-bundle verification (doc_type + heading_path
end-to-end via Task C + T10b-2). Now scaled to 745 bundles per user instruction
("再扩展到 745 条"). Same plumbing: docker cp → /v1/ingestion/notify (version=2)
→ poll → verify Qdrant payload.

Defaults are tuned for the full corpus. Override via flags for MVP reruns.

Key changes vs MVP version:
- N_BUNDLES 15 → 745 (configurable via --limit)
- MAX_BLOCKS_PER_DOC 30 → unlimited (--max-blocks, default 0=unlimited)
- min-blocks filter dropped (some valid bundles have <5 blocks)
- --version flag (default 2: forces re-ingest via pipeline skip bypass)
- --retry default 1 (don't pile on a wedged pipeline)
- --status-timeout default 600s (generous for bge-m3 encoding 200+ blocks)
- Checkpoint/resume via JSON file (--checkpoint)
- Noisy run-progress logs every 10 bundles

Operational notes (Phase 12 Task D, 2026-08-19):
- /v1/ingestion/notify returns 202 + queued in ~12ms (parent §204 async-ack)
- Actual ingestion runs async in the pipeline background; ONE document at a time
- Large bundles (485+ blocks) can take 10+ min each — filter via --max-blocks if wedging
- PIDs in rag container accumulate if queue backs up → 100% CPU + hang
- Restart container (`docker compose restart rag`) recovers from wedge
- Use --reset-checkpoint for clean run, --resume for retry of failed bundles
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Force unbuffered stdout/stderr (live progress in CI/background runs)
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass

RAG_URL = os.environ.get("RAG_URL", "http://localhost:8000")
DOCKER_TARGET = os.environ.get("DOCKER_TARGET", "deployment-rag-1")
SHARED_STORAGE_PATH = os.environ.get("SHARED_STORAGE_PATH", "/parsed_lib")
CORPUS_ROOT = Path("/home/pangzy/code_project/doc-to-md/output/text")
DEFAULT_LIMIT = 745
DEFAULT_VERSION = 2
STATUS_TIMEOUT_S = 600.0  # generous: bge-m3 encoding 200 blocks ~60s + Qdrant upsert ~5s + margin
POLL_INTERVAL_S = 2.0
NOTIFY_TIMEOUT_S = 90.0
SEQUENTIAL_PACE_S = 1.0  # minimal — pipeline is async (parent §204), notify returns 202 immediately


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
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"{type(e).__name__}: {e}"


def _docker_cp(src: Path, dst_in_container: str) -> tuple[bool, str]:
    """Copy src → <DOCKER_TARGET>:<dst_in_container> via `docker cp`."""
    proc = subprocess.run(
        ["docker", "cp", str(src), f"{DOCKER_TARGET}:{dst_in_container}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout.strip()


def _bundle_has_content(b: Path) -> bool:
    """True if any block in data.jsonl has non-empty 'content.raw'.

    Returns False for "all-empty" bundles that the chunker will reject
    with no_chunks (pipeline logs ingestion_failed but doesn't update
    TaskRepo — status queries return 404 forever). Pre-filtering here
    avoids the script polling for status that will never appear.

    Robust to non-string 'raw' values (lists/dicts in some source data);
    only treat as content if raw is a non-empty string after strip().
    """
    jsonl = b / "data.jsonl"
    if not jsonl.exists():
        return False
    for line in jsonl.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = obj.get("content", {})
        raw = None
        if isinstance(content, dict):
            raw = content.get("raw")
        elif isinstance(content, str):
            raw = content
        if isinstance(raw, str) and raw.strip():
            return True
    return False


def pick_bundles(corpus_root: Path, n: int, min_blocks: int, max_blocks: int, require_content: bool = True):
    """Pick n smallest bundles with min_blocks <= blocks <= max_blocks.

    Sorting by block count makes the run deterministic + fast early
    progress (smallest docs ingest quickly). max_blocks=0 = no upper limit.
    If require_content=True (default), skip all-empty bundles whose
    pipeline run would log no_chunks and never create a TaskRepo record.
    """
    candidates = []
    for b in sorted(corpus_root.iterdir()):
        jsonl = b / "data.jsonl"
        if not jsonl.exists():
            continue
        n_blocks = sum(1 for line in jsonl.read_text().splitlines() if line.strip())
        if n_blocks < min_blocks:
            continue
        if max_blocks > 0 and n_blocks > max_blocks:
            continue
        if not (b / "index.json").exists():
            continue
        if require_content and not _bundle_has_content(b):
            continue
        candidates.append((n_blocks, b))
    candidates.sort()
    return [b for _, b in candidates[:n]]


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
    """Poll /v1/ingestion/status until terminal or timeout.

    Terminal states: success, failed, rejected. "queued" / "processing"
    are transient async-pipeline states — keep polling. /v1/ingestion/notify
    returns HTTP 202 + status="queued" as the async-ack contract
    (parent §204 audit isolation); terminal is "success" once chunks finish.
    """
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
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"Max bundles to ingest (default {DEFAULT_LIMIT})")
    ap.add_argument("--min-blocks", type=int, default=0,
                    help="Min blocks per bundle filter (default 0 = no min)")
    ap.add_argument("--max-blocks", type=int, default=0,
                    help="Max blocks per bundle filter (0 = unlimited)")
    ap.add_argument("--version", type=int, default=DEFAULT_VERSION,
                    help=f"Notify version (default {DEFAULT_VERSION} = force re-ingest)")
    ap.add_argument("--retry", type=int, default=1,
                    help="Max retries per bundle on transient timeouts (default 1 — don't pile on a wedged pipeline)")
    ap.add_argument("--pace", type=float, default=SEQUENTIAL_PACE_S,
                    help=f"Seconds between bundles (default {SEQUENTIAL_PACE_S})")
    ap.add_argument("--status-timeout", type=float, default=STATUS_TIMEOUT_S,
                    help=f"Per-bundle status polling timeout (default {STATUS_TIMEOUT_S}s)")
    ap.add_argument("--notify-timeout", type=float, default=NOTIFY_TIMEOUT_S,
                    help=f"Per-attempt notify HTTP timeout (default {NOTIFY_TIMEOUT_S}s)")
    ap.add_argument("--checkpoint", type=Path,
                    default=Path("/tmp/task_d_checkpoint.json"),
                    help="Resume checkpoint file (default /tmp/task_d_checkpoint.json)")
    ap.add_argument("--reset-checkpoint", action="store_true",
                    help="Ignore existing checkpoint and start fresh")
    args = ap.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        print(f"FATAL: env var {args.token_env} not set", file=sys.stderr)
        return 2

    bundles = pick_bundles(CORPUS_ROOT, args.limit, args.min_blocks, args.max_blocks)
    print(f"Picked {len(bundles)} bundles (min={args.min_blocks}, max={args.max_blocks or '∞'}, sorted by size)")
    if len(bundles) < args.limit:
        print(f"  NOTE: only {len(bundles)} bundles available in corpus (asked for {args.limit})")

    # Load checkpoint
    if args.reset_checkpoint and args.checkpoint.exists():
        args.checkpoint.unlink()
    ckpt = load_checkpoint(args.checkpoint)
    completed = set(ckpt.get("completed", []))
    failed = ckpt.get("failed", [])
    if completed or failed:
        print(f"  Checkpoint: {len(completed)} completed, {len(failed)} failed (resume mode)")

    # Filter bundles
    pending = [b for b in bundles if b.name not in completed]
    print(f"  Pending after checkpoint filter: {len(pending)}")

    # Step 1: docker cp each pending bundle into /parsed_lib/
    print(f"\n=== Step 1: docker cp {len(pending)} bundles into {DOCKER_TARGET}:{SHARED_STORAGE_PATH}/ ===")
    for b in pending:
        ok, err = _docker_cp(b, SHARED_STORAGE_PATH)
        if not ok:
            print(f"  FAIL cp {b.name}: {err}", file=sys.stderr)
            return 3
    print(f"  All {len(pending)} bundles copied")

    # Step 2: send notification + poll status (with retry)
    print(f"\n=== Step 2: notify (v={args.version}) + poll ===")
    new_outcomes = []
    completed_iter = iter(completed)  # not used; just for clarity
    t_run_start = time.monotonic()
    for i, b in enumerate(pending, 1):
        doc_hash = b.name
        output_path = f"{SHARED_STORAGE_PATH}/{doc_hash}"
        trace_id = f"task-d-scale-{doc_hash[:8]}-{int(time.time())}"

        # Retry loop for transient failures (notify HTTP 0 / timeout)
        last_status = None
        last_body = None
        chunk_count = -1
        notify_code = 0
        for attempt in range(1, args.retry + 1):
            t0 = time.monotonic()
            notify_code, payload = send_notify(token, doc_hash, args.version, output_path, trace_id)
            if notify_code not in (200, 202):
                print(f"  [{i:3d}/{len(pending)}] {doc_hash[:12]}... NOTIFY FAIL HTTP={notify_code} (attempt {attempt}/{args.retry})", file=sys.stderr)
                last_body = payload
                time.sleep(args.pace)
                continue
            status, body = poll_status(token, doc_hash, args.status_timeout)
            dt = time.monotonic() - t0
            chunk_count = body.get("chunks_indexed", -1) if isinstance(body, dict) else -1
            last_status = status
            last_body = body
            if status == "success":
                print(f"  [{i:3d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → {status:9s} chunks={chunk_count}  ({dt:.1f}s)")
                break
            if status in ("failed", "rejected"):
                print(f"  [{i:3d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → {status:9s} (terminal, no retry) chunks={chunk_count}")
                break
            # status == "timeout" → retry
            if attempt < args.retry:
                print(f"  [{i:3d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → timeout (attempt {attempt}/{args.retry}, retrying)")
                time.sleep(args.pace)
            else:
                print(f"  [{i:3d}/{len(pending)}] {doc_hash[:12]}... HTTP={notify_code} → timeout (final, {args.retry} attempts exhausted)")

        new_outcomes.append((doc_hash, last_status or "rejected", chunk_count if isinstance(last_body, dict) else -1, last_body))

        # Update checkpoint after each bundle (durable resume point)
        if last_status == "success":
            completed.add(doc_hash)
        else:
            failed.append({"doc_hash": doc_hash, "status": last_status, "retries": args.retry})
        save_checkpoint(args.checkpoint, {
            "version": args.version,
            "limit": args.limit,
            "started_at": ckpt.get("started_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            "completed": sorted(completed),
            "failed": failed,
        })

        # Progress log every 10 bundles
        if i % 10 == 0:
            elapsed = time.monotonic() - t_run_start
            rate = i / elapsed if elapsed > 0 else 0
            eta_s = (len(pending) - i) / rate if rate > 0 else 0
            print(f"  --- progress: {i}/{len(pending)} | elapsed {elapsed:.0f}s | rate {rate:.2f} docs/s | ETA {eta_s:.0f}s ---")

        time.sleep(args.pace)

    # Step 3: summary
    print(f"\n=== Summary ===")
    n_success = sum(1 for _, s, *_ in new_outcomes if s == "success")
    n_failed = sum(1 for _, s, *_ in new_outcomes if s in ("failed", "rejected", "timeout"))
    total_chunks = sum(c for _, _, c, _ in new_outcomes if isinstance(c, int) and c > 0)
    elapsed_total = time.monotonic() - t_run_start
    print(f"  This run: {len(new_outcomes)}  Success: {n_success}  Failed/timeout: {n_failed}  Chunks: {total_chunks}")
    print(f"  Cumulative: {len(completed)}/{len(bundles)} ingested (all-time)")
    print(f"  Elapsed: {elapsed_total:.0f}s ({elapsed_total/60:.1f} min)")
    if n_failed:
        for h, s, c, body in new_outcomes:
            if s in ("failed", "rejected", "timeout"):
                print(f"    FAILED {h}: {s} body={body!r}")

    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())