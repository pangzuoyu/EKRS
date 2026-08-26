"""Phase 13b T5 shared utilities — corpus discovery, notify/status helpers,
state reset (wipe). Reused by phase13b_poc_bench.py / equiv_check.py /
failover_test.py.

Extracted from scripts/live_stress_60.py:
- read_corpus: live_stress_60.py:511-548 (alphabetic first-N with non-empty
  data.jsonl filter)
- build_notify_payload: live_stress_60.py:632-645
- notify_one / poll_status: live_stress_60.py:653-689 / 794-832 (verbatim,
  zero behavioral change)

Plan: docs/superpowers/plans/2026-08-24-phase13b-T5-e2e-acceptance.md
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Reuse the live_stress_60 retry constants without depending on the module
# (which has side effects at import).
NOTIFY_HTTP_TIMEOUT_S = 60
STATUS_POLL_INTERVAL_S = 2.0
STATUS_TIMEOUT_S_DEFAULT = 90.0
NOTIFY_RETRY_MAX = 2
NOTIFY_RETRY_BACKOFF_S = (1.0, 2.0)

_TERMINAL_STATUSES = frozenset({"success", "completed", "failed"})


@dataclass
class DocOutcome:
    """Per-doc ingest result (notify + poll + chunk count).

    Mirrors live_stress_60.DocOutcome (lines 728-734) so callers can compare
    results across Phase A / Phase B / Phase A-replay.
    """

    doc_hash: str
    status: str = "pending"
    notify_ms: float = 0.0
    terminal_ms: float = 0.0
    chunks_indexed: int = 0
    failure_reason: Optional[str] = None


@dataclass
class PhaseReport:
    """Aggregated benchmark report for one phase (A or B).

    Returned by phase13b_poc_bench.run() so the T5.4 integration test can
    assert against typed properties (largest_doc_ms, gpu_memory_peak_bytes,
    etc.) without parsing JSON.
    """

    phase_name: str  # "A" or "B"
    n_docs: int
    n_success: int
    n_failed: int
    total_chunks: int
    p50_ms: float
    p99_ms: float
    largest_doc_ms: float
    largest_doc_chunks: int
    largest_doc_hash: str
    gpu_memory_peak_bytes: int  # 0 if N/A (CPU phase)
    doc_outcomes: list[DocOutcome] = field(default_factory=list)


@dataclass
class EquivReport:
    """T5.2 retrieval-equivalence summary."""

    n_compared: int  # doc × query pairs
    mean_top10_jaccard: float
    mean_cosine: float
    mean_sparse_jaccard: float
    n_recall_degraded: int  # doc × query where recall@10 dropped > 1pp


@dataclass
class FailoverReport:
    """T5.3 failover-test summary."""

    transition_detection_ms: float
    all_succeeded: bool
    at_least_one_cpu: bool
    recovery_detection_ms: float  # cpu→gpu re-emit timing


def _http(method: str, url: str, *, headers: dict, body: bytes | None = None,
          timeout: float = NOTIFY_HTTP_TIMEOUT_S) -> tuple[int, object]:
    """Minimal HTTP client — same contract as live_stress_60._http (lines 203-?).

    Returns (status_code, body). status_code=0 + body=exception-str on
    transport-layer failure (URLError/TimeoutError/OSError). HTTP 4xx/5xx
    still returns a real status code; the caller decides whether to retry.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return resp.status, {"raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, {"raw": raw.decode("utf-8", "replace")}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def discover_28_corpus(
    corpus_root: Path,
    fallback_path: Path | None = None,
) -> list[tuple[str, str, list[dict]]]:
    """Pick first 28 docs with non-empty data.jsonl.

    Returns list of (doc_id, file_name, blocks). Falls back to
    fallback_path (a newline-delimited list of doc_ids) if fewer than 28
    docs are discovered — the fallback is Phase 12 v10 verification's
    actual 28-doc ingest list (T5.1 risk #1 mitigation).

    Each dir in corpus_root is expected to be `corpus_root/<doc_hash>/`.
    Skips dirs without `data.jsonl` or with empty/JSON-invalid jsonl.
    """
    if not corpus_root.is_dir():
        raise FileNotFoundError(f"corpus_root not a directory: {corpus_root}")

    candidates: list[tuple[str, str, list[dict]]] = []
    for entry in sorted(corpus_root.iterdir()):
        if not entry.is_dir():
            continue
        jsonl_path = entry / "data.jsonl"
        if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
            continue
        blocks: list[dict] = []
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    blocks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not blocks:
            continue
        candidates.append((entry.name, jsonl_path.name, blocks))
        if len(candidates) >= 28:
            break

    if len(candidates) >= 28:
        return candidates

    # Fallback path — read a pre-curated list of doc_ids and look up their
    # dirs in corpus_root.
    if fallback_path is None or not fallback_path.exists():
        raise RuntimeError(
            f"discover_28_corpus: only {len(candidates)}/28 docs found "
            f"in {corpus_root} and no fallback list at {fallback_path}"
        )

    with fallback_path.open() as f:
        target_ids = [line.strip() for line in f if line.strip()]
    for doc_id in target_ids:
        entry = corpus_root / doc_id
        if not entry.is_dir():
            continue
        jsonl_path = entry / "data.jsonl"
        if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
            continue
        blocks = []
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    blocks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not blocks:
            continue
        candidates.append((entry.name, jsonl_path.name, blocks))
        if len(candidates) >= 28:
            break

    if len(candidates) < 28:
        raise RuntimeError(
            f"discover_28_corpus: only {len(candidates)}/28 docs after "
            f"fallback to {fallback_path}"
        )
    return candidates[:28]


def read_corpus(
    corpus_root: Path,
    n: int,
) -> list[tuple[str, str, list[dict]]]:
    """Pick N docs from corpus_root — alias for discover_28_corpus when
    caller wants a different count (T5.2 sample_n=20). Same logic.
    """
    if not corpus_root.is_dir():
        raise FileNotFoundError(f"corpus_root not a directory: {corpus_root}")
    candidates: list[tuple[str, str, list[dict]]] = []
    for entry in sorted(corpus_root.iterdir()):
        if not entry.is_dir():
            continue
        jsonl_path = entry / "data.jsonl"
        if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
            continue
        blocks: list[dict] = []
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    blocks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not blocks:
            continue
        candidates.append((entry.name, jsonl_path.name, blocks))
        if len(candidates) >= n:
            break
    return candidates


def build_notify_payload(
    doc_hash: str,
    output_path: Path,
    callback_url: str,
    version: int | None = None,
) -> dict:
    """Construct the /v1/ingestion/notify request body. Verbatim from
    live_stress_60.py:632-645.

    2026-08-26 Phase 13b PoC fix: default version is `int(time.time())` so
    each bench run uses a fresh (doc_hash, version) tuple — the notify
    handler's idempotency check returns 202 "duplicate" for repeated runs
    that reuse version=1, which trips the chunk-count threshold. Override
    with explicit version when callers want reproducible runs.
    """
    trace_id = f"trace_{doc_hash}"
    if version is None:
        version = int(time.time())
    return {
        "trace_id": trace_id,
        "doc_hash": doc_hash,
        "version": version,
        "output_path": str(output_path),
        "callback_url": callback_url,
    }


def notify_one(
    rag_url: str,
    token: str,
    payload: dict,
    *,
    retry_backoff_s: tuple[float, ...] = NOTIFY_RETRY_BACKOFF_S,
    timeout_s: float = NOTIFY_HTTP_TIMEOUT_S,
) -> tuple[int, dict, float]:
    """POST notify + retry on transport-level failures. Verbatim from
    live_stress_60.py:653-689.

    Returns (status_code, body, elapsed_ms).
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Parser-Token": token,
    }
    url = f"{rag_url}/v1/ingestion/notify"
    t0 = time.perf_counter()
    code, resp = 0, ""
    for attempt in range(NOTIFY_RETRY_MAX + 1):
        code, resp = _http("POST", url, headers=headers, body=body, timeout=timeout_s)
        if code != 0:
            break
        if attempt < NOTIFY_RETRY_MAX:
            backoff = retry_backoff_s[min(attempt, len(retry_backoff_s) - 1)]
            sys.stderr.write(
                f"[PH13b] notify TRANSIENT FAIL doc={payload.get('doc_hash','?')[:20]} "
                f"attempt={attempt + 1}/{NOTIFY_RETRY_MAX + 1} err={str(resp)[:80]} "
                f"backoff={backoff:.1f}s\n"
            )
            time.sleep(backoff)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if not isinstance(resp, dict):
        resp = {"raw": str(resp)}
    return code, resp, elapsed_ms


def poll_status(
    rag_url: str,
    doc_hash: str,
    timeout_s: float,
    *,
    poll_interval_s: float = STATUS_POLL_INTERVAL_S,
) -> tuple[str, float, Optional[str]]:
    """Poll /v1/ingestion/status/{doc_hash} until terminal. Verbatim from
    live_stress_60.py:794-832. Returns (status, elapsed_ms, reason)."""
    deadline = time.monotonic() + timeout_s
    start = deadline - timeout_s
    while True:
        code, body = _http(
            "GET", f"{rag_url}/v1/ingestion/status/{doc_hash}",
            headers={}, body=None, timeout=5.0,
        )
        if code == 200 and isinstance(body, dict):
            status = (body.get("status") or "").strip()
            if status in _TERMINAL_STATUSES:
                reason = body.get("error") or body.get("failure_reason")
                if reason is None and status == "failed":
                    reason = f"unknown failure (body={json.dumps(body)[:200]})"
                elapsed = (time.monotonic() - start) * 1000
                return status, elapsed, reason
            if code == 429:
                if time.monotonic() >= deadline:
                    return "rate_limited", (time.monotonic() - start) * 1000, \
                        "/status 429; deadline reached"
                time.sleep(poll_interval_s)
                continue
        elapsed = (time.monotonic() - start) * 1000
        if time.monotonic() >= deadline:
            return "pending", elapsed, "poll timeout"
        time.sleep(poll_interval_s)


def drain_all_pending(
    rag_url: str,
    doc_hashes: list[str],
    *,
    timeout_s: float = 300.0,
    force: bool = False,
    poll_interval_s: float = STATUS_POLL_INTERVAL_S,
    progress_every_s: float = 5.0,
) -> dict[str, tuple[str, float, Optional[str]]]:
    """Drain all docs to terminal state — eng-review fix #1 (T5.1).

    Polls /v1/ingestion/status for every doc_hash concurrently-ish
    (sequential is fine; we only have 28). Returns map of
    doc_hash → (status, elapsed_ms, reason). With `force=True`, returns
    immediately after the first round if any doc is still pending
    (operator override — risk #5: --force flag).
    """
    results: dict[str, tuple[str, float, Optional[str]]] = {}
    deadline = time.monotonic() + timeout_s
    last_progress = time.monotonic()

    def _still_pending() -> list[str]:
        return [
            h for h in doc_hashes
            if h not in results
            or results[h][0] not in _TERMINAL_STATUSES
        ]

    while _still_pending():
        for doc_hash in doc_hashes:
            if doc_hash in results and results[doc_hash][0] in _TERMINAL_STATUSES:
                continue
            status, elapsed, reason = poll_status(
                rag_url, doc_hash,
                timeout_s=max(0.1, deadline - time.monotonic()),
                poll_interval_s=poll_interval_s,
            )
            results[doc_hash] = (status, elapsed, reason)

        if force and _still_pending():
            break

        if time.monotonic() >= deadline:
            break

        if time.monotonic() - last_progress >= progress_every_s:
            succ = sum(1 for r in results.values() if r[0] in _TERMINAL_STATUSES - {"failed"})
            fail = sum(1 for r in results.values() if r[0] == "failed")
            pend = len(_still_pending())
            sys.stderr.write(
                f"[PH13b DRAIN] status: success={succ}/{len(doc_hashes)} "
                f"failed={fail} pending={pend}\n"
            )
            last_progress = time.monotonic()

        time.sleep(poll_interval_s)

    return results


def reset_state(
    *,
    qdrant_url: str,
    fts_path: Path,
    collection: str = "rag_documents",
    vector_size: int = 1024,
) -> None:
    """Wipe Qdrant collection + FTS SQLite file — T5.1/5.2 suggestion 1.

    Encapsulates the destructive clean-slate between Phase A and Phase B
    so callers don't sprinkle `docker compose down && up` glue. Safe to
    call when the collection is missing (delete is a no-op).

    After DELETE we PUT the collection back with the dense (bge-m3
    vector_size) + sparse schema. Without recreate, the pool worker's
    first upsert hits `Unexpected Response: 404 ... Collection
    rag_documents doesn't exist` (caught 2026-08-25: bench ran 4 min
    then 100% FAILED with qdrant_upsert_failed). ensure_collection runs
    only in uvicorn lifespan at startup, not per-upsert.

    2026-08-26 Phase 13b PoC: tasks.db is NOT wiped here — TaskRepo holds
    a long-lived sqlite3 connection and unlinking the file out from under
    it breaks notify handlers (500 on insert, no schema to insert into).
    Duplicate detection is avoided upstream via time-based payload versions
    (`build_notify_payload` defaults to `int(time.time())`), so each bench
    run gets a fresh (doc_hash, version) tuple.

    Raises on Qdrant error — caller decides whether to fall back to
    `docker compose down -v` (Phase 12 Task D fallback).
    """
    # Qdrant: DELETE collection (idempotent — 404 is OK).
    code, body = _http(
        "DELETE", f"{qdrant_url}/collections/{collection}",
        headers={}, body=None, timeout=10.0,
    )
    if code not in (200, 404):
        raise RuntimeError(f"Qdrant DELETE failed: code={code} body={body}")

    # Recreate with the same schema uvicorn uses in lifespan startup
    # (qdrant_client.py:154-170). PUT so we don't depend on Qdrant
    # internals; matches `QdrantManager.ensure_collection` output.
    # Body MUST be bytes (urllib rejects str) — encode here.
    create_body = json.dumps({
        "vectors": {
            "dense": {"size": vector_size, "distance": "Cosine"},
        },
        "sparse_vectors": {
            "sparse": {"index": {"on_disk": False}},
        },
    }).encode("utf-8")
    code, body = _http(
        "PUT", f"{qdrant_url}/collections/{collection}",
        headers={"Content-Type": "application/json"},
        body=create_body, timeout=10.0,
    )
    if code not in (200, 201, 409):
        raise RuntimeError(
            f"Qdrant PUT (recreate) failed: code={code} body={body}",
        )

    # FTS: unlink SQLite file (Phase 10 T10a-1 FTS5 db).
    if fts_path.exists():
        fts_path.unlink()


def gpu_memory_stats(
    rag_url: str,
    admin_key: str,
    *,
    timeout_s: float = 5.0,
) -> tuple[int, int]:
    """POST /v1/admin/gpu/memory-stats → (peak_bytes, allocated_bytes).

    Returns (0, 0) on 503 (CUDA unavailable / torch missing) so callers
    can distinguish "GPU not available" from "GPU has zero peak".
    Raises on 500 (driver fault — operator needs to investigate).
    """
    code, body = _http(
        "POST", f"{rag_url}/v1/admin/gpu/memory-stats",
        headers={"X-Admin-Key": admin_key},
        body=b"{}",
        timeout=timeout_s,
    )
    if code == 503:
        return 0, 0
    if code == 500:
        raise RuntimeError(f"gpu_memory_stats 500: {body}")
    if code != 200:
        raise RuntimeError(f"gpu_memory_stats unexpected {code}: {body}")
    if not isinstance(body, dict):
        return 0, 0
    return int(body.get("peak_bytes", 0)), int(body.get("allocated_bytes", 0))


def gpu_invalidate(rag_url: str, admin_key: str, *, timeout_s: float = 5.0) -> bool:
    """POST /v1/admin/gpu/invalidate — T5.3 trigger.

    Returns True on 200, False on 503 (router not yet initialized).
    """
    code, body = _http(
        "POST", f"{rag_url}/v1/admin/gpu/invalidate",
        headers={"X-Admin-Key": admin_key},
        body=b"{}",
        timeout=timeout_s,
    )
    return code == 200


def load_ground_truth(gt_path: Path) -> dict[str, dict[str, float]]:
    """Load T5.2 recall ground truth from deployment/phase12-recall-gt.json.

    Returns {doc_hash: {query: recall_float}}. Raises if the file is
    missing or not valid JSON (T5.2 fail-fast per eng-review fix #2).
    """
    if not gt_path.exists():
        raise FileNotFoundError(
            f"recall ground truth not found: {gt_path} (T5.2 fail-fast per "
            f"eng-review fix #2 — no doc-intrinsic fallback)"
        )
    with gt_path.open() as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"ground truth must be dict, got {type(data)}")
    return data


def percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile (0..100) of values. Returns 0.0 if empty."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (p / 100.0) * (len(s) - 1)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# Make env-var override helpers (T5.1 risk #4 — perf budget env).
def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))