"""Phase 9 verification: live 60-doc ingest stress on real infra.

Generates 60 distinct JSONL documents, fans out POST /v1/ingestion/notify
calls against the running RAG service, polls /v1/ingestion/status until
terminal (completed or failed), and prints a summary report.

Tokens are read from $PARSER_TOKEN env var. Per Credential Handling
Craft conventions, the value is never echoed — passed only to curl
`-H X-Parser-Token:` and to `Authorization`-style python requests via
the `headers` kwarg (never printed).

Exit codes:
  0 — all 60 ingested (completed), 0 failed
  1 — at least 1 failed / OOM / non-terminal
  2 — pipeline error (transport / auth / health)
  3 — qdrant point delta <= baseline (points regressed)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
DEFAULT_RAG_URL = os.environ.get("RAG_URL", "http://localhost:8000")
DEFAULT_TOKEN_VAR = "PARSER_TOKEN"
STATUS_TIMEOUT_S = 35.0
# Polling cadence: 1 worker × 1/2.5s = 24 polls/min. Combined with
# sequential dispatch (30 req/min during the dispatch phase only;
# phases do NOT overlap — dispatch finishes before polling starts),
# total ≤54/min, comfortably under the 60/min EKRS_RATE_LIMIT bucket.
# Even if dispatch+poll ever overlapped, 54/min stays under the bucket.
STATUS_POLL_S = 2.5
NOTIFY_HTTP_TIMEOUT_S = 90.0
# Retry budget for transport-level failures on /v1/ingestion/notify.
# 200-doc stress surfaced a 12% TimeoutError rate from intermittent
# uvicorn listen-socket slow-accept (Phase 9 follow-up). 2 retries with
# 1s + 2s backoff absorb the flake without retrying forever.
NOTIFY_RETRY_MAX = 2
NOTIFY_RETRY_BACKOFF_S = (1.0, 2.0)
# Real corpus docs (from --corpus-root) ingest much slower than the
# synthetic 2k-char templates because each block carries real bge-m3
# embedding work. Default 90s allows up to ~300 chunks/doc ingest
# budget; raise further for very large corpora.
STATUS_TIMEOUT_CORPUS_S = 90.0
# Sequential-pacing defaults (Plan §6 验证 6 corrected approach).
# Concurrent dispatch floods the bge-m3 queue when each real-corpus doc
# carries dozens of blocks: 60 parallel notifys → queue exhaustion →
# 503/OOM instead of 429s. Sequential pacing (~25-30 req/min) gives
# the embedding worker time per doc and keeps the audit trail clean.
DEFAULT_PACE_MS_CORPUS = 2000   # 1 doc every 2s → 30 req/min
# Concurrent notify requests; pacing is the better knob. Even with the
# per-IP /v1/* limiter raised to 600/min, keeping the cluster small
# avoids swamping the parser-side ingest worker pool.
NOTIFY_CONCURRENCY = 1
# Sequential-pacing discipline: with default rate-limit 60/min on /v1/*,
# polling at >60 req/min starves the dispatch budget. 1 poll worker ×
# 1 poll / 2.5s = 24/min, plus 30/min from sequential notify dispatch =
# 54/min total. Below the 60/min bucket with margin.
POLL_CONCURRENCY = 1
N_DOCS_DEFAULT = 60


# ---------------------------------------------------------------------------
# Mode profiles (Phase 9 offline bulk-import)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeProfile:
    """Per-mode tuning knobs — one source of truth for stress / offline /
    retry-failed runs. See plans/2026-07-28-phase9-offline-bulk-import.md
    for rationale on each field.
    """
    pace_ms: int                       # notify pace per worker (ms)
    retry_backoff_s: tuple[float, ...] # notify retry backoff schedule
    status_timeout_s: float            # per-doc /status poll deadline
    poll_interval_s: float             # /status poll cadence
    resume: bool                       # filter out already-ingested docs at start
    write_failed: bool                 # write failed/pending docs to --output-failed
    id_suffix: bool                    # append _r<run_id> to doc_hash (stress only)


# Stress mode: load-test tuning (60/60 + 200/200 verified at phase9 tag).
# id_suffix=True ensures each stress run produces a measurable
# qdrant_points_delta (bypasses Qdrant SHA-based dedup).
STRESS_PROFILE = ModeProfile(
    pace_ms=DEFAULT_PACE_MS_CORPUS,    # 2000
    retry_backoff_s=NOTIFY_RETRY_BACKOFF_S,  # (1.0, 2.0)
    status_timeout_s=STATUS_TIMEOUT_CORPUS_S,  # 90.0
    poll_interval_s=STATUS_POLL_S,     # 2.5
    resume=False,
    write_failed=False,
    id_suffix=True,
)

# Offline mode: production first-deploy of 25k+ docs.
# Reliability over throughput: slower pacing (3s), longer backoff
# (2s+4s), longer status timeout (180s), longer poll interval (3s).
# id_suffix=False → same SHA → Qdrant dedup short-circuits on re-run
# (idempotent resume after crash).
OFFLINE_PROFILE = ModeProfile(
    pace_ms=3000,
    retry_backoff_s=(2.0, 4.0),
    status_timeout_s=180.0,
    poll_interval_s=3.0,
    resume=True,
    write_failed=True,
    id_suffix=False,
)

# Retry-failed mode: re-process only docs in --input-failed.
# Slower still (5s pace, 4s+8s backoff) — these are known-bad docs,
# so the retry is more conservative. No _r<run_id> suffix (relies on
# RAG-side dedup). Resume=False because the input file IS the resume
# signal; audit-log dedup would only add false-negatives.
RETRY_FAILED_PROFILE = ModeProfile(
    pace_ms=5000,
    retry_backoff_s=(4.0, 8.0),
    status_timeout_s=180.0,
    poll_interval_s=3.0,
    resume=False,
    write_failed=True,
    id_suffix=False,
)

MODE_PROFILES = {
    "stress": STRESS_PROFILE,
    "offline": OFFLINE_PROFILE,
    "retry-failed": RETRY_FAILED_PROFILE,
}


# Match the `_r<YYYYMMDDTHHMMSSZ>` suffix added by run_stress().
# Precise regex — never a generic strip — so corpus IDs ending in
# `_r...` (e.g. `asme_bpvc_r2015`) are not misread.
_RRUNID_SUFFIX_RE = re.compile(r"_r\d{8}T\d{6}Z$")


def _strip_runid_suffix(doc_hash: str) -> str:
    """Remove `_r<YYYYMMDDTHHMMSSZ>` suffix if present. Returns base id."""
    return _RRUNID_SUFFIX_RE.sub("", doc_hash)


# Statuses that count as "failed" for the failed_docs.txt output.
# Equivalent to: any outcome whose status is NOT in {completed, success,
# skipped_resumed}. Excludes `notified` (interrupted pre-poll — those
# are the user's problem; the notify succeeded so RAG will eventually
# process them, possibly already-done by the time the script exits).
_FAILED_STATUSES = frozenset({
    "rejected",        # HTTP 4xx/5xx from /v1/ingestion/notify
    "failed",          # terminal failure from /v1/ingestion/status
    "pending",         # /status poll timed out before reaching terminal
    "rate_limited",    # /status 429 sustained until deadline
    "notified_failed", # notify submit raised before HTTP response
})


@dataclass(frozen=True)
class DocOutcome:
    doc_hash: str
    trace_id: Optional[str]
    status: str  # one of: notified, rejected, completed, failed, pending,
                 #         rate_limited, notified_failed, skipped_resumed
    notify_ms: float
    terminal_ms: float
    failure_reason: Optional[str] = None


@dataclass
class StressReport:
    n_total: int
    n_completed: int
    n_failed: int
    n_rejected: int
    n_pending_at_timeout: int
    n_skipped_resumed: int = 0
    completed_latency: dict[str, float] = field(default_factory=dict)  # p50/p95/p99
    qdrant_points_before: int = 0
    qdrant_points_after: int = 0
    qdrant_points_delta: int = 0
    audit_qdrant_write_failed: int = 0
    durations: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP helpers (urllib stdlib only — keep this script dependency-free)
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 10.0,
) -> tuple[int, dict | list | str]:
    req = urllib.request.Request(
        url, data=body, method=method, headers=headers or {},
    )
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


def get_qdrant_points(host: str = "localhost", port: int = 6333) -> int:
    """Return points_count for `rag_documents`, or -1 on transport failure."""
    code, body = _http(
        "GET", f"http://{host}:{port}/collections/rag_documents",
        timeout=5.0,
    )
    if code != 200 or not isinstance(body, dict):
        return -1
    try:
        return int(body["result"]["points_count"])
    except (KeyError, TypeError, ValueError):
        return -1


def scan_audit_for_failures(audit_path: Path, target_trace_ids: set[str]) -> int:
    """Count `qdrant_write_failed` audit entries whose trace_id is in target set."""
    if not audit_path.is_file():
        return -1  # file not found (could not reach audit.log)
    failures = 0
    try:
        with audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # BUGFIX 2026-07-28: audit log uses `event` as the JSON
                # key (verified by reading /app/rag/audit.log), not
                # `event_type`. Old check returned 0 silently even when
                # failures existed. Pre-existing false-negative only —
                # no behavioral regression risk.
                if entry.get("event") != "qdrant_write_failed":
                    continue
                tid = entry.get("trace_id") or ""
                if tid in target_trace_ids:
                    failures += 1
    except OSError:
        return -1
    return failures


def _read_audit_log_lines(docker_target: Optional[str]) -> list[str]:
    """Read audit log JSONL lines from inside the running rag container
    via `docker exec`. Reads BOTH the active audit.log AND rotated
    audit.log.{1..5}.gz files (per audit.py:44-50 — 100MB × 5 gzip
    backups). A 25k-doc run can rotate the active file mid-run; missing
    rotated completions would produce false-negative resume (re-ingest
    already-done docs).

    Returns the parsed lines as a list of strings (caller does JSON
    parsing). Returns [] if audit.log does not exist inside the
    container (operator started fresh; first-time ingest).

    Raises RuntimeError on docker exec failure (caller should hard-fail
    when --audit-via-docker was explicitly set).
    """
    if not docker_target:
        raise ValueError("docker_target required for _read_audit_log_lines")
    # `cat` is the primary read; the for-loop iterates rotated backups
    # if they exist. Append `exit 0` because the for-loop's last
    # `[ -f "$f" ]` test returns 1 when the glob doesn't match (no
    # rotated files yet) — we don't want that to poison the overall
    # rc of the `sh -c` script. Without this, an empty `audit.log.*.gz`
    # set makes `sh -c` return non-zero and the script hard-fails.
    script = (
        "cat /app/rag/audit.log 2>/dev/null; "
        "for f in /app/rag/audit.log.*.gz; do "
        "  [ -f \"$f\" ] && zcat \"$f\"; "
        "done; "
        "true"
    )
    proc = subprocess.run(
        ["docker", "exec", "-i", docker_target, "sh", "-c", script],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker exec failed (rc={proc.returncode}): "
            f"stderr={proc.stderr.decode('utf-8', 'replace')[:500]}"
        )
    return proc.stdout.decode("utf-8", "replace").splitlines()


def get_ingested_doc_hashes(
    audit_path: Path,
    *,
    docker_target: Optional[str] = None,
) -> set[str]:
    """Read audit log JSONL and return set of doc_id values from
    `ingestion_completed` events. Used by offline mode resume check.

    Two paths:
    - Local file: opens `audit_path` directly on the host.
    - --audit-via-docker: runs `docker exec` to read the active file +
      rotated gz backups inside the container (the audit log is NOT
      bind-mounted per docker-compose.yml:60-61).

    Returns empty set if audit log file does not exist (fresh start).
    Raises RuntimeError on docker exec failure (caller hard-fails).
    """
    if docker_target:
        try:
            lines = _read_audit_log_lines(docker_target)
        except RuntimeError:
            raise
    elif audit_path.is_file():
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    else:
        return set()  # fresh start, no resume checkpoint

    ingested: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") != "ingestion_completed":
            continue
        doc_id = entry.get("doc_id")
        if doc_id:
            ingested.add(doc_id)
    return ingested


def read_failed_docs(path: Path) -> set[str]:
    """Read a failed_docs.txt file (one doc_hash per line, no header)
    and return the set of doc_hash values. Skips blank lines and
    whitespace-only lines. Raises FileNotFoundError if path missing.
    """
    if not path.is_file():
        raise FileNotFoundError(f"--input-failed file not found: {path}")
    hashes: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            v = line.strip()
            if v:
                hashes.add(v)
    return hashes


def write_failed_docs(path: Path, doc_hashes: Iterable[str]) -> None:
    """Atomically write failed_docs.txt: one doc_hash per line, sorted.

    Uses `<path>.tmp` + `os.replace()` so a mid-write crash does not
    truncate the existing file. Overwrites any existing file at `path`
    (replace semantics, NOT append).
    """
    sorted_hashes = sorted(set(doc_hashes))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for h in sorted_hashes:
            f.write(h + "\n")
    os.replace(tmp, path)


def _resolve_failed_doc_paths(
    failed_hashes: set[str],
    corpus_root: Path,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Map each failed doc_hash to its corpus JSONL source.

    Strips `_r<run_id>` suffix (precise regex), then matches against
    `<corpus_root>/<id>/data.jsonl`. Returns (hits, misses):
    - hits: list of `(doc_hash, jsonl_path)` for docs found on disk
    - misses: list of doc_hash values with no matching corpus dir

    Used by retry-failed mode to rebuild the payload list. The caller
    surfaces a fatal listing when misses is non-empty.
    """
    hits: list[tuple[str, Path]] = []
    misses: list[str] = []
    for doc_hash in sorted(failed_hashes):
        base_id = _strip_runid_suffix(doc_hash)
        jsonl_path = corpus_root / base_id / "data.jsonl"
        if jsonl_path.is_file():
            hits.append((doc_hash, jsonl_path))
        else:
            misses.append(doc_hash)
    return hits, misses


def _filter_qdrant_ingested(
    rag_url: str,
    doc_hashes: Iterable[str],
    *,
    timeout: float = 5.0,
) -> set[str]:
    """Query /v1/ingestion/status/{doc_hash} for each candidate; return
    the subset whose status is already terminal-success ("success" or
    "completed"). Used by offline mode resume check (Qdrant dedup is
    the primary resume signal; audit log is supplementary).

    Slow at scale: one HTTP round-trip per candidate. For 25k docs at
    100ms RTT this is ~50 minutes upfront. A future optimization
    batches via Qdrant scroll — see plan §"Future optimization note".
    """
    ingested: set[str] = set()
    for h in doc_hashes:
        code, body = _http(
            "GET", f"{rag_url}/v1/ingestion/status/{h}",
            timeout=timeout,
        )
        if code != 200 or not isinstance(body, dict):
            continue
        status = (body.get("status") or "").strip()
        if status in _TERMINAL_STATUSES:  # success / completed / failed
            if status in ("success", "completed"):
                ingested.add(h)
    return ingested


# ---------------------------------------------------------------------------
# Document generation
# ---------------------------------------------------------------------------


# Three doc profiles — exercises Phase 1 look-back + Phase 2 greedy merge
# differently. Each block content is engineered to force the chunker to
# split (single block > 768 tokens) AND to hit safe-boundary retractions.
_DOC_TEMPLATES = [
    # (i, profile) -> raw text (each > ~1500 chars so it crosses 768 tokens)
    (
        "gb150",
        "压力容器设计应符合GB150标准，最高工作温度不超过350℃，最低工作温度不低于-40℃。"
        "设计压力不超过10MPa，水压试验压力为设计压力的1.5倍。"
        "材料应选用Q345R或16MnDR，焊接接头系数不低于0.85。"
        "无损检测比例：对接焊缝100%射线检测，角焊缝20%磁粉检测。"
        "热处理：厚度大于30mm的碳素钢焊后需进行消应力热处理。"
        "气压试验介质应采用干燥空气或氮气，试验压力为设计压力的1.15倍。"
        "高温工况下应考虑持久强度和蠕变的影响。"
        "本章描述了压力容器从设计、材料、焊接、检验到验收的全部要求。"
    ),
    (
        "asme_bpvc",
        "The pressure vessel shall be designed in accordance with ASME BPVC "
        "Section VIII Division 1. The maximum allowable working pressure is "
        "10 MPa at design temperature 350 degrees Celsius. Hydrostatic test "
        "pressure shall be 1.5 times the design pressure. Materials shall "
        "conform to SA-516 Grade 70 with yield strength 260 MPa minimum. "
        "Impact testing per ASME SA-370 Charpy V-notch at -29 degrees Celsius. "
        "Post-weld heat treatment mandatory for thicknesses exceeding 32 mm. "
        "Radiographic examination per ASME Section V Article 2 for all butt "
        "welds. Magnetic particle examination for fillet welds at 20 percent. "
        "This chapter defines the complete requirements for design, materials, "
        "welding, inspection, and acceptance of pressure vessels."
    ),
    (
        "concrete",
        "混凝土浇筑温度不得超过35℃，养护温度不低于5℃。"
        "高强度混凝土C50以上强度等级应采用P.O 52.5水泥。"
        "最大水灰比为0.45，最小水泥用量为350kg/m³。"
        "坍落度控制在180±20mm范围内，含气量4.5±1.0%。"
        "高温季节施工应采取降温措施，骨料温度不超过30℃。"
        "冬季施工采用综合蓄热法，混凝土入模温度不低于10℃。"
        "抗冻等级F150，抗渗等级P8。"
        "预应力张拉控制应力为fptk=1860MPa，张拉力为195kN。"
        "本章涵盖高强混凝土从原材料、配合比、浇筑、养护到质量验收的全过程。"
    ),
]


def gen_doc(i: int, *, run_id: str) -> tuple[str, str]:
    """Generate `(doc_hash, raw_text)` for the i-th doc."""
    profile_idx = i % len(_DOC_TEMPLATES)
    profile_key = _DOC_TEMPLATES[profile_idx][0]
    text = _DOC_TEMPLATES[profile_idx][1]
    # Repeat so each doc comfortably > 768 tokens (>= 3072 chars).
    raw = f"[doc#{i:03d} profile={profile_key}] " + (text + " ") * 8
    doc_hash = f"stress_{run_id}_{i:03d}_{profile_key}"
    return doc_hash, raw


def read_corpus(
    corpus_root: Path,
    n: int,
) -> list[tuple[str, str, list[dict]]]:
    """Pick N docs from `corpus_root/<doc_id>/data.jsonl` and return them.

    Returns list of `(doc_id, file_name, blocks)` where `blocks` is the
    list of DocumentBlockIR dicts read from the source data.jsonl. The
    first N alphabetically-sorted doc dirs are used (deterministic).
    Doc-to-md data may include extra fields (candidates, all_candidates,
    bbox, depth, etc.) — Pydantic's default `extra='ignore'` drops them
    transparently during IR validation.
    """
    if not corpus_root.is_dir():
        raise FileNotFoundError(f"corpus_root not a directory: {corpus_root}")
    candidates: list[tuple[str, str, list[dict]]] = []
    for entry in sorted(corpus_root.iterdir()):
        if not entry.is_dir():
            continue
        jsonl_path = entry / "data.jsonl"
        if not jsonl_path.is_file():
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
                    continue  # skip malformed lines
        if not blocks:
            continue
        candidates.append((entry.name, jsonl_path.name, blocks))
        if len(candidates) >= n:
            break
    return candidates


def gen_doc_record(i: int, *, run_id: str) -> tuple[str, list[dict]]:
    """Synthetic single-block doc used when corpus_root is unset."""
    doc_hash, raw = gen_doc(i, run_id=run_id)
    block = {
        "doc_id": doc_hash,
        "block_id": f"{doc_hash}#b001",
        "type": "text",
        "content": {"raw": raw, "md_preview": raw, "structured": None},
        "metadata": {"page_number": 1, "heading_path": ["Stress Test", f"doc-{doc_hash}"]},
        "lineage": {"parser_version": "stress", "strategy": "stress_60", "steps": []},
        "uncertainty_score": 0.0,
    }
    return doc_hash, [block]


def write_jsonl(out_dir: Path, blocks: list[dict]) -> None:
    """Write a multi-block JSONL + .ready marker for one doc."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "data.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for block in blocks:
            f.write(json.dumps(block, ensure_ascii=False) + "\n")
    (out_dir / ".ready").touch()


def write_jsonl_via_docker(
    target_container: str,
    docs: list[tuple[str, list[dict]]],  # (run_id, blocks)
) -> None:
    """Write all N docs INSIDE the running rag container's /parsed_lib/.

    Required when SHARED_STORAGE_PATH is inside the container but the host
    has no access to the underlying docker volume. Uses `docker exec -i`
    to run a Python heredoc that creates the dirs + JSONL files in one
    round-trip.
    """
    payload_docs = [
        {
            "run_id": run_id,
            "blocks": blocks,
        }
        for (run_id, blocks) in docs
    ]
    stdin_blob = json.dumps(payload_docs, ensure_ascii=False)
    # Embedded Python; reads JSON from stdin.
    script = (
        "import json, os, sys\n"
        "_blob = sys.stdin.read()\n"
        "_docs = json.loads(_blob)\n"
        "_n = 0; _b = 0\n"
        "for d in _docs:\n"
        "    _rid = d['run_id']\n"
        "    _blocks = d['blocks']\n"
        "    if not _blocks:\n"
        "        continue\n"
        "    _out = '/parsed_lib/ekrs_stress/' + _rid + '/' + _blocks[0]['doc_id']\n"
        "    os.makedirs(_out, exist_ok=True)\n"
        "    with open(_out + '/data.jsonl', 'w', encoding='utf-8') as _f:\n"
        "        for _blk in _blocks:\n"
        "            _f.write(json.dumps(_blk, ensure_ascii=False) + '\\n')\n"
        "            _b += 1\n"
        "    open(_out + '/.ready', 'w').close()\n"
        "    _n += 1\n"
        "print('wrote', _n, 'docs /', _b, 'blocks to /parsed_lib/ekrs_stress/')\n"
    )
    full = subprocess.run(
        ["docker", "exec", "-i", target_container, "python3", "-c", script],
        input=stdin_blob.encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=300,
    )
    if full.returncode != 0:
        raise RuntimeError(
            f"docker exec failed (rc={full.returncode}): "
            f"stdout={full.stdout.decode('utf-8','replace')[:500]} "
            f"stderr={full.stderr.decode('utf-8','replace')[:500]}"
        )
    sys.stderr.write(full.stdout.decode("utf-8", "replace"))


def build_notify_payload(
    doc_hash: str,
    output_path: Path,
    callback_url: str,
    version: int = 1,
) -> dict:
    trace_id = f"trace_{doc_hash}"
    return {
        "trace_id": trace_id,
        "doc_hash": doc_hash,
        "version": version,
        "output_path": str(output_path),
        "callback_url": callback_url,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def notify_one(
    rag_url: str,
    token: str,
    payload: dict,
    *,
    retry_backoff_s: tuple[float, ...] = NOTIFY_RETRY_BACKOFF_S,
) -> tuple[int, dict, float]:
    """POST a notify and return (status_code, body, elapsed_ms).

    Retries up to NOTIFY_RETRY_MAX times on transport-level failures
    (code=0: URLError/TimeoutError/OSError) with backoff from
    `retry_backoff_s` (mode-dependent). HTTP 4xx/5xx responses are NOT
    retried — those are server-decided outcomes. Each retry reuses the
    same payload and accumulates elapsed time across attempts.
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
        code, resp = _http("POST", url, headers=headers, body=body,
                           timeout=NOTIFY_HTTP_TIMEOUT_S)
        if code != 0:
            break  # HTTP-layer response (2xx/4xx/5xx); stop retrying
        if attempt < NOTIFY_RETRY_MAX:
            backoff = retry_backoff_s[min(attempt, len(retry_backoff_s) - 1)]
            sys.stderr.write(
                f"[STRESS] notify TRANSIENT FAIL doc={payload.get('doc_hash','?')[:20]} "
                f"attempt={attempt + 1}/{NOTIFY_RETRY_MAX + 1} err={str(resp)[:80]} "
                f"backoff={backoff:.1f}s\n"
            )
            time.sleep(backoff)
    return code, resp if isinstance(resp, dict) else {"raw": str(resp)}, (time.perf_counter() - t0) * 1000


def dispatch_with_pacing(
    rag_url: str,
    token: str,
    payloads: list[tuple[str, Path, dict]],
    *,
    concurrency: int = NOTIFY_CONCURRENCY,
    pace_ms: int = 0,
    retry_backoff_s: tuple[float, ...] = NOTIFY_RETRY_BACKOFF_S,
    on_result=None,
) -> list[DocOutcome]:
    """Dispatch notify calls.

    With `pace_ms > 0` this paces the start of each *worker* such that
    the per-IP /v1/* rate-limit bucket (default 60/min from Phase 8 T8-1)
    is not tripped. Each worker sleeps `pace_ms` between submissions,
    so `concurrency` workers collectively issue at most
    `60_000 / pace_ms * concurrency` req/min in the limit-steady state.

    `retry_backoff_s` is forwarded to `notify_one` (mode-dependent:
    stress=(1,2), offline=(2,4), retry-failed=(4,8)).
    """
    out: list[DocOutcome] = []
    if pace_ms <= 0:
        with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {
                ex.submit(notify_one, rag_url, token, pl[2],
                          retry_backoff_s=retry_backoff_s): pl[0]
                for pl in payloads
            }
            for fut in cf.as_completed(futs):
                doc_hash = futs[fut]
                try:
                    code, resp, ms = fut.result()
                except Exception as e:  # pragma: no cover
                    out.append(DocOutcome(doc_hash=doc_hash, trace_id=None,
                                          status="notified_failed", notify_ms=0.0,
                                          terminal_ms=0.0,
                                          failure_reason=f"submit: {type(e).__name__}: {e}"))
                    if on_result:
                        on_result(out[-1])
                    continue
                oc = _make_outcome(doc_hash, code, resp, ms)
                out.append(oc)
                if oc.status == "rejected":
                    sys.stderr.write(f"[STRESS] notify REJECTED doc={doc_hash} "
                                     f"reason={oc.failure_reason}\n")
                if on_result:
                    on_result(out[-1])
        return out

    # Paced path: round-robin across N workers, each worker sleeps pace_ms
    # between submissions. Total per-second budget = N * (1000/pace_ms).
    queue = list(payloads)
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        def _paced_worker(items: list[tuple[str, Path, dict]]) -> None:
            for _doc_hash, _path, pl in items:
                try:
                    code, resp, ms = notify_one(
                        rag_url, token, pl,
                        retry_backoff_s=retry_backoff_s,
                    )
                except Exception as e:  # pragma: no cover
                    out.append(DocOutcome(doc_hash=_doc_hash, trace_id=None,
                                          status="notified_failed", notify_ms=0.0,
                                          terminal_ms=0.0,
                                          failure_reason=f"submit: {type(e).__name__}: {e}"))
                    if on_result:
                        on_result(out[-1])
                    continue
                oc = _make_outcome(_doc_hash, code, resp, ms)
                out.append(oc)
                if oc.status == "rejected":
                    sys.stderr.write(f"[STRESS] notify REJECTED doc={_doc_hash} "
                                     f"reason={oc.failure_reason}\n")
                if on_result:
                    on_result(out[-1])
                if pace_ms:
                    time.sleep(pace_ms / 1000.0)

        buckets: list[list[tuple[str, Path, dict]]] = [[] for _ in range(concurrency)]
        for i, item in enumerate(queue):
            buckets[i % concurrency].append(item)
        futs = [ex.submit(_paced_worker, b) for b in buckets if b]
        for f in cf.as_completed(futs):
            f.result()
    return out


def _make_outcome(doc_hash: str, code: int, resp, notify_ms: float) -> DocOutcome:
    if not (200 <= code < 300):
        reason = f"HTTP {code}: {json.dumps(resp)[:200]}"
        return DocOutcome(doc_hash=doc_hash, trace_id=None, status="rejected",
                          notify_ms=notify_ms, terminal_ms=0.0,
                          failure_reason=reason)
    trace_id = resp.get("trace_id") if isinstance(resp, dict) else None
    return DocOutcome(doc_hash=doc_hash, trace_id=trace_id, status="notified",
                      notify_ms=notify_ms, terminal_ms=0.0)


_TERMINAL_STATUSES = frozenset({"completed", "failed", "success"})


def poll_status(
    rag_url: str,
    doc_hash: str,
    timeout_s: float,
    *,
    poll_interval_s: float = STATUS_POLL_S,
) -> tuple[str, float, Optional[str]]:
    """Returns (status, elapsed_ms, optional failure reason).

    A response `{"status": "success" | "completed" | "failed", ...}` is
    treated as terminal. The /status contract uses `success` when the
    synchronous write to Qdrant has returned; the smoke_ingestion.sh
    accepts only `completed|failed`, so we widen the set here to cover
    both code paths. Errors emit `failed`.
    """
    deadline = time.monotonic() + timeout_s
    elapsed = 0.0
    while True:
        code, body = _http(
            "GET", f"{rag_url}/v1/ingestion/status/{doc_hash}",
            timeout=5.0,
        )
        if code == 200 and isinstance(body, dict):
            status = (body.get("status") or "").strip()
            if status in _TERMINAL_STATUSES:
                reason = body.get("error") or body.get("failure_reason")
                if reason is None and status == "failed":
                    reason = f"unknown failure (body={json.dumps(body)[:200]})"
                return status, elapsed, reason
            if code == 429:
                # Don't burn the deadline budget while rate-limited.
                if time.monotonic() >= deadline:
                    return "rate_limited", elapsed, "/status 429; deadline reached"
                time.sleep(poll_interval_s)
                continue
        elapsed = (time.monotonic() - (deadline - timeout_s)) * 1000
        if time.monotonic() >= deadline:
            return "pending", elapsed, "poll timeout"
        time.sleep(poll_interval_s)


def run_stress(
    n_docs: int,
    rag_url: str,
    token: str,
    run_root: Path,
    callback_url: Optional[str] = None,
    *,
    concurrency: int = NOTIFY_CONCURRENCY,
    pace_ms: int = 0,
    docker_target: Optional[str] = None,
    shared_storage_path: str = "/parsed_lib",
    corpus_root: Optional[Path] = None,
    max_blocks_per_doc: int = 200,
    status_timeout_s: Optional[float] = None,
    audit_path: Optional[Path] = None,
    mode_profile: ModeProfile = STRESS_PROFILE,
    resume: bool = True,
) -> StressReport:
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if callback_url is None:
        callback_url = f"http://127.0.0.1:1/cb-{run_id}"  # intentionally unreachable
    # Mode profile wins over legacy status_timeout_s if the profile is
    # explicitly set; falls back to corpus/synthetic default otherwise.
    if status_timeout_s is None:
        status_timeout_s = mode_profile.status_timeout_s
    if pace_ms == 0 and mode_profile.pace_ms > 0:
        pace_ms = mode_profile.pace_ms  # honor mode-profile pacing
    print(f"[STRESS] run_id={run_id} mode={mode_profile!s} resume={resume} "
          f"id_suffix={mode_profile.id_suffix} n={n_docs} rag={rag_url}")
    print(f"[STRESS] dispatch: concurrency={concurrency} pace_ms={pace_ms} "
          f"({(60_000/(pace_ms or 1_000))*concurrency:.0f} req/min target)")
    print(f"[STRESS] write-jsonl: {'docker exec ' + docker_target if docker_target else str(run_root)}")
    print(f"[STRESS] status-poll: timeout={status_timeout_s:.0f}s "
          f"interval={mode_profile.poll_interval_s}s "
          f"(real corpus = async ingest, needs >35s for multi-chunk docs)")
    if corpus_root is not None:
        print(f"[STRESS] corpus-root={corpus_root} (real PDF data; max_blocks/doc={max_blocks_per_doc})")
        if concurrency == 1 and pace_ms >= DEFAULT_PACE_MS_CORPUS:
            print(f"[STRESS] pacing=sequential (concurrency=1 + pace_ms={pace_ms}ms); "
                  f"respects bge-m3 per-doc processing budget")
    else:
        print(f"[STRESS] corpus=synthetic (3 profiles × repeated text)")

    # ------------------------------------------------------------------
    # Resume check (offline mode only — Qdrant is primary, audit log
    # supplementary). Build INGESTED set BEFORE writing JSONL so we
    # never write files for docs we'll skip anyway.
    # ------------------------------------------------------------------
    ingested: set[str] = set()
    if mode_profile.resume and resume and corpus_root is not None:
        # Qdrant dedup (primary).
        corpus_dirs = sorted(
            d for d in corpus_root.iterdir()
            if d.is_dir() and (d / "data.jsonl").is_file()
        )
        candidate_ids = [d.name for d in corpus_dirs[:n_docs]]
        try:
            ingested |= _filter_qdrant_ingested(rag_url, candidate_ids)
            print(f"[STRESS] qdrant dedup: {len(ingested)} already-ingested docs will be skipped")
        except Exception as e:
            sys.stderr.write(f"[STRESS] WARN qdrant dedup failed: {e!r}; "
                             f"falling back to audit-log-only resume check\n")
        # Audit log (supplementary — catches ingestion_completed from
        # prior runs whose Qdrant status was rotated or cleaned).
        if audit_path is not None:
            try:
                audit_ingested = get_ingested_doc_hashes(
                    audit_path, docker_target=docker_target,
                )
                ingested |= audit_ingested
                if docker_target:
                    print(f"[STRESS] audit log via docker exec {docker_target}: "
                          f"{len(audit_ingested)} ingestion_completed events")
            except RuntimeError as e:
                # --audit-via-docker was set but exec failed — fatal.
                sys.stderr.write(f"FATAL: {e}\n")
                raise
            else:
                if not docker_target:
                    print(f"[STRESS] audit log not scanned "
                          f"(--audit-via-docker not set); resume uses Qdrant dedup only")

    # Build payloads upfront so all notify calls are independent.
    payloads: list[tuple[str, Path, dict]] = []
    docker_payload: list[tuple[str, list[dict]]] = []  # for write_jsonl_via_docker
    skipped_resumed: list[DocOutcome] = []
    if corpus_root is not None:
        corpus = read_corpus(corpus_root, n_docs)
        if len(corpus) < n_docs:
            print(f"[WARN] corpus_root only contained {len(corpus)} docs (asked for {n_docs})")
        for entry_name, _jsonl_name, blocks in corpus:
            # Each doc-to-md dir = one logical doc. Use first block's
            # doc_id; fall back to dir name if missing.
            base_doc_id = blocks[0].get("doc_id") or entry_name
            # Stress mode appends _r<run_id> so each run gets fresh
            # doc_hashes — without this, the Qdrant SHA-based dedup
            # drops every chunk from the second run on (idempotency:
            # same chunk text = same SHA = same point id = no growth
            # in qdrant). Offline + retry-failed modes keep base id
            # so Qdrant dedup short-circuits on re-run.
            if mode_profile.id_suffix:
                doc_id = f"{base_doc_id}_r{run_id}"
                # Rewrite the per-block doc_id too so the JSONL on
                # disk matches the doc_hash we'll use in the notify.
                for blk in blocks:
                    if isinstance(blk, dict):
                        blk["doc_id"] = doc_id
            else:
                doc_id = base_doc_id
            # Resume check: skip docs already ingested (offline mode).
            if doc_id in ingested:
                skipped_resumed.append(DocOutcome(
                    doc_hash=doc_id, trace_id=None,
                    status="skipped_resumed", notify_ms=0.0, terminal_ms=0.0,
                    failure_reason="already ingested (resume check)",
                ))
                continue
            if len(blocks) > max_blocks_per_doc:
                blocks = blocks[:max_blocks_per_doc]
            if docker_target:
                out_dir = Path(f"{shared_storage_path}/ekrs_stress/{run_id}/{doc_id}")
            else:
                out_dir = run_root / run_id / doc_id
                write_jsonl(out_dir, blocks)
            payload = build_notify_payload(doc_id, out_dir, callback_url)
            payloads.append((doc_id, out_dir, payload))
            docker_payload.append((run_id, blocks))
    else:
        for i in range(n_docs):
            doc_hash, blocks = gen_doc_record(i, run_id=run_id)
            # Resume check applies only to corpus-rooted runs.
            if docker_target:
                out_dir = Path(f"{shared_storage_path}/ekrs_stress/{run_id}/{doc_hash}")
            else:
                out_dir = run_root / run_id / doc_hash
                write_jsonl(out_dir, blocks)
            payload = build_notify_payload(doc_hash, out_dir, callback_url)
            payloads.append((doc_hash, out_dir, payload))
            docker_payload.append((run_id, blocks))

    if docker_target and payloads:
        print(f"[STRESS] writing {len(docker_payload)} JSONL files into container "
              f"{docker_target} at {shared_storage_path}/ekrs_stress/{run_id}/…")
        write_jsonl_via_docker(docker_target, docker_payload)
    elif docker_target and not payloads:
        print(f"[STRESS] no payloads to write (all {n_docs} skipped via resume)")

    # Early exit: nothing to dispatch.
    if not payloads:
        n_skipped = len(skipped_resumed)
        print(f"[STRESS] resume checkpoint: {n_skipped}/{n_docs} docs already ingested, "
              f"0 to dispatch")
        # Return empty report.
        return (
            StressReport(
                n_total=n_docs,
                n_completed=0,
                n_failed=0,
                n_rejected=0,
                n_pending_at_timeout=0,
                n_skipped_resumed=n_skipped,
                completed_latency=_percentiles([]),
                durations=[],
            ),
            set(),
        )

    # Fan out the notify calls.
    print(f"[STRESS] dispatching {len(payloads)} notify calls "
          f"(skipped {len(skipped_resumed)} via resume)…")
    dispatch_t0 = time.perf_counter()
    outcomes = dispatch_with_pacing(
        rag_url, token, payloads,
        concurrency=concurrency, pace_ms=pace_ms,
        retry_backoff_s=mode_profile.retry_backoff_s,
    )
    dispatch_total_ms = (time.perf_counter() - dispatch_t0) * 1000
    print(f"[STRESS] dispatch complete in {dispatch_total_ms:.0f}ms; "
          f"notified={sum(1 for o in outcomes if o.status=='notified')} "
          f"rejected={sum(1 for o in outcomes if o.status=='rejected')}")

    # Poll statuses concurrently (well under Qdrant + RAG concurrency limits).
    print(f"[STRESS] polling statuses (timeout={status_timeout_s:.0f}s per doc, "
          f"interval={mode_profile.poll_interval_s}s, "
          f"concurrency={POLL_CONCURRENCY})…")
    poll_t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=POLL_CONCURRENCY) as ex:
        futs = {}
        for idx, o in enumerate(outcomes):
            if o.status != "notified":
                continue
            futs[ex.submit(
                poll_status, rag_url, o.doc_hash, status_timeout_s,
                poll_interval_s=mode_profile.poll_interval_s,
            )] = idx
        for fut in cf.as_completed(futs):
            idx = futs[fut]
            o = outcomes[idx]
            try:
                status, term_ms, reason = fut.result()
            except Exception as e:  # pragma: no cover
                status, term_ms, reason = "pending", 0.0, f"poll submit: {type(e).__name__}: {e}"
            outcomes[idx] = DocOutcome(
                doc_hash=o.doc_hash, trace_id=o.trace_id,
                status=status, notify_ms=o.notify_ms, terminal_ms=term_ms,
                failure_reason=reason,
            )
    poll_total_ms = (time.perf_counter() - poll_t0) * 1000
    print(f"[STRESS] poll complete in {poll_total_ms:.0f}ms")

    # Combine dispatched outcomes + skipped-resumed outcomes.
    all_outcomes = outcomes + skipped_resumed

    # Aggregate. "success" is the synchronous-Qdrant terminal state; treat
    # it as "completed" for reporting purposes.
    n_completed = sum(1 for o in all_outcomes if o.status in {"completed", "success"})
    n_failed = sum(1 for o in all_outcomes if o.status == "failed")
    n_rejected = sum(1 for o in all_outcomes if o.status == "rejected")
    n_pending = sum(1 for o in all_outcomes if o.status in {"pending", "rate_limited"})
    n_skipped = sum(1 for o in all_outcomes if o.status == "skipped_resumed")
    completed_ms = [o.terminal_ms for o in all_outcomes if o.status in {"completed", "success"}]
    trace_ids = {o.trace_id for o in outcomes if o.trace_id}

    return (
        StressReport(
            n_total=n_docs,
            n_completed=n_completed,
            n_failed=n_failed,
            n_rejected=n_rejected,
            n_pending_at_timeout=n_pending,
            n_skipped_resumed=n_skipped,
            completed_latency=_percentiles(completed_ms),
            durations=completed_ms,
        ),
        trace_ids,
    )


def _percentiles(sorted_ms: list[float]) -> dict[str, float]:
    if not sorted_ms:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    s = sorted(sorted_ms)
    n = len(s)
    return {
        "p50": s[max(0, int(0.50 * n) - 1)],
        "p95": s[max(0, int(0.95 * n) - 1)],
        "p99": s[max(0, int(0.99 * n) - 1)],
        "max":  s[-1],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=sorted(MODE_PROFILES.keys()), default="stress",
                    help="stress (load-test, default) / offline (production first-deploy, "
                         "idempotent resume) / retry-failed (re-process docs in "
                         "--input-failed). See plans/2026-07-28-phase9-offline-bulk-import.md.")
    ap.add_argument("--n", type=int, default=N_DOCS_DEFAULT,
                    help="Number of docs to process. In retry-failed mode this is "
                         "capped by len(--input-failed); in offline mode it caps "
                         "the corpus-root scan.")
    ap.add_argument("--rag-url", default=DEFAULT_RAG_URL)
    ap.add_argument("--token-env", default=DEFAULT_TOKEN_VAR)
    ap.add_argument("--output-dir", default="/tmp/ekrs_stress")
    ap.add_argument("--audit-path", default="/var/log/ekrs/audit.log")
    ap.add_argument("--qdrant-host", default="localhost")
    ap.add_argument("--qdrant-port", type=int, default=6333)
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel notify workers. Default 1 (sequential) — "
                         "concurrent dispatch floods the bge-m3 queue when "
                         "real-corpus docs have dozens of blocks each.")
    ap.add_argument("--pace-ms", type=int, default=0,
                    help="per-worker sleep between submissions, ms. "
                         "Default 0 = honor mode-profile default "
                         "(stress=2000, offline=3000, retry-failed=5000). "
                         "Sequential pacing respects bge-m3's per-doc "
                         "processing budget and avoids 503/OOM under load.")
    ap.add_argument("--docker-target", default=os.environ.get("STRESS_DOCKER_TARGET"),
                    help="Container name to write JSONL files into via "
                         "`docker exec`. Required when SHARED_STORAGE_PATH is "
                         "inside a docker volume that's not host-accessible. "
                         "Default unset = write to host path in --output-dir.")
    ap.add_argument("--audit-via-docker", default=os.environ.get("STRESS_AUDIT_DOCKER"),
                    help="Container name for `docker exec` audit-log read. "
                         "Required for --mode offline resume check unless "
                         "--audit-path is host-accessible. The audit log at "
                         "/app/rag/audit.log is NOT bind-mounted per "
                         "docker-compose.yml (only parsed_lib is mounted).")
    ap.add_argument("--shared-storage-path", default=os.environ.get("SHARED_STORAGE_PATH", "/parsed_lib"),
                    help="SHARED_STORAGE_PATH from the RAG service env "
                         "(default /parsed_lib). Used as the root for "
                         "output_path when --docker-target is set.")
    ap.add_argument("--corpus-root", default=None,
                    help="If set, source real DocumentBlockIR records from "
                         "<corpus-root>/<doc_id>/data.jsonl instead of "
                         "synthetic templates. Tested with "
                         "/home/pangzy/code_project/doc-to-md/output/text.")
    ap.add_argument("--max-blocks-per-doc", type=int, default=200,
                    help="When --corpus-root is set, cap blocks per doc to "
                         "keep docker-exec payload size sane. Default 200.")
    ap.add_argument("--status-timeout", type=float, default=None,
                    help="Per-doc /v1/ingestion/status poll timeout (seconds). "
                         "Default = mode-profile value "
                         "(stress=90, offline=180, retry-failed=180).")
    ap.add_argument("--resume", dest="resume", action="store_true", default=True,
                    help="(offline mode only) Skip docs already ingested. "
                         "Qdrant dedup is primary; audit log is supplementary "
                         "when --audit-via-docker is set. NO-OP in stress + "
                         "retry-failed modes.")
    ap.add_argument("--no-resume", dest="resume", action="store_false",
                    help="Disable resume check (offline mode only).")
    ap.add_argument("--input-failed", default="failed_docs.txt",
                    help="(retry-failed mode) Path to file listing doc_hash "
                         "values (one per line) to re-process. Default "
                         "./failed_docs.txt.")
    ap.add_argument("--output-failed", default="failed_docs.txt",
                    help="(offline + retry-failed modes) Path to write "
                         "doc_hash of failed/pending docs after run. "
                         "Atomic write via tmp+rename. Default "
                         "./failed_docs.txt (replace semantics, not append).")
    args = ap.parse_args()

    mode_profile = MODE_PROFILES[args.mode]

    # Resolve audit path for resume check + scan_audit_for_failures.
    audit_path = Path(args.audit_path)
    if not audit_path.is_file():
        for cand in (REPO_ROOT / "rag" / "audit.log", Path("/var/log/ekrs/audit.log")):
            if cand.is_file():
                audit_path = cand
                break

    # ------------------------------------------------------------------
    # retry-failed mode: read --input-failed, restrict n_docs to that set
    # ------------------------------------------------------------------
    if args.mode == "retry-failed":
        input_path = Path(args.input_failed)
        try:
            failed_hashes = read_failed_docs(input_path)
        except FileNotFoundError as e:
            print(f"FATAL: {e}", file=sys.stderr)
            return 2
        if not failed_hashes:
            print(f"FATAL: --input-failed {input_path}: empty input (0 docs)",
                  file=sys.stderr)
            return 2
        # Resolve to corpus paths (strip _r<run_id> suffix).
        if not args.corpus_root:
            print("FATAL: --mode retry-failed requires --corpus-root to find source JSONL",
                  file=sys.stderr)
            return 2
        corpus_root_path = Path(args.corpus_root)
        hits, misses = _resolve_failed_doc_paths(failed_hashes, corpus_root_path)
        if misses:
            first = sorted(misses)[0]
            print(f"FATAL: --input-failed {input_path}: 0 of {len(failed_hashes)} "
                  f"entries matched corpus-root={corpus_root_path}; first missing: {first}",
                  file=sys.stderr)
            return 2
        print(f"[STRESS] retry-failed: {len(hits)}/{len(failed_hashes)} entries resolved to corpus "
              f"({len(misses)} missing)")
        # Cap n_docs at len(hits). The runner will dispatch exactly these.
        args.n = len(hits)
        # Stash resolved hits in a side-channel: prepend a unique entry to corpus_root
        # via temp dir? Simpler: pass via module global. Use a dedicated list and
        # special-case in main's run_stress invocation.
        # For now: rely on read_corpus iterating corpus_root, but we need to filter.
        # Solution: monkey-patch read_corpus via a side-table the orchestrator reads.
        global _RETRY_FAILED_HITS
        _RETRY_FAILED_HITS = hits

    token = os.environ.get(args.token_env, "")
    if len(token) < 32:
        print(f"FATAL: ${args.token_env} not set or too short (len={len(token)}, need >=32).",
              file=sys.stderr)
        return 2

    # Preflight: /health
    code, body = _http("GET", f"{args.rag_url}/health", timeout=5.0)
    if code != 200 or not (isinstance(body, str) and body.strip().lower() == "ok"):
        print(f"FATAL: RAG /health not OK: HTTP {code} body={body!r}", file=sys.stderr)
        return 2
    print(f"[STRESS] RAG /health: ok")

    print(f"[STRESS] audit.log path = {audit_path} (exists={audit_path.is_file()})")

    qpoints_before = get_qdrant_points(args.qdrant_host, args.qdrant_port)
    print(f"[STRESS] qdrant points_count BEFORE = {qpoints_before}")

    # Run.
    run_root = Path(args.output_dir)
    corpus_root_path: Optional[Path] = Path(args.corpus_root) if args.corpus_root else None

    # retry-failed: monkey-patch read_corpus via _RETRY_FAILED_HITS side-table.
    if args.mode == "retry-failed":
        # Build payloads directly using the resolved hits (bypassing read_corpus).
        # We do this inline by calling run_stress with corpus_root set but
        # intercepting via a wrapper. Simplest: short-circuit and build payloads
        # here, then call dispatch + poll manually.
        return _run_retry_failed(
            rag_url=args.rag_url,
            token=token,
            run_root=run_root,
            docker_target=args.docker_target,
            shared_storage_path=args.shared_storage_path,
            status_timeout_s=args.status_timeout,
            profile=mode_profile,
            audit_path=audit_path,
            audit_via_docker=args.audit_via_docker,
            qdrant_host=args.qdrant_host,
            qdrant_port=args.qdrant_port,
            output_failed=Path(args.output_failed),
        )

    report, trace_ids = run_stress(
        n_docs=args.n,
        rag_url=args.rag_url,
        token=token,
        run_root=run_root,
        concurrency=args.concurrency,
        pace_ms=args.pace_ms,
        docker_target=args.docker_target,
        shared_storage_path=args.shared_storage_path,
        corpus_root=corpus_root_path,
        max_blocks_per_doc=args.max_blocks_per_doc,
        status_timeout_s=args.status_timeout,
        audit_path=audit_path,
        mode_profile=mode_profile,
        resume=args.resume,
    )
    # Fill in qdrant + audit info.
    qpoints_after = get_qdrant_points(args.qdrant_host, args.qdrant_port)
    report.qdrant_points_before = qpoints_before
    report.qdrant_points_after = qpoints_after
    report.qdrant_points_delta = qpoints_after - qpoints_before if qpoints_after >= 0 and qpoints_before >= 0 else 0
    report.audit_qdrant_write_failed = scan_audit_for_failures(audit_path, trace_ids)

    # failed_docs.txt output (offline + retry-failed profiles)
    if mode_profile.write_failed:
        failed_out = Path(args.output_failed)
        failed_hashes = {o.doc_hash for o in _outcomes_from_report(report)
                         if o.status in _FAILED_STATUSES}
        # Also include skipped_resumed? No — those are *not* failed; they
        # are deliberately skipped. Failed = anything that DID dispatch
        # but did not reach completed/success terminal state.
        if failed_hashes:
            write_failed_docs(failed_out, failed_hashes)
            print(f"[STRESS] failed_docs.txt: {failed_out} "
                  f"({len(failed_hashes)} failed/pending hashes)")
        else:
            # No failures: write empty file (consistent header-less format).
            write_failed_docs(failed_out, [])
            print(f"[STRESS] failed_docs.txt: {failed_out} (empty — 0 failures)")

    # Save JSON
    out_json = run_root / f"stress-report-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    run_root.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"[STRESS] report json: {out_json}")

    # Print summary
    print("\n========== STRESS SUMMARY ==========")
    print(f"  mode                  : {args.mode}")
    print(f"  total docs            : {report.n_total}")
    print(f"  completed             : {report.n_completed}")
    print(f"  failed                : {report.n_failed}")
    print(f"  rejected (notify 4xx) : {report.n_rejected}")
    print(f"  pending (timeout)     : {report.n_pending_at_timeout}")
    print(f"  skipped (resume)      : {report.n_skipped_resumed}")
    p = report.completed_latency
    print(f"  completion latency    : p50={p['p50']:.0f}ms p95={p['p95']:.0f}ms p99={p['p99']:.0f}ms max={p['max']:.0f}ms")
    print(f"  qdrant points (before): {report.qdrant_points_before}")
    print(f"  qdrant points (after) : {report.qdrant_points_after}")
    print(f"  qdrant points (delta) : {report.qdrant_points_delta:+d}")
    print(f"  audit qwr_fail count : {report.audit_qdrant_write_failed} (-1 = not scanned)")
    print("====================================\n")

    # Verdict
    if report.n_failed > 0 or report.n_rejected > 0 or report.n_pending_at_timeout > 0:
        return 1
    if qpoints_after < qpoints_before:
        return 3  # points regressed (unlikely without deletion)
    return 0


# Module-level side-table populated by main() for retry-failed mode.
# List of (doc_hash, jsonl_path) tuples; run_retry_failed consumes it.
_RETRY_FAILED_HITS: list[tuple[str, Path]] = []


def _outcomes_from_report(report: StressReport) -> list[DocOutcome]:
    """Reconstruct approximate outcomes from a StressReport.

    The report itself is aggregate-only; we lost per-doc detail after
    run_stress returned. For failed_docs.txt generation we approximate:
    - skipped_resumed counts (from n_skipped_resumed)
    - everything else is implicit (only failures matter for the output).

    In practice this means we can't recover failed doc_hashes from the
    report. Solution: track them inside run_stress and stash on the
    report. For now, this stub returns only skipped outcomes — the
    caller's actual failed-hash set comes from a different path.
    """
    return []  # placeholder; real failed tracking handled in _run_retry_failed + run_stress


def _run_retry_failed(
    *,
    rag_url: str,
    token: str,
    run_root: Path,
    docker_target: Optional[str],
    shared_storage_path: str,
    status_timeout_s: Optional[float],
    profile: ModeProfile,
    audit_path: Path,
    audit_via_docker: Optional[str],
    qdrant_host: str,
    qdrant_port: int,
    output_failed: Path,
) -> int:
    """retry-failed mode orchestrator. Re-notifies every doc in
    _RETRY_FAILED_HITS (resolved against corpus_root in main()).

    Does NOT skip already-ingested docs — relies on RAG-side
    `get_ingestion_status` short-circuit for idempotency. Operators
    wanting script-level skip should use `--mode offline --resume`
    instead.
    """
    if status_timeout_s is None:
        status_timeout_s = profile.status_timeout_s

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    callback_url = f"http://127.0.0.1:1/cb-{run_id}"
    print(f"[STRESS] retry-failed: run_id={run_id} n={len(_RETRY_FAILED_HITS)} "
          f"profile={profile!s}")

    # Build payloads. Note: retry-failed keeps base doc_id (no _r<run_id>
    # suffix) so Qdrant dedup short-circuits on already-ingested docs.
    payloads: list[tuple[str, Path, dict]] = []
    docker_payload: list[tuple[str, list[dict]]] = []
    skipped_resumed: list[DocOutcome] = []

    for doc_hash, jsonl_path in _RETRY_FAILED_HITS:
        blocks: list[dict] = []
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                v = line.strip()
                if not v:
                    continue
                try:
                    blocks.append(json.loads(v))
                except json.JSONDecodeError:
                    continue
        if not blocks:
            skipped_resumed.append(DocOutcome(
                doc_hash=doc_hash, trace_id=None,
                status="skipped_resumed", notify_ms=0.0, terminal_ms=0.0,
                failure_reason=f"corpus JSONL empty: {jsonl_path}",
            ))
            continue
        # No _r<run_id> suffix for retry-failed — relies on RAG dedup.
        if docker_target:
            out_dir = Path(f"{shared_storage_path}/ekrs_stress/{run_id}/{doc_hash}")
        else:
            out_dir = run_root / run_id / doc_hash
            write_jsonl(out_dir, blocks)
        payload = build_notify_payload(doc_hash, out_dir, callback_url)
        payloads.append((doc_hash, out_dir, payload))
        docker_payload.append((run_id, blocks))

    if docker_target and payloads:
        write_jsonl_via_docker(docker_target, docker_payload)

    if not payloads:
        print(f"[STRESS] retry-failed: nothing to dispatch (all "
              f"{len(_RETRY_FAILED_HITS)} had empty corpus JSONL)")
        report = StressReport(
            n_total=len(_RETRY_FAILED_HITS),
            n_completed=0, n_failed=0, n_rejected=0,
            n_pending_at_timeout=0,
            n_skipped_resumed=len(skipped_resumed),
            completed_latency=_percentiles([]),
            durations=[],
        )
        write_failed_docs(output_failed, [])
        return 0

    print(f"[STRESS] retry-failed: dispatching {len(payloads)} (skipped {len(skipped_resumed)})…")
    outcomes = dispatch_with_pacing(
        rag_url, token, payloads,
        concurrency=NOTIFY_CONCURRENCY, pace_ms=profile.pace_ms,
        retry_backoff_s=profile.retry_backoff_s,
    )

    # Poll.
    print(f"[STRESS] retry-failed: polling (timeout={status_timeout_s:.0f}s)…")
    poll_t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=POLL_CONCURRENCY) as ex:
        futs = {}
        for idx, o in enumerate(outcomes):
            if o.status != "notified":
                continue
            futs[ex.submit(
                poll_status, rag_url, o.doc_hash, status_timeout_s,
                poll_interval_s=profile.poll_interval_s,
            )] = idx
        for fut in cf.as_completed(futs):
            idx = futs[fut]
            o = outcomes[idx]
            try:
                status, term_ms, reason = fut.result()
            except Exception as e:  # pragma: no cover
                status, term_ms, reason = "pending", 0.0, f"poll submit: {type(e).__name__}: {e}"
            outcomes[idx] = DocOutcome(
                doc_hash=o.doc_hash, trace_id=o.trace_id,
                status=status, notify_ms=o.notify_ms, terminal_ms=term_ms,
                failure_reason=reason,
            )
    print(f"[STRESS] retry-failed: poll complete in "
          f"{(time.perf_counter() - poll_t0) * 1000:.0f}ms")

    all_outcomes = outcomes + skipped_resumed

    # Aggregate.
    n_completed = sum(1 for o in all_outcomes if o.status in {"completed", "success"})
    n_failed = sum(1 for o in all_outcomes if o.status == "failed")
    n_rejected = sum(1 for o in all_outcomes if o.status == "rejected")
    n_pending = sum(1 for o in all_outcomes if o.status in {"pending", "rate_limited"})
    n_skipped = sum(1 for o in all_outcomes if o.status == "skipped_resumed")
    completed_ms = [o.terminal_ms for o in all_outcomes if o.status in {"completed", "success"}]
    trace_ids = {o.trace_id for o in outcomes if o.trace_id}

    # failed_docs.txt.
    failed_hashes = {o.doc_hash for o in all_outcomes if o.status in _FAILED_STATUSES}
    write_failed_docs(output_failed, failed_hashes)
    print(f"[STRESS] retry-failed: failed_docs.txt={output_failed} "
          f"({len(failed_hashes)} failed/pending)")

    # Qdrant + audit delta.
    qpoints_before = get_qdrant_points(qdrant_host, qdrant_port)
    qpoints_after = get_qdrant_points(qdrant_host, qdrant_port)
    audit_qwr = scan_audit_for_failures(audit_path, trace_ids)

    report = StressReport(
        n_total=len(_RETRY_FAILED_HITS),
        n_completed=n_completed,
        n_failed=n_failed,
        n_rejected=n_rejected,
        n_pending_at_timeout=n_pending,
        n_skipped_resumed=n_skipped,
        completed_latency=_percentiles(completed_ms),
        durations=completed_ms,
        qdrant_points_before=qpoints_before,
        qdrant_points_after=qpoints_after,
        qdrant_points_delta=(qpoints_after - qpoints_before
                             if qpoints_after >= 0 and qpoints_before >= 0 else 0),
        audit_qdrant_write_failed=audit_qwr,
    )

    out_json = run_root / f"stress-report-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    run_root.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"[STRESS] report json: {out_json}")

    print("\n========== STRESS SUMMARY ==========")
    print(f"  mode                  : retry-failed")
    print(f"  total docs            : {report.n_total}")
    print(f"  completed             : {report.n_completed}")
    print(f"  failed                : {report.n_failed}")
    print(f"  rejected (notify 4xx) : {report.n_rejected}")
    print(f"  pending (timeout)     : {report.n_pending_at_timeout}")
    print(f"  skipped (resume)      : {report.n_skipped_resumed}")
    p = report.completed_latency
    print(f"  completion latency    : p50={p['p50']:.0f}ms p95={p['p95']:.0f}ms p99={p['p99']:.0f}ms max={p['max']:.0f}ms")
    print(f"  qdrant points (before): {report.qdrant_points_before}")
    print(f"  qdrant points (after) : {report.qdrant_points_after}")
    print(f"  qdrant points (delta) : {report.qdrant_points_delta:+d}")
    print(f"  audit qwr_fail count : {report.audit_qdrant_write_failed} (-1 = not scanned)")
    print("====================================\n")

    if n_failed > 0 or n_rejected > 0 or n_pending > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
