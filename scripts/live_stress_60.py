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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


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


@dataclass(frozen=True)
class DocOutcome:
    doc_hash: str
    trace_id: Optional[str]
    status: str  # one of: notified, rejected, completed, failed, pending
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
                if entry.get("event_type") != "qdrant_write_failed":
                    continue
                tid = entry.get("trace_id") or ""
                if tid in target_trace_ids:
                    failures += 1
    except OSError:
        return -1
    return failures


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
) -> tuple[int, dict, float]:
    """POST a notify and return (status_code, body, elapsed_ms).

    Retries up to NOTIFY_RETRY_MAX times on transport-level failures
    (code=0: URLError/TimeoutError/OSError) with backoff from
    NOTIFY_RETRY_BACKOFF_S. HTTP 4xx/5xx responses are NOT retried —
    those are server-decided outcomes. Each retry reuses the same
    payload and accumulates elapsed time across attempts.
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
            backoff = NOTIFY_RETRY_BACKOFF_S[min(attempt, len(NOTIFY_RETRY_BACKOFF_S) - 1)]
            sys.stderr.write(
                f"[STRESS] notify TRANSIENT FAIL doc={payload.get('doc_id','?')[:20]} "
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
    on_result=None,
) -> list[DocOutcome]:
    """Dispatch notify calls.

    With `pace_ms > 0` this paces the start of each *worker* such that
    the per-IP /v1/* rate-limit bucket (default 60/min from Phase 8 T8-1)
    is not tripped. Each worker sleeps `pace_ms` between submissions,
    so `concurrency` workers collectively issue at most
    `60_000 / pace_ms * concurrency` req/min in the limit-steady state.
    """
    out: list[DocOutcome] = []
    if pace_ms <= 0:
        with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(notify_one, rag_url, token, pl[2]): pl[0] for pl in payloads}
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
                    code, resp, ms = notify_one(rag_url, token, pl)
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


def poll_status(rag_url: str, doc_hash: str, timeout_s: float) -> tuple[str, float, Optional[str]]:
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
                time.sleep(STATUS_POLL_S)
                continue
        elapsed = (time.monotonic() - (deadline - timeout_s)) * 1000
        if time.monotonic() >= deadline:
            return "pending", elapsed, "poll timeout"
        time.sleep(STATUS_POLL_S)


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
) -> StressReport:
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if callback_url is None:
        callback_url = f"http://127.0.0.1:1/cb-{run_id}"  # intentionally unreachable
    if status_timeout_s is None:
        status_timeout_s = STATUS_TIMEOUT_CORPUS_S if corpus_root else STATUS_TIMEOUT_S
    print(f"[STRESS] run_id={run_id} n={n_docs} rag={rag_url} callback={callback_url}")
    print(f"[STRESS] dispatch: concurrency={concurrency} pace_ms={pace_ms} "
          f"({(60_000/(pace_ms or 1_000))*concurrency:.0f} req/min target)")
    print(f"[STRESS] write-jsonl: {'docker exec ' + docker_target if docker_target else str(run_root)}")
    print(f"[STRESS] status-poll: timeout={status_timeout_s:.0f}s (real corpus = async ingest, "
          f"needs >35s for multi-chunk docs)")
    if corpus_root is not None:
        print(f"[STRESS] corpus-root={corpus_root} (real PDF data; max_blocks/doc={max_blocks_per_doc})")
        if concurrency == 1 and pace_ms >= DEFAULT_PACE_MS_CORPUS:
            print(f"[STRESS] pacing=sequential (concurrency=1 + pace_ms={pace_ms}ms); "
                  f"respects bge-m3 per-doc processing budget")
    else:
        print(f"[STRESS] corpus=synthetic (3 profiles × repeated text)")

    # Build payloads upfront so all notify calls are independent.
    payloads: list[tuple[str, Path, dict]] = []
    docker_payload: list[tuple[str, list[dict]]] = []  # for write_jsonl_via_docker
    if corpus_root is not None:
        corpus = read_corpus(corpus_root, n_docs)
        if len(corpus) < n_docs:
            print(f"[WARN] corpus_root only contained {len(corpus)} docs (asked for {n_docs})")
        for entry_name, _jsonl_name, blocks in corpus:
            # Each doc-to-md dir = one logical doc. Use first block's
            # doc_id; fall back to dir name if missing.
            base_doc_id = blocks[0].get("doc_id") or entry_name
            # Append run_id so each run gets fresh doc_hashes — without
            # this, the Qdrant SHA-based dedup drops every chunk from
            # the second run on (idempotency: same chunk text = same
            # SHA = same point id = no growth in qdrant).
            doc_id = f"{base_doc_id}_r{run_id}"
            # Rewrite the per-block doc_id too so the JSONL on disk
            # matches the doc_hash we'll use in the notify payload.
            for blk in blocks:
                if isinstance(blk, dict):
                    blk["doc_id"] = doc_id
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
            if docker_target:
                out_dir = Path(f"{shared_storage_path}/ekrs_stress/{run_id}/{doc_hash}")
            else:
                out_dir = run_root / run_id / doc_hash
                write_jsonl(out_dir, blocks)
            payload = build_notify_payload(doc_hash, out_dir, callback_url)
            payloads.append((doc_hash, out_dir, payload))
            docker_payload.append((run_id, blocks))

    if docker_target:
        print(f"[STRESS] writing {len(docker_payload)} JSONL files into container "
              f"{docker_target} at {shared_storage_path}/ekrs_stress/{run_id}/…")
        write_jsonl_via_docker(docker_target, docker_payload)

    # Fan out the 60 notify calls.
    outcomes: list[DocOutcome] = []
    print(f"[STRESS] dispatching {n_docs} notify calls…")
    dispatch_t0 = time.perf_counter()
    outcomes = dispatch_with_pacing(
        rag_url, token, payloads,
        concurrency=concurrency, pace_ms=pace_ms,
    )
    dispatch_total_ms = (time.perf_counter() - dispatch_t0) * 1000
    print(f"[STRESS] dispatch complete in {dispatch_total_ms:.0f}ms; "
          f"notified={sum(1 for o in outcomes if o.status=='notified')} "
          f"rejected={sum(1 for o in outcomes if o.status=='rejected')}")

    # Poll statuses concurrently (well under Qdrant + RAG concurrency limits).
    print(f"[STRESS] polling statuses (timeout={status_timeout_s:.0f}s per doc, "
          f"concurrency={POLL_CONCURRENCY})…")
    poll_t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=POLL_CONCURRENCY) as ex:
        futs = {}
        for idx, o in enumerate(outcomes):
            if o.status != "notified":
                continue
            futs[ex.submit(poll_status, rag_url, o.doc_hash, status_timeout_s)] = idx
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

    # Aggregate. "success" is the synchronous-Qdrant terminal state; treat
    # it as "completed" for reporting purposes.
    n_completed = sum(1 for o in outcomes if o.status in {"completed", "success"})
    n_failed = sum(1 for o in outcomes if o.status == "failed")
    n_rejected = sum(1 for o in outcomes if o.status == "rejected")
    n_pending = sum(1 for o in outcomes if o.status in {"pending", "rate_limited"})
    completed_ms = [o.terminal_ms for o in outcomes if o.status in {"completed", "success"}]
    trace_ids = {o.trace_id for o in outcomes if o.trace_id}

    return (
        StressReport(
            n_total=n_docs,
            n_completed=n_completed,
            n_failed=n_failed,
            n_rejected=n_rejected,
            n_pending_at_timeout=n_pending,
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
    ap.add_argument("--n", type=int, default=N_DOCS_DEFAULT)
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
    ap.add_argument("--pace-ms", type=int, default=DEFAULT_PACE_MS_CORPUS,
                    help="per-worker sleep between submissions, ms. "
                         "Default 2000 (sequential pacing, ~30 req/min) when "
                         "--corpus-root is set; 0 for synthetic corpus. "
                         "Sequential pacing respects bge-m3's per-doc "
                         "processing budget and avoids 503/OOM under load.")
    ap.add_argument("--docker-target", default=os.environ.get("STRESS_DOCKER_TARGET"),
                    help="Container name to write JSONL files into via "
                         "`docker exec`. Required when SHARED_STORAGE_PATH is "
                         "inside a docker volume that's not host-accessible. "
                         "Default unset = write to host path in --output-dir.")
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
                         "Defaults to 90 with --corpus-root (real docs ingest "
                         "async, multi-chunk), 35 with synthetic corpus.")
    args = ap.parse_args()

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

    # Audit log: try common paths.
    audit_path = Path(args.audit_path)
    if not audit_path.is_file():
        for cand in (REPO_ROOT / "rag" / "audit.log", Path("/var/log/ekrs/audit.log")):
            if cand.is_file():
                audit_path = cand
                break
    print(f"[STRESS] audit.log path = {audit_path} (exists={audit_path.is_file()})")

    qpoints_before = get_qdrant_points(args.qdrant_host, args.qdrant_port)
    print(f"[STRESS] qdrant points_count BEFORE = {qpoints_before}")

    # Run stress.
    run_root = Path(args.output_dir)
    corpus_root_path: Optional[Path] = Path(args.corpus_root) if args.corpus_root else None
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
    )
    # Fill in qdrant + audit info.
    qpoints_after = get_qdrant_points(args.qdrant_host, args.qdrant_port)
    report.qdrant_points_before = qpoints_before
    report.qdrant_points_after = qpoints_after
    report.qdrant_points_delta = qpoints_after - qpoints_before if qpoints_after >= 0 and qpoints_before >= 0 else 0
    report.audit_qdrant_write_failed = scan_audit_for_failures(audit_path, trace_ids)

    # Save JSON
    out_json = run_root / f"stress-report-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    run_root.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"[STRESS] report json: {out_json}")

    # Print summary
    print("\n========== STRESS SUMMARY ==========")
    print(f"  total docs            : {report.n_total}")
    print(f"  completed             : {report.n_completed}")
    print(f"  failed                : {report.n_failed}")
    print(f"  rejected (notify 4xx) : {report.n_rejected}")
    print(f"  pending (timeout)     : {report.n_pending_at_timeout}")
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


if __name__ == "__main__":
    sys.exit(main())
