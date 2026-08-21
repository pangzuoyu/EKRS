#!/usr/bin/env python3
"""Classify ingest-checkpoint failures into retry-safe vs real classes.

Phase 12 F2 (docs/superpowers/plans/2026-08-21-phase12-followups.md):
after the v10 batch completes, failures MUST be classified before any
retry — a blind full rerun buries real data defects inside the ~106
storm-window false failures.

Checkpoint schema (/tmp/ingest_new_v4_checkpoint.json):
    {"version": 2, "started_at": ..., "completed": [name, ...],
     "failed": [{"doc_hash", "status", "retries"}, ...]}
`failed[].status` is the LAST server status seen before the script gave
up: None → notify never reached a terminal state (storm/transient
candidate); "failed"/"rejected" → server-side pipeline verdict.

Classes:
    A  transient   status=None AND live status != success → notify retry
    B  mislabeled  status=None AND live status == success → server already
                   ingested; move to completed (or just skip — no retry)
    C  real        checkpoint status or live status in failed/rejected →
                   needs per-doc debugging (debug.log), human decision
    D  unknown     live status unreachable (server busy/down) → re-run later

The pending semantics of ingest_new_bundles.py (`pending = not in
completed`) retry EVERYTHING in failed, so A-only retry is done via a
filtered --include-list, not by mutating the checkpoint.

Usage:
    python3 scripts/classify_ingest_failures.py \
        --checkpoint /tmp/ingest_new_v4_checkpoint.json \
        [--check-status] [--token-env PARSER_TOKEN] \
        [--rag-url http://localhost:8000]

    --check-status  query GET /v1/ingestion/status/{doc_hash} per failed
                    doc (needs the token env; X-Parser-Token header).
                    Without it, classes A/B/D collapse — only the
                    checkpoint status is used (C still detected).

Outputs (stdout summary + files under /tmp):
    /tmp/fail_A_retry.json        bundle-name list → ingest --include-list
    /tmp/fail_B_mislabeled.txt    doc_hash list (server already has them)
    /tmp/fail_C_real.txt          doc_hash + reason list
    /tmp/fail_D_unknown.txt       doc_hash list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

A_RETRY = Path("/tmp/fail_A_retry.json")
B_MISLABELED = Path("/tmp/fail_B_mislabeled.txt")
C_REAL = Path("/tmp/fail_C_real.txt")
D_UNKNOWN = Path("/tmp/fail_D_unknown.txt")

TERMINAL = ("success", "failed", "rejected")


def load_failed(ckpt_path: Path) -> list[dict]:
    """Normalize failed entries to {doc_hash, status} dicts.

    Tolerates plain-string entries from older checkpoint formats.
    """
    ckpt = json.loads(ckpt_path.read_text())
    out: list[dict] = []
    for f in ckpt.get("failed", []):
        if isinstance(f, dict):
            out.append({
                "doc_hash": f.get("doc_hash") or f.get("name") or "",
                "status": f.get("status"),
            })
        else:
            out.append({"doc_hash": str(f), "status": None})
    return out


def live_status(rag_url: str, token: str, doc_hash: str) -> str:
    """GET /v1/ingestion/status/{doc_hash}; returns 'unreachable' on any
    transport error / non-200 so a busy server degrades to class D."""
    req = urllib.request.Request(
        f"{rag_url}/v1/ingestion/status/{doc_hash}",
        headers={"X-Parser-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return str(body.get("status", "unknown"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "unreachable"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--check-status", action="store_true")
    ap.add_argument("--token-env", default="PARSER_TOKEN")
    ap.add_argument("--rag-url", default="http://localhost:8000")
    args = ap.parse_args()

    token = os.environ.get(args.token_env, "") if args.check_status else ""
    if args.check_status and not token:
        print(f"FATAL: env {args.token_env} not set", file=sys.stderr)
        return 2

    failed = load_failed(args.checkpoint)
    a_retry: list[str] = []
    b_mislabeled: list[str] = []
    c_real: list[str] = []
    d_unknown: list[str] = []

    for i, f in enumerate(failed, 1):
        h, ckpt_status = f["doc_hash"], f["status"]
        if ckpt_status in ("failed", "rejected"):
            c_real.append(f"{h}\tcheckpoint_status={ckpt_status}")
            continue
        if not args.check_status:
            a_retry.append(h)  # cannot distinguish further without live status
            continue

        s = live_status(args.rag_url, token, h)
        if s == "success":
            b_mislabeled.append(h)
        elif s in ("failed", "rejected"):
            c_real.append(f"{h}\tlive_status={s}")
        elif s == "unreachable":
            d_unknown.append(h)
        else:  # queued / processing / unknown → notify never finished
            a_retry.append(h)
        if i % 25 == 0:
            print(f"  ... {i}/{len(failed)} checked", flush=True)
        time.sleep(0.2)

    A_RETRY.write_text(json.dumps(a_retry, indent=1))
    B_MISLABELED.write_text("\n".join(b_mislabeled) + ("\n" if b_mislabeled else ""))
    C_REAL.write_text("\n".join(c_real) + ("\n" if c_real else ""))
    D_UNKNOWN.write_text("\n".join(d_unknown) + ("\n" if d_unknown else ""))

    print(f"\nfailed total: {len(failed)}")
    print(f"  A transient (retry via --include-list): {len(a_retry)}  -> {A_RETRY}")
    print(f"  B mislabeled (server has them, skip):   {len(b_mislabeled)}  -> {B_MISLABELED}")
    print(f"  C real (human review):                  {len(c_real)}  -> {C_REAL}")
    print(f"  D unknown (re-run classifier later):    {len(d_unknown)}  -> {D_UNKNOWN}")
    if c_real:
        print("\nC-class details:")
        for line in c_real:
            print(f"  {line}")
    if not args.check_status:
        print("\nNOTE: --check-status omitted — A/B/D not separated; "
              "rerun with the flag once the batch is done and the server idle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
