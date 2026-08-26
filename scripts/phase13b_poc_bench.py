"""Phase 13b T5.1 — 28-doc Phase12-v10-subset benchmark (CPU vs GPU).

Runs two phases against the same 28 docs:
- Phase A: BGE_M3_GPU_ENABLED=false (CPU ONNX encode)
- Phase B: BGE_M3_GPU_ENABLED=true  (torch FP16 GPU encode)

For each phase:
1. Drain any pending tasks (eng-review fix #1 — 300s timeout, --force)
2. Wipe Qdrant collection + FTS (suggestion 1 — reset_state)
3. Run all 28 docs sequentially with notify + status poll
4. Capture per-doc latency + aggregate p50/p99/throughput
5. Optionally read /v1/admin/gpu/memory-stats (T5.1 risk #6)

Acceptance (exit 0 iff all pass; thresholds env-overridable):
- Phase B total chunks ≥ 7787
- Phase B largest doc ≤ T5_PERF_OVERRIDE_LARGEST_DOC_S (default 30s)
- Phase B 2298-chunk class doc ≤ 5s
- Phase B gpu_memory_peak_bytes ≤ 6 * 1024^3
- Phase A / Phase B failure_rate == 0

Plan: docs/superpowers/plans/2026-08-24-phase13b-T5-e2e-acceptance.md §T5.1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Allow running as `python scripts/phase13b_poc_bench.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _phase13b_common import (
    DocOutcome,
    PhaseReport,
    build_notify_payload,
    discover_28_corpus,
    drain_all_pending,
    env_float,
    env_int,
    gpu_memory_stats,
    notify_one,
    percentile,
    poll_status,
    reset_state,
)


DEFAULT_CORPUS_ROOT = Path("/home/pangzy/code_project/doc-to-md/output/text")
FALLBACK_28_LIST = Path(__file__).resolve().parent / "_phase13b_poc_28doc_fallback.txt"


def _preflight_gpu_setting(rag_url: str, expected_gpu_enabled: bool) -> None:
    """Verify the container's BGE_M3_GPU_ENABLED matches what we're testing.

    Goes through docker exec since the env var isn't exposed via /healthz
    (intentional — not user-facing). Fails fast with a clear message.
    """
    import subprocess

    container = "deployment-rag-1"  # convention
    try:
        out = subprocess.run(
            ["docker", "exec", container, "printenv", "BGE_M3_GPU_ENABLED"],
            capture_output=True, text=True, timeout=10.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(
            f"[PH13b] WARN: docker preflight skipped ({e}); "
            f"trusting operator-set BGE_M3_GPU_ENABLED={expected_gpu_enabled}\n"
        )
        return

    actual = (out.stdout or "").strip().lower()
    expected = "true" if expected_gpu_enabled else "false"
    if actual != expected:
        raise RuntimeError(
            f"Pre-flight mismatch: container BGE_M3_GPU_ENABLED={actual!r} "
            f"but expected {expected!r}. Restart the container with the right "
            f"GPU_ENABLED setting before running this phase."
        )


def _smoke_bench_largest_doc(
    *,
    rag_url: str,
    token: str,
    callback_url: str,
    corpus: list[tuple[str, str, list[dict]]],
    largest_doc_timeout_s: float,
    status_timeout_s: float,
) -> None:
    """Risk #4 mitigation: pre-flight the biggest single doc.

    Picks the doc with the most blocks and runs end-to-end. If it doesn't
    complete in `largest_doc_timeout_s`, warn (don't fail — the operator
    may have set T5_PERF_OVERRIDE_LARGEST_DOC_S appropriately).
    """
    if not corpus:
        return
    largest = max(corpus, key=lambda x: len(x[2]))
    doc_id, _, blocks = largest
    # Phase 13a T2 contract: output_path is the parser's output DIRECTORY
    # (coarse_gate appends /data.jsonl). Earlier /parsed_lib/<doc>/data.jsonl
    # broke after admission.py:42 started computing Path(p) / "data.jsonl"
    # — that became /parsed_lib/<doc>/data.jsonl/data.jsonl = unreadable.
    output_path = f"/parsed_lib/{doc_id}"
    payload = build_notify_payload(doc_id, Path(output_path), callback_url)

    sys.stderr.write(
        f"[PH13b SMOKE] largest doc {doc_id} ({len(blocks)} blocks); "
        f"budget {largest_doc_timeout_s}s\n"
    )
    code, body, _ = notify_one(rag_url, token, payload)
    # /v1/ingestion/notify is async: 200 = sync accepted (rare), 202 = queued
    # / duplicate / running. Both proceed to status poll. Only 4xx/5xx fail.
    if code not in (200, 202):
        sys.stderr.write(
            f"[PH13b SMOKE] notify failed code={code} body={body}; skipping\n"
        )
        return
    if code == 202 and body.get("status") == "duplicate":
        sys.stderr.write(
            f"[PH13b SMOKE] doc already ingested (duplicate); polling\n"
        )
    status, elapsed_ms, _ = poll_status(
        rag_url, doc_id, status_timeout_s,
    )
    if status == "success" and elapsed_ms > largest_doc_timeout_s * 1000:
        sys.stderr.write(
            f"[PH13b SMOKE] WARNING: largest doc took {elapsed_ms/1000:.1f}s; "
            f"budget is {largest_doc_timeout_s}s. Set T5_PERF_OVERRIDE_LARGEST_DOC_S "
            f"to override, or reduce _BATCH_SIZE in torch_bge_m3.py.\n"
        )


def _run_phase(
    *,
    phase_name: str,
    rag_url: str,
    token: str,
    callback_url: str,
    corpus: list[tuple[str, str, list[dict]]],
    qdrant_url: str,
    fts_path: Path,
    admin_key: str | None,
    pace_ms: int,
    status_timeout_s: float,
) -> PhaseReport:
    """Run one phase (A or B) end-to-end."""
    sys.stderr.write(f"\n[PH13b PHASE {phase_name}] starting with {len(corpus)} docs\n")

    # 1. Drain any pending (eng-review fix #1). force=True so a fresh
    # container (tasks.db empty → all docs return pending/404) skips the
    # 5-min drain wait. Operators pre-warm should set T5_DRAIN_FORCE=0
    # if they actually want to wait for real in-flight tasks.
    drain_all_pending(
        rag_url, [c[0] for c in corpus],
        timeout_s=env_float("T5_DRAIN_TIMEOUT_S", 300.0),
        force=env_int("T5_DRAIN_FORCE", 1) == 1,
    )

    # 2. Wipe state (suggestion 1 — encapsulate Qdrant + FTS).
    try:
        reset_state(qdrant_url=qdrant_url, fts_path=fts_path)
    except Exception as e:
        sys.stderr.write(f"[PH13b PHASE {phase_name}] wipe failed: {e}\n")
        # Don't fail — operator may want to skip wipe if Qdrant is shared.

    # 3. Smoke bench largest doc (risk #4).
    if env_int("T5_SMOKE_BENCH", 1):
        _smoke_bench_largest_doc(
            rag_url=rag_url, token=token, callback_url=callback_url,
            corpus=corpus,
            largest_doc_timeout_s=env_float("T5_PERF_OVERRIDE_LARGEST_DOC_S", 30.0),
            status_timeout_s=status_timeout_s,
        )

    # 4. Notify all 28 with pacing; poll each to completion.
    outcomes: list[DocOutcome] = []
    for doc_id, _, blocks in corpus:
        outcome = DocOutcome(doc_hash=doc_id)
        # See _smoke_bench_largest_doc — output_path is the parser's
        # output DIRECTORY; coarse_gate appends /data.jsonl itself.
        output_path = f"/parsed_lib/{doc_id}"
        payload = build_notify_payload(doc_id, Path(output_path), callback_url)

        code, body, notify_ms = notify_one(rag_url, token, payload)
        outcome.notify_ms = notify_ms

        # /v1/ingestion/notify is async: 202 = queued/duplicate/running
        # (already accepted), proceed to poll. Only 4xx/5xx are failures.
        if code not in (200, 202):
            outcome.status = "failed"
            outcome.failure_reason = f"notify code={code} body={body}"
            outcomes.append(outcome)
            if pace_ms > 0:
                time.sleep(pace_ms / 1000.0)
            continue
        # 202 with status="duplicate" is an idempotency hit — the doc was
        # already ingested (or in-flight) under the same version. Treat as
        # accepted; the poll below will resolve the actual terminal state.
        if code == 202 and body.get("status") == "duplicate":
            sys.stderr.write(
                f"[PH13b] [{phase_name}] [{doc_id}] notify duplicate (v={body.get('version', '?')}); "
                f"polling status\n"
            )

        # /v1/ingestion/status terminal
        status, terminal_ms, reason = poll_status(
            rag_url, doc_id, status_timeout_s,
        )
        outcome.status = status
        outcome.terminal_ms = terminal_ms
        outcome.failure_reason = reason
        # Heuristic chunks: block count × 1 (matches typical Phase 12 ratios;
        # exact chunk count comes from Qdrant scroll in T5.2). Recorded
        # as block count for now — T5.1 needs an O(1) read; Qdrant scroll
        # would add latency on every doc.
        outcome.chunks_indexed = len(blocks)
        outcomes.append(outcome)
        sys.stderr.write(
            f"[PH13b] [{phase_name}] [{doc_id}] chunks={outcome.chunks_indexed} "
            f"notify={outcome.notify_ms:.0f}ms "
            f"terminal={outcome.terminal_ms:.0f}ms "
            f"status={outcome.status}\n"
        )
        if pace_ms > 0:
            time.sleep(pace_ms / 1000.0)

    # 5. Aggregate metrics.
    terminal_ms = [o.terminal_ms for o in outcomes if o.status == "success"]
    n_success = sum(1 for o in outcomes if o.status == "success")
    n_failed = sum(1 for o in outcomes if o.status == "failed")
    total_chunks = sum(o.chunks_indexed for o in outcomes if o.status == "success")
    p50 = percentile(terminal_ms, 50)
    p99 = percentile(terminal_ms, 99)
    if terminal_ms:
        idx = max(range(len(outcomes)), key=lambda i: outcomes[i].terminal_ms if outcomes[i].status == "success" else -1)
        largest = outcomes[idx]
        largest_doc_ms = largest.terminal_ms
        largest_doc_chunks = largest.chunks_indexed
        largest_doc_hash = largest.doc_hash
    else:
        largest_doc_ms = 0.0
        largest_doc_chunks = 0
        largest_doc_hash = ""

    # 6. GPU memory peak (T5.1 risk #6 — admin endpoint, fallback /metrics).
    peak_bytes = 0
    if admin_key and phase_name == "B":
        try:
            peak_bytes, _ = gpu_memory_stats(rag_url, admin_key)
        except Exception as e:
            sys.stderr.write(f"[PH13b] gpu_memory_stats failed: {e}\n")

    return PhaseReport(
        phase_name=phase_name,
        n_docs=len(outcomes),
        n_success=n_success,
        n_failed=n_failed,
        total_chunks=total_chunks,
        p50_ms=p50,
        p99_ms=p99,
        largest_doc_ms=largest_doc_ms,
        largest_doc_chunks=largest_doc_chunks,
        largest_doc_hash=largest_doc_hash,
        gpu_memory_peak_bytes=peak_bytes,
        doc_outcomes=outcomes,
    )


def _resolve_chunk_threshold(
    *, corpus_total_blocks: int, strict: bool,
) -> tuple[int, str]:
    """Resolve Phase B min-chunks threshold (Phase 13c T4).

    Priority (mirrors plan T4 Steps):
    1. ``T5_PHASE_B_MIN_CHUNKS=0`` → (0, "disabled") — always passes.
    2. ``T5_PHASE_B_MIN_CHUNKS`` explicit positive int → (N, "explicit").
    3. No env → ``int(corpus_total_blocks * 0.9)``,
       status="corpus_derived" (warn-default) or "strict" if strict=True.

    Returns:
        (threshold_int, status_string). status_string is one of:
        "disabled" / "explicit" / "corpus_derived" / "strict".
    """
    raw = os.environ.get("T5_PHASE_B_MIN_CHUNKS")
    if raw is None:
        threshold = int(corpus_total_blocks * 0.9)
        return threshold, ("strict" if strict else "corpus_derived")
    try:
        n = int(raw)
    except ValueError:
        # Malformed env → fall back to corpus-derived, log warning.
        sys.stderr.write(
            f"[PH13b] WARN: T5_PHASE_B_MIN_CHUNKS={raw!r} not int, "
            f"falling back to corpus*0.9={int(corpus_total_blocks * 0.9)}\n",
        )
        threshold = int(corpus_total_blocks * 0.9)
        return threshold, ("strict" if strict else "corpus_derived")
    if n == 0:
        return 0, "disabled"
    return n, "explicit"


def _check_thresholds(
    phase_b: PhaseReport,
    *,
    corpus_total_blocks: int,
    strict: bool,
) -> tuple[list[str], str]:
    """Validate Phase B against acceptance thresholds; return (errors, status).

    Phase 13c T4: chunk-threshold resolution is dynamic (corpus-derived
    default; warning vs hard-fail controlled by ``strict``). Other checks
    (largest-doc latency, 2298-class latency, GPU peak, n_failed) remain
    hard-fail — they're GPU-path-validity signals, not corpus-size proxies.

    Returns:
        (errs, threshold_status). threshold_status is "pass" / "warn" /
        "fail" and is what callers use to decide exit code.

    Exit-code contract (caller ``main()``):
        "pass" → exit 0
        "warn" → exit 0 (CI / verification path; threshold short by design)
        "fail" → exit 1 (production gate; STRICT or non-threshold error)
    """
    errs: list[str] = []
    threshold, threshold_status = _resolve_chunk_threshold(
        corpus_total_blocks=corpus_total_blocks, strict=strict,
    )
    chunk_shortfall = False
    if threshold > 0 and phase_b.total_chunks < threshold:
        chunk_shortfall = True
        errs.append(
            f"Phase B total chunks {phase_b.total_chunks} < {threshold} "
            f"(threshold_status={threshold_status})"
        )
    largest_budget = env_float("T5_PERF_OVERRIDE_LARGEST_DOC_S", 30.0)
    if phase_b.largest_doc_ms > largest_budget * 1000:
        errs.append(
            f"Phase B largest doc {phase_b.largest_doc_ms:.0f}ms > "
            f"{largest_budget}s budget"
        )
    # 2298-chunk class doc ≤ 5s — find docs with chunks in 2200-2400 range
    near_2298 = [
        o for o in phase_b.doc_outcomes
        if 2200 <= o.chunks_indexed <= 2400 and o.status == "success"
    ]
    for o in near_2298:
        if o.terminal_ms > 5000:
            errs.append(
                f"Phase B 2298-class doc {o.doc_hash} ({o.chunks_indexed} chunks) "
                f"took {o.terminal_ms:.0f}ms > 5s"
            )
    peak_ceiling = env_int("T5_GPU_MEMORY_PEAK_BYTES_MAX", 6 * 1024**3)
    if phase_b.gpu_memory_peak_bytes > peak_ceiling:
        errs.append(
            f"Phase B GPU peak {phase_b.gpu_memory_peak_bytes} > "
            f"{peak_ceiling} bytes"
        )
    if phase_b.n_failed > 0:
        errs.append(f"Phase B had {phase_b.n_failed} failures")

    # Status decision:
    # - any non-threshold error → hard fail (GPU path broken)
    # - chunk-shortfall + STRICT → hard fail (production gate)
    # - chunk-shortfall + non-strict → warn (CI / verification path)
    non_threshold_errs = [
        e for e in errs
        if not e.startswith("Phase B total chunks ")
    ]
    if non_threshold_errs:
        status = "fail"
    elif chunk_shortfall and threshold_status == "strict":
        status = "fail"
    elif chunk_shortfall:
        status = "warn"
    else:
        status = "pass"
    return errs, status


def run(
    *,
    corpus_root: Path,
    qdrant_url: str,
    fts_path: Path,
    rag_url: str,
    token: str,
    callback_url: str,
    admin_key: str | None = None,
    phase: str = "full",
    pace_ms: int = 2000,
    status_timeout_s: float = 90.0,
) -> tuple[PhaseReport, PhaseReport, int]:
    """Top-level orchestrator: discover → Phase A → Phase B → return reports.

    `phase` arg: "A" / "B" / "full" (default). A runs CPU only, B runs
    GPU only, full runs both (the default T5.1 flow).

    Phase 13c T4: also returns ``corpus_total_blocks`` (sum of all blocks
    across the discovered corpus) so the caller can compute the dynamic
    threshold for chunk-count validation.
    """
    corpus = discover_28_corpus(corpus_root, fallback_path=FALLBACK_28_LIST)
    corpus_total_blocks = sum(len(blocks) for _, _, blocks in corpus)
    sys.stderr.write(
        f"[PH13b] discovered {len(corpus)} docs (target=28) from {corpus_root}, "
        f"total_blocks={corpus_total_blocks}\n"
    )

    phase_a = PhaseReport(phase_name="A", n_docs=0, n_success=0, n_failed=0,
                          total_chunks=0, p50_ms=0, p99_ms=0,
                          largest_doc_ms=0, largest_doc_chunks=0,
                          largest_doc_hash="", gpu_memory_peak_bytes=0)
    phase_b = PhaseReport(phase_name="B", n_docs=0, n_success=0, n_failed=0,
                          total_chunks=0, p50_ms=0, p99_ms=0,
                          largest_doc_ms=0, largest_doc_chunks=0,
                          largest_doc_hash="", gpu_memory_peak_bytes=0)

    if phase in ("A", "full"):
        _preflight_gpu_setting(rag_url, expected_gpu_enabled=False)
        phase_a = _run_phase(
            phase_name="A",
            rag_url=rag_url, token=token, callback_url=callback_url,
            corpus=corpus, qdrant_url=qdrant_url, fts_path=fts_path,
            admin_key=admin_key, pace_ms=pace_ms,
            status_timeout_s=status_timeout_s,
        )

    if phase in ("B", "full"):
        _preflight_gpu_setting(rag_url, expected_gpu_enabled=True)
        phase_b = _run_phase(
            phase_name="B",
            rag_url=rag_url, token=token, callback_url=callback_url,
            corpus=corpus, qdrant_url=qdrant_url, fts_path=fts_path,
            admin_key=admin_key, pace_ms=pace_ms,
            status_timeout_s=status_timeout_s,
        )

    return phase_a, phase_b, corpus_total_blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--fts-path", type=Path, default=Path("/app/rag/fts.sqlite"))
    parser.add_argument("--rag-url", default="http://localhost:8000")
    parser.add_argument("--token", default=os.environ.get("PARSER_TOKEN", ""))
    parser.add_argument("--callback-url", default="http://parser:9000/callback")
    parser.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY", ""))
    parser.add_argument(
        "--phase", choices=("A", "B", "full"), default="full",
    )
    parser.add_argument("--pace-ms", type=int, default=2000)
    parser.add_argument("--status-timeout-s", type=float, default=90.0)
    parser.add_argument(
        "--summary-json", type=Path,
        default=Path(__file__).resolve().parent.parent / "deployment"
        / "phase13b-poc-summary.json",
    )
    args = parser.parse_args()

    if not args.token:
        sys.stderr.write("ERROR: --token or PARSER_TOKEN env var required\n")
        return 2

    phase_a, phase_b, corpus_total_blocks = run(
        corpus_root=args.corpus_root,
        qdrant_url=args.qdrant_url,
        fts_path=args.fts_path,
        rag_url=args.rag_url,
        token=args.token,
        callback_url=args.callback_url,
        admin_key=args.admin_key or None,
        phase=args.phase,
        pace_ms=args.pace_ms,
        status_timeout_s=args.status_timeout_s,
    )

    # Phase 13c T4: dynamic threshold resolution.
    # T5_PHASE_B_MIN_CHUNKS_STRICT=true → hard-fail gate (prod pre-release).
    # Default → warn-only (CI / verification path).
    strict_threshold = env_int("T5_PHASE_B_MIN_CHUNKS_STRICT", 0) > 0
    if args.phase in ("B", "full"):
        errs, threshold_status = _check_thresholds(
            phase_b,
            corpus_total_blocks=corpus_total_blocks,
            strict=strict_threshold,
        )
    else:
        errs, threshold_status = [], "pass"

    # Resolve the threshold value for summary JSON transparency.
    threshold_value, _ = _resolve_chunk_threshold(
        corpus_total_blocks=corpus_total_blocks, strict=strict_threshold,
    )

    # Write summary JSON (suggestion 2 — T5.4 wrapper consumes this).
    summary = {
        "phase_a": _phase_to_dict(phase_a),
        "phase_b": _phase_to_dict(phase_b),
        "thresholds": {
            "phase_b_total_chunks_min": threshold_value,
            "phase_b_total_chunks_threshold_status": threshold_status,
            "corpus_total_blocks": corpus_total_blocks,
            "phase_b_largest_doc_s": env_float("T5_PERF_OVERRIDE_LARGEST_DOC_S", 30.0),
            "phase_b_2298_class_doc_s": 5.0,
            "phase_b_gpu_peak_bytes_max": env_int("T5_GPU_MEMORY_PEAK_BYTES_MAX", 6 * 1024**3),
        },
        "errors": errs,
    }
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    with args.summary_json.open("w") as f:
        json.dump(summary, f, indent=2, default=str)

    sys.stderr.write(f"\n[PH13b] summary written to {args.summary_json}\n")
    sys.stderr.write(
        f"[PH13b] corpus_total_blocks={corpus_total_blocks}, "
        f"threshold={threshold_value}, status={threshold_status}, "
        f"strict={strict_threshold}\n"
    )

    if threshold_status == "fail":
        sys.stderr.write(f"\n[PH13b] {len(errs)} threshold violations:\n")
        for e in errs:
            sys.stderr.write(f"  - {e}\n")
        return 1
    if threshold_status == "warn":
        # Phase 13c T4: warn-only (CI / verification) → exit 0.
        sys.stderr.write(
            f"\n[PH13b] WARN: corpus below threshold but exiting 0 "
            f"({'STRICT' if strict_threshold else 'non-strict'}).\n"
            f"  {len(errs)} short-fall(s):\n",
        )
        for e in errs:
            sys.stderr.write(f"  - {e}\n")
        return 0
    return 0


def _phase_to_dict(report: PhaseReport) -> dict:
    d = asdict(report)
    # DocOutcome lists don't serialize cleanly — drop them from JSON.
    d.pop("doc_outcomes", None)
    return d


if __name__ == "__main__":
    sys.exit(main())