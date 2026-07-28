# Phase 9 — Offline bulk-import mode for `scripts/live_stress_60.py`

> Status: plan approved (2026-07-28)
> Date: 2026-07-28
> Author: Claude (Sonnet)
> Predecessor: Phase 9 T9 stress verified 60/60 + 200/200 on real corpus (`phase9` tag at bdb233f; HEAD 3bca08a)
> Scope: Add `--mode offline` + `--mode retry-failed` for production first-deploy of 25k+ real corpus docs

## Context

The Phase 9 stress script (`scripts/live_stress_60.py`) currently has a
single fast mode (`--concurrency 1 --pace-ms 2000`, 60s timeout, 1s/2s
retry backoff) tuned for *verification* on small corpora. The user now
needs a **production offline bulk-import mode** for first-deploy
ingestion of 25k+ real corpus docs where:

- The time window is hours-to-days (not minutes)
- Reliability >> throughput
- Operators need to **resume** interrupted runs and **re-run** only the
  failed docs

The existing stress mode is **load-tested** (60/60 + 200/200 verified)
but unsuitable for production first-deploy because:

- It appends `_r<run_id>` to every doc_hash — each run produces fresh
  chunks, so re-runs double-write the corpus
- It has no `failed_docs.txt` output — operators can't tell which docs
  failed
- It has no resume logic — restarting mid-run re-ingests everything
- Its 1s/2s retry backoff is too aggressive for the intermittent
  uvicorn slow-accept at slow-pacing throughput

The user gave a concrete spec: `--mode offline` with 3s pacing, 2
retries with 2s/4s backoff, `--status-timeout 180`,
`--poll-interval 3.0`, failed-docs logging, resume via audit log +
Qdrant, separate `--mode retry-failed` for the failed list.

## Design

### Mode profile (single source of truth)

Replace the bare module-level constants (`NOTIFY_RETRY_BACKOFF_S`,
`STATUS_TIMEOUT_S`, `STATUS_POLL_S`, `DEFAULT_PACE_MS_CORPUS`) with a
frozen dataclass `ModeProfile` that bundles all per-mode knobs. Three
profiles:

```python
@dataclass(frozen=True)
class ModeProfile:
    pace_ms: int                # notify pace per worker (ms)
    retry_backoff_s: tuple[float, ...]   # notify retry backoff schedule
    status_timeout_s: float     # per-doc /status poll deadline
    poll_interval_s: float      # /status poll cadence
    resume: bool                # filter out already-ingested docs at start
    write_failed: bool          # write failed/pending docs to --output-failed at end
    id_suffix: bool             # append _r<run_id> to doc_hash (stress only)

STRESS_PROFILE      = ModeProfile(2000, (1.0, 2.0), 90.0,  2.5, False, False, True)
OFFLINE_PROFILE     = ModeProfile(3000, (2.0, 4.0), 180.0, 3.0, True,  True,  False)
RETRY_FAILED_PROFILE = ModeProfile(5000, (4.0, 8.0), 180.0, 3.0, False, True,  False)
```

**Why `id_suffix=False` for offline + retry-failed**: The stress mode's
`_r<run_id>` suffix exists to *bypass* Qdrant SHA-based dedup so each
stress run produces a measurable `qdrant_points_delta`. For offline
first-deploy, the opposite is wanted — same chunk text → same SHA →
same point ID → idempotent re-write. Without the suffix, the existing
`pipeline.py:126` `get_ingestion_status(doc_hash)` check recognizes the
doc is already in Qdrant and short-circuits, so re-running an offline
mode after a crash is safe.

**Operational constraint to document in --help:** because offline mode
uses the raw base doc_hash (no `_r<run_id>` suffix), two offline runs
against the same corpus produce ZERO net Qdrant growth on the second
run — even after a `delete_old_versions` or operator-driven re-index.
If an operator wants a clean re-ingest of the entire corpus (e.g.,
after a model upgrade), they must either (a) use a doc-to-md re-export
that produces fresh doc_ids, or (b) add a `--force-id-suffix` flag in
a follow-up commit (out of scope here). Document this in the
`--mode offline` argparse help.

### CLI surface

```
--mode {stress,offline,retry-failed}            # default stress
--resume / --no-resume                          # default --resume; ONLY honored in offline mode
                                                 # (stress never resumes; retry-failed uses the
                                                 # input file directly — --resume is a no-op there)
--input-failed PATH                             # default: failed_docs.txt; only used in retry-failed
--output-failed PATH                            # default: failed_docs.txt; written in offline+retry-failed
--audit-via-docker CONTAINER                    # NEW; matches --docker-target pattern
                                                 # (the audit log at /app/rag/audit.log is NOT
                                                 # bind-mounted to the host — see docker-compose.yml:60-61,
                                                 # only parsed_lib is mounted)
```

`--audit-via-docker` is the **supplementary** mechanism for the
audit-log half of the resume check in offline mode. Qdrant dedup
(`_filter_qdrant_ingested`) is always-on and works without
`--audit-via-docker`; audit log adds detection of
`ingestion_completed` events from prior runs whose Qdrant status might
have been rotated/cleaned. If `--audit-via-docker` is unset and
`--mode offline`, the script emits an INFO stderr note:
`audit log not scanned (--audit-via-docker not set); resume check
uses Qdrant dedup only`. Hard fail only when `--audit-via-docker` IS
set but docker exec fails.

### Resume-check algorithm (offline mode only)

```
1. Read audit log JSONL (via docker exec if --audit-via-docker is set,
   else from --audit-path on host). MUST read BOTH the active audit.log AND
   the rotated audit.log.1.gz...audit.log.5.gz files (per
   rag/ekrs_rag/observability/audit.py:44-50, audit rotates at 100MB × 5
   gzip backups). With 25k+ docs each emitting 4-6 audit events, the active
   file can rotate mid-run; missing rotated completions → false-negative
   resume (re-ingests already-done docs). When --audit-via-docker is set,
   use `docker exec -i <container> sh -c 'cat /app/rag/audit.log; for f in
   /app/rag/audit.log.*.gz; do [ -f "$f" ] && zcat "$f"; done'`.
2. Collect set INGESTED = {e["doc_id"] for e in entries if e.get("event") == "ingestion_completed"}.
3. For each candidate payload, check if its doc_id (the value used in the
   notify payload) is in INGESTED. If yes → skip (count as n_skipped_resumed).
4. Also dedup against Qdrant: query /v1/ingestion/status/{doc_hash} for each
   candidate, add doc_hash to the skip set if status is "success" or "completed".
   Qdrant is the PRIMARY source for resume (always-on, queryable per doc);
   audit log is supplementary (helps detect ingestion_completed events from
   prior runs whose Qdrant status was rotated or cleaned).
5. If --audit-via-docker is set but docker exec fails (container not running,
   docker CLI not on PATH, permission denied), the script MUST fail loudly
   (`fatal: --audit-via-docker=<container> docker exec failed: rc=N stderr=...`)
   rather than silently empty the INGESTED set — otherwise the operator gets
   a false "all docs pending" resume and re-ingests everything.
```

Audit log field check (verified at `2026-07-28`): the audit log uses
`event` (not `event_type`), and `ingestion_completed` carries `doc_id`
(not `doc_hash`). The existing `scan_audit_for_failures` at
`live_stress_60.py:147` has a **pre-existing bug** (checks
`event_type`) — fixed in this commit (see Files-to-modify §1).

### failed_docs.txt format

- One doc_hash per line, no header
- Replace semantics (each run writes the full set of failed docs from
  THAT run; not appending)
- Statuses that count as "failed": `rejected` (HTTP 4xx/5xx),
  `failed` (terminal failed from /status), `pending` (poll timeout),
  `notified_failed` (submit error). Equivalently: any outcome whose
  `status` is NOT in `{completed, success, skipped_resumed}`.
- File is line-sorted by doc_hash for diff-friendly output
- Atomic write: serialize to `<path>.tmp` then `os.replace()`
  (POSIX-atomic rename) so a mid-write crash doesn't truncate the
  file. If the operator passes `--output-failed /existing/failed_docs.txt`,
  the existing file is overwritten only after the tmp file is fully
  written + fsynced.

### retry-failed mode input handling

`failed_docs.txt` lines are the full doc_hash from the original
attempt (which may include `_r<run_id>` suffix if produced by a stress
run). Going with **Strategy A** — failed_docs.txt reflects the actual
IDs that were attempted, which is what an operator wants to see.

**Suffix-strip heuristic — precise regex required.** The suffix is
added by `run_stress` as `_r<YYYYMMDDTHHMMSSZ>` (15 alphanumeric chars
after `_r`), e.g. `_r20260728T092520Z`. Match the regex
`re.compile(r"_r\d{8}T\d{6}Z$")` exactly — never a generic strip.
This avoids misinterpreting corpus doc_ids that happen to end in
`_r...` (e.g. `asme_bpvc_r2015` would be misread as `asme_bpvc` +
bogus suffix if a generic strip were used).

Implementation:

```python
import re
_RRUNID_SUFFIX_RE = re.compile(r"_r\d{8}T\d{6}Z$")

def _strip_runid_suffix(doc_hash: str) -> str:
    """Remove _r<YYYYMMDDTHHMMSSZ> suffix if present. Returns base id."""
    return _RRUNID_SUFFIX_RE.sub("", doc_hash)

def _resolve_failed_doc_paths(failed_hashes, corpus_root):
    """For each failed doc_hash, find the corpus JSONL source.
    Strips _r<run_id> suffix (precise regex) and matches against
    <corpus>/<id>/data.jsonl. Returns [(doc_hash, jsonl_path)] for hits
    and surfaces a fatal listing for misses."""
```

**retry-failed resume semantics:** retry-failed RE-NOTIFIES every doc
in `failed_docs.txt` even if it's already in Qdrant. The RAG service's
`pipeline.py:126` `get_ingestion_status` check short-circuits
already-ingested docs to no-op (logs "Already indexed" but does not
re-upsert), so this is safe but produces noisy INFO log lines.
Operators who want to skip already-ingested in retry-failed should use
`--mode offline --resume` instead (which filters at the script level).
Document this in `--help` text.

**Missing/empty input file handling:**

- File missing → `fatal: --input-failed <path>: file not found` (exit 2)
- File exists but empty (zero non-blank lines) → `fatal: --input-failed
  <path>: empty input (0 docs)` (exit 2)
- File contains lines but none match corpus-root → `fatal:
  --input-failed <path>: 0 of N entries matched corpus-root=<path>;
  first missing: <hash>` (exit 2)

### Mode dispatch in main()

```python
def main():
    args = parse_args()
    profile = MODE_PROFILES[args.mode]
    if args.mode == "retry-failed":
        failed_hashes = read_failed_docs(args.input_failed)
        if not failed_hashes:
            fatal("retry-failed: empty input file")
        # n_docs = len(failed_hashes); payloads restricted to those
    # Otherwise: existing payload-build logic, with resume filter applied
    # before dispatch if profile.resume.

    report = run_stress(..., profile=profile, ...)
    if profile.write_failed:
        failed_hashes = {o.doc_hash for o in outcomes if o.status in FAILED_STATUSES}
        write_failed_docs(args.output_failed, failed_hashes)
```

## Files to modify

### 1. `scripts/live_stress_60.py` (single file)

**Constants section (lines ~38-72)**: Replace bare constants with
three `ModeProfile(...)` constants + `MODE_PROFILES` dict. Keep
`NOTIFY_HTTP_TIMEOUT_S` and `NOTIFY_RETRY_MAX` as global (they're
invariant across modes).

**New functions**:

- `get_ingested_doc_hashes(audit_path: Path, *, docker_target: Optional[str] = None) -> set[str]`
  — read audit log JSONL, return set of `doc_id` from `event ==
  "ingestion_completed"`. If `docker_target` is set, use
  `docker exec -i ... python3 -c` to read inside the container
  (matches the existing `write_jsonl_via_docker` pattern at line 298).
- `read_failed_docs(path: Path) -> set[str]` — read .txt file, return
  set of doc_hash values (strip whitespace, skip blank lines).
- `write_failed_docs(path: Path, doc_hashes: Iterable[str]) -> None` —
  atomic write (write to tmp + rename), sorted.
- `_resolve_failed_doc_paths(failed_hashes: set[str], corpus_root: Path) -> list[tuple[str, Path]]`
  — strip `_r<run_id>` suffix, match against
  `<corpus_root>/<id>/data.jsonl`, return
  `[(doc_hash, jsonl_path)]`.
- `_filter_qdrant_ingested(rag_url, doc_hashes: set[str]) -> set[str]`
  — query `/v1/ingestion/status/{doc_hash}` for each, return the subset
  with status `success|completed`.

**Modified functions**:

- `notify_one()` (line 375): add
  `retry_backoff_s: tuple[float, ...] = NOTIFY_RETRY_BACKOFF_S`
  keyword-only parameter. Default keeps existing behavior.
- `poll_status()` (line 503): add
  `poll_interval_s: float = STATUS_POLL_S` keyword-only parameter.
- `scan_audit_for_failures()` (line 147): **fix pre-existing bug** —
  change `entry.get("event_type")` → `entry.get("event")` to match the
  actual JSON key written by `AuditWriter` (confirmed via `audit.log`
  inspection at `2026-07-28`: the field is `event`, not `event_type`).
  Without this fix, the function silently returns 0 even when failures
  exist (false-negative). Tightly coupled to this file and to the same
  audit-log scan pattern that the new resume check uses — fixing here
  reduces future tech debt.
- `run_stress()` (line 538): add `mode_profile: ModeProfile` parameter.
  Internally:
  - When `profile.id_suffix is False`, skip the `_r{run_id}` suffix
    logic (use base doc_id).
  - When `profile.resume is True`, apply `_filter_qdrant_ingested()`
    + audit-log filter before dispatch.
  - When `profile.write_failed is True`, the caller (main) writes
    failed_docs.txt after run_stress returns.
  - Pass `retry_backoff_s=` to `notify_one()` and `poll_interval_s=`
    to `poll_status()`.
- `main()` (line 699): add argparse flags
  (--mode, --resume/--no-resume, --input-failed, --output-failed,
  --audit-via-docker). Branch on `--mode`:
  - `stress`: unchanged behavior
  - `offline`: prefix with resume check; suffix with failed_docs.txt
    write
  - `retry-failed`: read --input-failed, restrict n_docs to that set,
    prefix with audit-log skip (already-ingested), suffix with
    failed_docs.txt write

### 2. `CHANGELOG.md` (single entry)

Add to `[Unreleased]` → `### Added`:

- `--mode offline` (production first-deploy): 3s pace, 2 retries with
  2s/4s backoff, 180s status timeout, 3s poll interval, no
  `_r<run_id>` suffix (relies on Qdrant dedup for idempotency),
  `--resume` filter via audit log + Qdrant status check, writes failed
  docs to `failed_docs.txt`.
- `--mode retry-failed`: reads `failed_docs.txt`, re-processes only
  those docs with 5s pace + 4s/8s backoff.
- `--audit-via-docker CONTAINER`: reads audit log inside container via
  `docker exec` (required for offline mode resume check; the audit log
  is not bind-mounted to host in current deployment).

Add to `[Unreleased]` → `### Fixed`:

- `scan_audit_for_failures()`: pre-existing bug (`event_type` →
  `event`) — the audit log uses `event` as the JSON key, so the
  function silently returned 0 even when failures existed. Tightly
  coupled to the new audit-log scan pattern, fixed in this commit.
  False-negative only (no false-positives), so no behavioral regression
  risk.

## Reuse existing utilities

- `write_jsonl_via_docker(target_container, docs)` (line 298) — pattern
  reuse for `get_ingested_doc_hashes` audit-log read via `docker exec`
- `get_qdrant_points(host, port)` (line 133) — pattern reuse for
  `_filter_qdrant_ingested` (use Qdrant REST scroll endpoint to find
  ingested doc_hash values directly)
- `_make_outcome()` (line 489) and `DocOutcome` dataclass (line 75) —
  unchanged
- `read_corpus()` (line 233) — already iterates
  `corpus_root/<doc_id>/data.jsonl`; retry-failed mode filters this list

## Verification

After implementation, run on the existing 60-doc real corpus at
`/home/pangzy/code_project/doc-to-md/output/text`:

1. **Mode dispatch sanity** (`--mode stress`): existing 60/60 behavior
   preserved (regression check).
2. **Offline mode 5-doc smoke**:

   ```bash
   set -a; source ./.env; set +a
   python scripts/live_stress_60.py --mode offline --n 5 \
     --corpus-root /home/pangzy/code_project/doc-to-md/output/text \
     --docker-target deployment-rag-1 \
     --audit-via-docker deployment-rag-1 \
     --output-failed /tmp/offline_failed.txt
   ```

   - Expect: 5/5 completed, 0 qdrant_write_failed, paced at 3s/req, 0
     TRANSIENT FAIL (audit log check: `_r<run_id>` suffix absent in
     doc_ids).
3. **Resume check**: re-run with same args. Expect: 5 docs skipped
   (`n_skipped_resumed=5`), 0 dispatched, exit 0.
4. **Retry-failed with empty file**: `touch /tmp/empty.txt && python
   scripts/live_stress_60.py --mode retry-failed --input-failed
   /tmp/empty.txt ...` → exit 2 with "empty input file".
5. **Retry-failed with 1 fake entry**: write `nonexistent_doc_hash` to
   `/tmp/failed.txt` → exit 2 with "corpus missing for <hash>".
6. **Qdrant dedup invariant**: with `id_suffix=False`, two consecutive
   offline runs of the same N docs should produce `qdrant_points_delta
   = 0` on the second run (the service short-circuits via
   `get_ingestion_status`).
7. **Audit path fallback**: run with `--audit-via-docker` unset →
   expect stderr warning `audit log not scanned; resume checkpoint
   disabled`, but the Qdrant dedup check still filters (so resume
   partially works).

## Out of scope

- Adding a `/v1/admin/audit-log/clear` endpoint for run hygiene
  (operator runs `truncate -s 0 /app/rag/audit.log` instead)
- Multi-corpus offline runs (single --corpus-root only)
- Parallel offline runs across multiple containers (single RAG service
  assumption)
- Prometheus metrics for offline resume counts (could be added in a
  follow-up; not in this commit)
- Bookkeeping for partial progress within a single doc (e.g., resume
  mid-chunk) — out of scope, would require schema changes

## Future optimization note (recorded, not implemented)

`_filter_qdrant_ingested()` currently calls
`/v1/ingestion/status/{doc_hash}` once per candidate doc. At 25k docs
in sequential pacing (3s/req notify + 1 poll call/3s = ~6s/req, the
poll is the dominant cost), the resume check adds ~25k HTTP
round-trips upfront (~50 min at 100ms RTT) — still well under the
ingest budget but not free. A future optimization would batch via
Qdrant's `POST /collections/{name}/points/scroll` with a filter like
`doc_hash IN [...]` to fetch all statuses in one or two round-trips,
or use the existing `QdrantManager.get_ingestion_status(doc_hash)`
method directly (would require importing it into the script —
currently a stdlib-only design). Logged here so it's discoverable when
offline-mode throughput becomes a concern.

## GSTACK REVIEW REPORT

**Run:** 1 · **Status:** resolved
**Date:** 2026-07-28
**Reviewer:** gstack-review (eng-review pass)

### Findings (13 raised, 13 resolved)

| # | Severity | Confidence | Finding | Resolution |
|---|---|---|---|---|
| 1 | CRITICAL | 9/10 | Audit log resume reads only active `audit.log`, missing `audit.log.{1..5}.gz` rotated backups → false-negative resume on 25k-doc runs | ✅ Resume check now `cat`s active file + `zcat`s all rotated gz files via docker exec |
| 2 | CRITICAL | 8/10 | `_r<run_id>` strip heuristic ambiguous; corpus IDs containing `_r` would be corrupted | ✅ Replaced with precise regex `_r\d{8}T\d{6}Z$` (matches `run_stress` emit format) |
| 3 | CRITICAL | 7/10 | retry-failed re-notifies already-ingested docs (noisy INFO logs, no data corruption) | ✅ Documented as safe-via-Qdrant-dedup; operators wanting skip should use `--mode offline --resume` |
| 4 | HIGH | 9/10 | docker exec failure has no error handling in `get_ingested_doc_hashes` | ✅ Hard-fail when `--audit-via-docker` is set but exec returns non-zero |
| 5 | HIGH | 8/10 | `id_suffix=False` is a behavior change; operators can't re-ingest same corpus after model upgrade | ✅ Documented in `--help` text as operational constraint + follow-up `--force-id-suffix` |
| 6 | HIGH | 7/10 | Re-dispatch of pending-but-ingested docs wastes notify budget | ✅ Documented as expected (RAG short-circuits via `get_ingestion_status`) |
| 7 | HIGH | 6/10 | `pending` classification ambiguous — only `/status` timeout? what about `notified` (interrupted pre-poll)? | ✅ Reworded: "any outcome whose status is NOT in {completed, success, skipped_resumed}" |
| 8 | INFO | 9/10 | `--resume/--no-resume` default semantics unclear outside offline mode | ✅ Clarified: `--resume` is a no-op for stress + retry-failed |
| 9 | INFO | 9/10 | Stress mode CHANGELOG entry not addressed (it's unchanged → no entry needed, but worth noting) | ✅ Confirmed: CHANGELOG section only adds/changes for new features |
| 10 | INFO | 8/10 | Gzipped audit.log handling implicit (folded into finding #1) | ✅ Resolved via finding #1 |
| 11 | INFO | 7/10 | `--input-failed` missing file handling not specified | ✅ Added: fatal "file not found" / "empty input" / "0 of N matched" |
| 12 | INFO | 7/10 | `--audit-via-docker` warning overstated (Qdrant dedup is primary) | ✅ Reworded: Qdrant = primary, audit log = supplementary |
| 13 | INFO | 5/10 | `--force-id-suffix` not in plan (deferred to follow-up) | ✅ Listed under Future Work as next follow-up |

### Verdict

**PLAN QUALITY: 8.5/10.** All 3 CRITICAL findings resolved with
concrete implementations (no hand-waving). 4 HIGH findings addressed
either by fix-in-design or explicit documentation. 5 INFO findings
resolved with CLI text / docs. The plan is ready for implementation.

### Implementation order (recommended)

1. **Add `ModeProfile` dataclass + 3 profile constants** (lowest risk;
   foundation for everything else)
2. **Add `get_ingested_doc_hashes` + `read_failed_docs` +
   `write_failed_docs`** (pure functions; easy to unit-test mentally)
3. **Add `_filter_qdrant_ingested`** (HTTP helper; uses existing
   `/v1/ingestion/status/{hash}` endpoint)
4. **Parametrize `notify_one` + `poll_status`** (additive, non-breaking)
5. **Fix `scan_audit_for_failures` bug** (1-line, low-risk)
6. **Modify `run_stress` to accept `mode_profile`** (integration point)
7. **Add CLI flags + main() branching** (last — pulls it all together)
8. **Smoke test all 3 modes** (verify #5 #6 from Verification §)
9. **CHANGELOG entry + commit + push** (ghfast.top pattern, see Phase 8
   T8-3b precedent)

## Open questions

None — plan is approved and ready for implementation.