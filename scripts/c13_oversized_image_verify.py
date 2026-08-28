#!/usr/bin/env python3
"""Phase 13c-C13 oversized_image_block D3 verify: coord doc §五 acceptance checks.

Reads the 2 re-output bundles (doc-to-md D2, merge_consecutive_image_blocks)
from SHARED_STORAGE_PATH, evaluates the static acceptance lines, optionally
folds bge-m3 encode latency from the D3 canary report (--canary-report),
and writes deployment/phase13c-oversized-image-d3-verify-report.json.

Acceptance (v0.4, MERGE_MAX_PER_BLOCK=5 per coord §八.Q5; thresholds calibrated
against D2 dry-run actuals 435/2781 with ~15% margin):
- image blocks: 8ab548bb51c076d0 <= 500 / ad58aff523d8d880 <= 3100
- raw_chars < 5,000,000 per bundle (D5-A admission hard gate, commit fe58d64)
- merge landed: >=1 composite block (metadata.merged_image_count) per bundle;
  every composite has merged_image_count in [MERGE_MIN_RUN..MERGE_MAX_PER_BLOCK]
  and len(merged_from_block_ids) == merged_image_count
- encode latency < 10s/bundle (only when --canary-report given; D3 run stage)
- jsonl bytes / total blocks: report-only (composite metadata overhead is a
  known D2 dry-run finding; admission gates on raw_chars, not file bytes)

Usage:
  python3 scripts/c13_oversized_image_verify.py
  python3 scripts/c13_oversized_image_verify.py \
      --canary-report deployment/phase13c-oversized-image-d3-canary-report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SHARED_STORAGE_PATH = os.environ.get("SHARED_STORAGE_PATH", "/mnt/disk/text/v1.1")
REPORT_PATH = Path(os.environ.get(
    "REPORT_PATH", "deployment/phase13c-oversized-image-d3-verify-report.json"))

MERGE_MIN_RUN = 3   # coord §3.1: >=3 consecutive same-heading images to merge
MERGE_MAX_PER_BLOCK = 5  # coord §八.Q5 confirmed (D2 dry-run data)
LATENCY_TARGET_S = 10.0  # coord §五: bge-m3 encode <10s/bundle (pre-fix 23-56s)
RAW_CHARS_ADMISSION = 5_000_000  # D5-A permanent gate

# Per-bundle gates + D2 dry-run pre/post actuals (2026-08-28, /tmp/d2_dry_run.json)
BUNDLES = {
    "8ab548bb51c076d0": {
        "image_blocks_max": 500,
        "dry_run": {"image_pre": 1984, "image_post": 435,
                    "blocks_pre": 3186, "blocks_post": 1637,
                    "jsonl_bytes_post": 14_255_052},
    },
    "ad58aff523d8d880": {
        "image_blocks_max": 3100,
        "dry_run": {"image_pre": 9624, "image_post": 2781,
                    "blocks_pre": 14451, "blocks_post": 7608,
                    "jsonl_bytes_post": 13_232_935},
    },
}


def scan_bundle(doc_hash: str) -> dict:
    """Single pass over data.jsonl: type counts, raw_chars, composite checks."""
    path = Path(SHARED_STORAGE_PATH) / doc_hash / "data.jsonl"
    if not path.exists():
        return {"doc_hash": doc_hash, "error": f"data.jsonl not found: {path}"}

    type_counts: dict[str, int] = {}
    raw_chars = 0
    jsonl_bytes = path.stat().st_size
    composites = 0
    composite_errors: list[str] = []
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            try:
                b = json.loads(line)
            except json.JSONDecodeError as e:
                composite_errors.append(f"line {lineno}: JSONDecodeError {e}")
                continue
            t = b.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
            content = b.get("content") or {}
            raw = content.get("raw") if isinstance(content, dict) else None
            raw_chars += len(raw) if isinstance(raw, str) else 0
            md = b.get("metadata") or {}
            if isinstance(md, dict) and "merged_image_count" in md:
                composites += 1
                n = md.get("merged_image_count")
                from_ids = md.get("merged_from_block_ids")
                if not isinstance(n, int) or not (MERGE_MIN_RUN <= n <= MERGE_MAX_PER_BLOCK):
                    composite_errors.append(
                        f"line {lineno}: merged_image_count={n!r} outside "
                        f"[{MERGE_MIN_RUN},{MERGE_MAX_PER_BLOCK}]")
                if not isinstance(from_ids, list) or len(from_ids) != n:
                    composite_errors.append(
                        f"line {lineno}: merged_from_block_ids len "
                        f"{len(from_ids) if isinstance(from_ids, list) else from_ids!r} != {n!r}")
    return {
        "doc_hash": doc_hash,
        "path": str(path),
        "blocks_total": sum(type_counts.values()),
        "type_counts": type_counts,
        "image_blocks": type_counts.get("image", 0),
        "raw_chars": raw_chars,
        "jsonl_bytes": jsonl_bytes,
        "composite_blocks": composites,
        "composite_errors": composite_errors[:20],
    }


def evaluate(doc_hash: str, scan: dict, latency_s: float | None) -> dict:
    """Fold scan + optional latency into per-bundle gate verdicts."""
    cfg = BUNDLES[doc_hash]
    dry = cfg["dry_run"]
    gates: dict[str, dict] = {}
    if "error" in scan:
        return {"doc_hash": doc_hash, "gates": {}, "pass": False,
                "error": scan["error"]}
    gates["image_blocks"] = {
        "value": scan["image_blocks"], "target": f"<={cfg['image_blocks_max']}",
        "dry_run_post": dry["image_post"], "pre": dry["image_pre"],
        "ok": scan["image_blocks"] <= cfg["image_blocks_max"],
    }
    gates["raw_chars_admission"] = {
        "value": scan["raw_chars"], "target": f"<{RAW_CHARS_ADMISSION}",
        "ok": scan["raw_chars"] < RAW_CHARS_ADMISSION,
    }
    merge_ok = scan["composite_blocks"] >= 1 and not scan["composite_errors"]
    gates["merge_landed"] = {
        "value": scan["composite_blocks"], "target": ">=1 composite, 0 structural errors",
        "errors": scan["composite_errors"], "ok": merge_ok,
    }
    if latency_s is not None:
        gates["encode_latency"] = {
            "value_s": latency_s, "target": f"<{LATENCY_TARGET_S}s", "ok": latency_s < LATENCY_TARGET_S,
        }
    return {
        "doc_hash": doc_hash,
        "gates": gates,
        "pass": all(g["ok"] for g in gates.values()),
        "deltas_vs_dry_run": {
            "image_blocks": {"pre": dry["image_pre"], "dry_post": dry["image_post"],
                             "now": scan["image_blocks"]},
            "blocks_total": {"pre": dry["blocks_pre"], "dry_post": dry["blocks_post"],
                             "now": scan["blocks_total"]},
        },
    }


def load_latencies(canary_report: Path | None) -> dict[str, float]:
    """Extract per-bundle ingest latency_s from the D3 canary report."""
    if canary_report is None:
        return {}
    data = json.loads(canary_report.read_text())
    return {r["doc_hash"]: float(r["latency_s"]) for r in data.get("results", [])
            if r.get("doc_hash") in BUNDLES and r.get("status") == "success"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canary-report", type=Path, default=None,
                    help="D3 canary report JSON; enables the encode-latency gate")
    args = ap.parse_args()

    latencies = load_latencies(args.canary_report)
    print(f"=== oversized_image_block D3 verify ===")
    print(f"  storage: {SHARED_STORAGE_PATH}")
    print(f"  canary report: {args.canary_report or '(static stage — latency gate deferred)'}")
    print()

    evaluations = []
    all_pass = True
    for doc_hash in BUNDLES:
        scan = scan_bundle(doc_hash)
        ev = evaluate(doc_hash, scan, latencies.get(doc_hash))
        ev["scan"] = {k: v for k, v in scan.items() if k != "composite_errors"}
        evaluations.append(ev)
        if "error" in ev:
            print(f"  {doc_hash}: ERROR {ev['error']}")
            all_pass = False
            continue
        s = ev["scan"]
        g = ev["gates"]
        print(f"  {doc_hash}:")
        print(f"    blocks={s['blocks_total']} (types={s['type_counts']})")
        print(f"    composites={s['composite_blocks']}  raw_chars={s['raw_chars']:,}"
              f"  jsonl={s['jsonl_bytes']:,}B")
        for name, gate in g.items():
            val = gate.get("value", gate.get("value_s"))
            print(f"    [{'PASS' if gate['ok'] else 'FAIL'}] {name}: {val} "
                  f"(target {gate['target']})")
        if not ev["pass"]:
            all_pass = False

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "shared_storage_path": SHARED_STORAGE_PATH,
        "canary_report": str(args.canary_report) if args.canary_report else None,
        "thresholds": {
            "merge_min_run": MERGE_MIN_RUN, "merge_max_per_block": MERGE_MAX_PER_BLOCK,
            "raw_chars_admission": RAW_CHARS_ADMISSION,
            "latency_target_s": LATENCY_TARGET_S,
            "image_blocks_max": {h: c["image_blocks_max"] for h, c in BUNDLES.items()},
        },
        "all_pass": all_pass,
        "bundles": evaluations,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print()
    print(f"=== Report written to {REPORT_PATH} ===")
    print(f"=== Acceptance: {'PASS' if all_pass else 'FAIL'} ===")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
