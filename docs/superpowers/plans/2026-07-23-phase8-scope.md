# Phase 8 — Scope

> Status: closed (2026-07-24)
> Date: 2026-07-23
> Author: Claude (Sonnet)
> Predecessor: Phase 7 closed (`phase7` tag at 99c77f5)
> Closure: `phase8` tag force-moved per Decision §3 precedent; see `CHANGELOG.md` for the full commit list.

---

## Context

Phase 7 closed 2026-07-23 with 12 commits spanning T1–T8 (closure +
CHANGELOG). The `phase7` tag now reflects delivered state, not
snapshot time, per Decision §3.

Two follow-up artefacts split the previously-frozen deferral list into
two distinct scopes:

- `ekrs-handbook.md §6.1` — Phase 6+ deferral list (11 active items
  + 2 marked closed by Phase 7). Restart requires new plan doc.
- `ekrs-handbook.md §6.2` (new 2026-07-23) — **Post-deploy tech debt
  registry** (6 items: Qdrant optimization, multi-region, large-scale
  batch, mTLS, audit.log remote archival, bge-m3 vendor distribution).
  Frozen until production deployment + real load profile exists.

This Phase 8 plan doc scopes the **deployment-readiness** items —
the 5 candidates approved 2026-07-23 from §6.2 / §6.1.

---

## Goals

1. **Production hardening** — prevent misuse and credential staleness
   in a live environment.
2. **Smoke test completeness** — close the Phase 6C T4 gap where
   successful ingestion was never exercised in CI.
3. **Regression coverage** — extend the 42-case golden set so new
   constraint types ship with representative cases.
4. **Performance baseline** — capture chunker behavior at 10k
   document scale so future scale-out decisions have data.

Phase 8 closes when all 5 tasks (T8-1 .. T8-5) are merged + a fresh
`phase8` tag is force-moved per Decision §3 precedent from Phase 7.

---

## Recommended Phase 8 structure

| Task | Title | Effort | Risk | Dependencies |
|------|-------|--------|------|--------------|
| T8-1 | SlowAPI rate limiting (per-IP, `/v1/*` only) | small | low | none |
| T8-2 | Secret rotation SOP + validator | small | low | none |
| T8-3a | Dockerfile: vendor bge-m3 ONNX into RAG image | medium | medium | model file availability |
| T8-3b | successful ingestion smoke script (host + CI) | small | low | T8-3a |
| T8-4 | Golden set extension: +8 cases for new constraint types | small | low | T8-3b (or independent if smoke passes locally) |
| T8-5 | chunker perf baseline at 10k+ documents | medium | low | none |

Tasks are **independent enough to ship in any order**, except:

- T8-3a → T8-3b (image must contain model before smoke can run)
- T8-3b provides CI confirmation that constraint-engine changes don't
  break ingestion; **highly recommended** before T8-4 (which exercises
  the same end-to-end path).

Recommended order: T8-1, T8-2, T8-3a, T8-3b, T8-4, T8-5.

---

## Tasks in detail

### T8-1 — SlowAPI rate limiting

**Scope.** Add SlowAPI as a runtime dependency and apply a default
rate limit (`60/minute` per peer IP) to all `/v1/*` routes, with
explicit exemption for `/healthz`, `/metrics`, `/docs`, `/redoc`,
`/openapi.json` (high-frequency probes + Swagger UI scrapers that
would otherwise trip the limiter).

**Files touched.**
- `rag/pyproject.toml` — add `slowapi>=0.1.9` to `[project]`
  dependencies (production-grade, not dev-only).
- `rag/ekrs_rag/main.py` — install limiter, register exception
  handler, attach `dependencies=[Depends(rate_limit)]` on the
  `/v1` router prefix or per-route.
- `rag/ekrs_rag/api/routes/health.py` (or wherever `/healthz` lives)
  — exclude from limiter.
- `rag/tests/unit/test_rate_limit.py` (new) — RED→GREEN per task
  per V2 fixture convention (`docs/solutions/best-practices/ekrs-tdd-fixture-convention.md`).
- `docs/USAGE.md` — document the limit + the `Retry-After` header
  behavior so operators know to expect 429.

**Acceptance.** `tests/unit/test_rate_limit.py` passes; `/v1/*`
returns 429 after the configured burst; `/healthz`, `/metrics`,
`/docs`, `/redoc`, `/openapi.json` unaffected; mypy clean on changed
files; full test suite green.

**Out of scope.** Per-user limits (would need auth — outside the
threat model since the service uses `X-Parser-Token` for write paths
and `X-Admin-Key` for admin). Distributed rate limiting (would need
Redis-backed limiter; current Redis is used for locks, not counters).

### T8-2 — Secret rotation SOP + validator

**Scope.** Write the operator-facing SOP for rotating
`PARSER_TOKEN` and `ADMIN_KEY` without downtime. Ship a small
**offline validator** (`scripts/validate_rotation.py`) that:
- Reads old + new tokens from CLI args (never from disk).
- Computes hamming-style distance and rejects if too similar (catch
  typos).
- Emits a JSON report with rotation timestamp + which env vars must
  be updated on which deployment units.

**Files touched.**
- `docs/SECRET-ROTATION.md` (new) — human SOP: pre-rotation checklist,
  order of operations (Parser token first, then Admin key, then RAG
  restart), rollback steps, audit-trail verification.
- `scripts/validate_rotation.py` (new) — validator; reads from
  `argparse`, no file I/O, prints JSON to stdout.
- `rag/tests/unit/test_validate_rotation.py` (new) — covers similarity
  threshold (≥80% prefix → reject), accepts distinct strings,
  rejects equal strings, parses args correctly.

**Acceptance.** Validator catches typo-grade rotations (e.g. new token
differs from old by 1 char); SOP reviewed by ops; tests pass; mypy
clean.

**Out of scope.** Automatic rotation (would need a secrets manager
integration; current threat model is manual rotation per the SOP).
Breaking change to the auth protocol (the SOP preserves the existing
header contract).

### T8-3a — Dockerfile: vendor bge-m3 ONNX into RAG image

**Scope.** Extend `deployment/docker-compose.yml`'s RAG image build
to copy the vendored `rag/models/bge-m3/` directory (model.onnx +
sparse_linear.pt + bge-m3.sha256) into the image at a stable path.
Ensure the SHA256 verification step inside `EmbeddingService._load()`
runs against the in-image copy, not the host mount. Image size
acceptable since bge-m3 is ~2.1 GB; production image is the right
place for it.

**Files touched.**
- `deployment/Dockerfile.rag` (or wherever the RAG image is built)
  — add `COPY rag/models/bge-m3/ /opt/ekrs/models/bge-m3/` step;
  set `EMBEDDING_MODEL_DIR=/opt/ekrs/models/bge-m3` env var.
- `rag/ekrs_rag/retrieval/embedding_service.py` — verify the
  `EMBEDDING_MODEL_DIR` env var is consulted before the hardcoded
  `DEFAULT_MODEL_DIR`. If not, add a constructor arg override (no
  breaking change to existing callers — only the Docker entrypoint
  supplies it).
- `rag/tests/integration/test_docker_image.py` (new, marked `heavy`)
  — builds the image locally (skipped if Docker not available) and
  verifies the model file is present + SHA matches.
- `docs/DEPLOYMENT.md` — document the image build arg + the new env
  var.
- `.github/workflows/build-rag-image.yml` (new, or amend existing)
  — trigger on change to `rag/models/bge-m3/**` OR `bge-m3.sha256`.
  Without this trigger, model edits that don't rebuild the image
  would silently bypass the new vendored path. The cache-bust
  pattern from Phase 7 T7 (`sha256(model.onnx)|sha256(sparse_linear.pt)`)
  auto-invalidates runtime cache when SHA changes; the new image
  build trigger extends that to "image rebuilt when SHA changes".

**Acceptance.** `docker compose up` produces a RAG image with
bge-m3 ONNX baked in; integration test confirms SHA256 match; CI
rebuilds the image on model file change; heavy tests in CI nightly
run pass; mypy clean on changed files.

**Out of scope.** Model quantization / compression (different concern;
post-deploy optimization per §6.2 PD-1). Multi-arch images
(separate effort).

### T8-3b — successful ingestion smoke script

**Scope.** New script `scripts/smoke_ingestion.sh` (or `.py`) that
exercises the full happy path: parser notification → ingest →
upsert to Qdrant → callback (or simulated callback). The script must
work against a `docker compose up` stack (uses the in-image model
from T8-3a) and produce a structured PASS/FAIL report.

**Files touched.**
- `scripts/smoke_ingestion.sh` (new) — wraps curl + python helpers.
- `scripts/lib_smoke.py` (new, helper) — JSON helpers for the
  notification payload + status polling + diff verification.
- `Makefile` — add `make smoke-ingestion` target.
- `docs/DEPLOYMENT.md` — append the smoke step to the post-deploy
  checklist.

**Acceptance.** Running `make smoke-ingestion` against a fresh
`make dev` stack produces `PASS` end-to-end within 60s; failures
return non-zero exit + cite which step failed; no token logged
(uses the same safe-piping pattern as `make mock-notify`).

**Failure-signal contract.** The script MUST exit non-zero when any
of the following are observed (not just network failures):
1. `/v1/ingestion/notify` returns non-2xx after the configured
   retries (cover transport-level + parser-scheme-level errors).
2. `/v1/ingestion/status` polling never reaches a `completed`
   terminal state within the timeout (default 30s).
3. The terminal status reports a `qdrant_write_failed` audit
   event in `audit.log` even if HTTP returned 200 (silent write
   failure path caught by Phase 7 T1 integration test).
4. The callback `POST /v1/ingestion/notify/callback` (real or
   mock) returns non-2xx OR the mock callback received a payload
   with `status != "completed"`.

Each step emits a clearly attributed error line
(`[STEP N] <message>` format) so triage doesn't require reading
the full output.

**Out of scope.** Production smoke against real Parser (that belongs
to a separate ops deployment runbook, not the repo).

### T8-4 — Golden set extension

**Scope.** Add 8 new golden cases covering:
1. Negative temperature (cryogenic — Kelvin range).
2. Two-pressure constraint pair with different scopes (national vs
   industry).
3. Material limit with non-numeric unit (`%` elongation).
4. Multi-condition compound (T AND P simultaneously).
5. Empty query (must 400, not 500).
6. Invalid scope path (must filter or 400 — not silently accept).
7. Strict-mode refusal when context insufficient (R6).
8. Concurrent identical queries (must be deterministic — replay test).

**Files touched.**
- `rag/tests/golden_set/golden_set.json` — append 8 entries;
  maintain backward compat with Phase 6A §9.1 format.
- `rag/tests/golden_set/test_golden_set.py` (if exists) — adjust the
  expected count constant from 42 → 50.
- `ekrs-handbook.md §9.1` — append the new case descriptions per the
  Phase 6A convention.

**Acceptance.** `make golden-test` reports 50 cases, all pass; mypy
clean; existing 42 cases unchanged (no regressions in regression set).

**Out of scope.** Adversarial / fuzz cases (separate effort; fuzz
testing framework not yet adopted). Constraint type additions
beyond the 8 candidates (each new type should be one task / one PR).

### T8-5 — chunker perf baseline at 10k+ documents

**Scope.** Add `benchmarks/test_chunker_10k.py` (marked `heavy`,
excluded from default `make test`) that:
1. Generates 10k synthetic documents via deterministic seed.
2. Runs the chunker end-to-end with timing.
3. Reports p50 / p95 / p99 chunk durations + memory peak (via
   `tracemalloc` or `resource.getrusage`).
4. Writes a JSON report to `benchmarks/results/chunker-10k-<timestamp>.json`.
5. Asserts p99 < a configurable threshold (default 5s/document —
   ship the baseline number; tune later).

**Files touched.**
- `benchmarks/test_chunker_10k.py` (new, `pytest.mark.heavy`).
- `benchmarks/README.md` (new) — how to run; how to interpret.
- `Makefile` — add `make bench-chunker` target.
- `docs/DEPLOYMENT.md` — note the baseline for future comparison.

**Acceptance.** Heavy test runs cleanly on Python 3.11 + vendored
bge-m3 environment; baseline JSON written; threshold assertion passes
on the current chunker; CI nightly reports the number.

**Calibration note.** The `5s/document` threshold is a placeholder.
Before committing the assertion, run the benchmark once locally on
the dev machine, capture the actual p99 from the JSON report, and
either:
- (a) accept the placeholder and tighten later (fine for Phase 8 —
  the goal is a baseline number, not a tightened SLA), or
- (b) set the threshold to 1.5 × observed p99 so the assertion
  guards against regressions without false-failing.
Either way, the baseline JSON report is the deliverable; the
threshold is a guardrail, not a target.

**Out of scope.** Comparing alternative chunking strategies
(sentence-aware vs token-aware) — that's a research question, not a
baseline. Reducing memory peak — that requires profile data not yet
collected.

---

## Decisions (locked 2026-07-23)

| # | Item | Decision | Reason |
|---|------|----------|--------|
| 1 | **Rate limit scope** | (a) Per-peer-IP, all `/v1/*` routes, default 60/min, exempt `/healthz` + `/metrics` | Per-IP is sufficient threat model for an authenticated service (Parser + Admin have their own auth). Per-user requires session auth — out of scope. Default 60/min matches typical API gateway behavior; ops can tune via env var. Health probes exempted because k8s hits them every 5-10s. |
| 2 | **Secret rotation cadence** | (b) Manual rotation per SOP, every 90 days, no auto-rotation | Service has no secrets manager integration. Manual rotation + offline validator is the lowest-effort correctness step. Auto-rotation would require Vault/AWS SM integration (separate effort). 90-day cadence matches typical compliance windows (SOC2 / ISO27001). |
| 3 | **Golden set growth rule** | (c) Every new constraint type ships with ≥3 golden cases, appended to `golden_set.json` and `ekrs-handbook.md §9.1` | Prevents the "added a feature, forgot to test" regression pattern. 3 cases is the minimum to cover (i) happy path, (ii) edge case, (iii) conflict-with-existing. Adding a type without cases is now blocked by convention, not by tooling — anyone reviewing the PR can flag it. |

---

## Out of scope (defer)

### Already excluded — see §6.2
- Qdrant index optimization (PD-1)
- Multi-region / replication (PD-2)
- Large-scale batch processing (PD-3)
- Service-to-service authn / mTLS (PD-4)
- audit.log remote archival (PD-5)
- bge-m3 ONNX vendor distribution strategy (PD-6) — *note: T8-3a
  ships the model into the Docker image but does not decide the
  broader distribution strategy; PD-6 remains open for Phase 9+.*

### New deferrals identified in this planning
- **Per-user rate limiting** — needs session auth; not in threat model.
- **Distributed rate limiting** — would need Redis-backed limiter;
  current Redis usage is locks only.
- **Auto-rotation via Vault/AWS SM** — separate integration effort.
- **Adversarial fuzz testing** — no fuzz framework adopted yet.
- **Alternative chunking strategies benchmark** — research question.

---

## Dependencies

```
T8-1 ── independent
T8-2 ── independent
T8-3a ── independent (model file is already vendored in repo)
T8-3b ── depends on T8-3a (image must contain model)
T8-4  ── recommended after T8-3b (uses the same end-to-end path,
         so T8-3b passing is a precondition for confidence in T8-4)
T8-5  ── independent (uses heavy test infra, not the smoke infra)
```

T8-1, T8-2, T8-5 can ship in any order; T8-3a → T8-3b is a hard
ordering; T8-4 prefers T8-3b but is not strictly dependent.

---

## Tag strategy

Inherit the Phase 7 precedent (`phase7` represents delivered state
per Decision §3):

- After all 5 tasks land, force-move `phase8` to current HEAD with an
  updated annotation.
- Add a `phase8.1` historical anchor at the T8-3a commit (the
  "docker model integration" milestone) for traceability.
- `phase7` is NOT touched (frozen at 99c77f5).
- New `CHANGELOG.md` entry written per the Keep-a-Changelog format
  Phase 7 established.

---

## Verification

After all 5 tasks land:

```bash
# Unit + integration (default)
make test

# Heavy (nightly, requires Docker + Python 3.11 + vendored model)
make heavy-test
make smoke-ingestion
make bench-chunker
make golden-test   # now reports 50 cases
```

Tag force-move:

```bash
git tag -f -a phase8 HEAD -m "Phase 8: deployment-readiness hardening + smoke + regression + perf baseline. Force-moved from <initial T8-1 commit> to HEAD. phase7 stays at 99c77f5; phase8.1 stays at <T8-3a commit>."
git push --force origin refs/tags/phase8:refs/tags/phase8
```

---

## Resolved questions

- **T8-3 model distribution** — confirmed Docker layer is the right
  approach for Phase 8; broader git-lfs-vs-CDN-vs-image decision (PD-6)
  stays open as §6.2 item.
- **T8-4 minimum cases** — convention is ≥3 per new constraint type;
  Phase 8 ships 8 cases across 6 distinct scenarios (some overlap by
  type, some don't).
- **Rate limit exemption list** — `/healthz` and `/metrics` only;
  `/docs` and `/redoc` are protected by IP rate limit too (low cost;
  prevents Swagger scrapers from exhausting the quota).

---

## Closing (2026-07-24)

All 5 tasks T8-1 through T8-5 merged to master. One cross-phase
debt cleanup commit (`193b0db` — IngestionOutcome.rag_status Literal
widening) follows; it clears a 3-error mypy failure pre-dating
Phase 8.

### Tag force-move

- `phase8` annotated tag created at HEAD at Phase 8 closure,
  force-pushed to remote. Represents *delivered state*, not
  snapshot time, following Decision §3 precedent from Phase 7.
- `phase8.1` annotated tag at `7151f13` (T8-3a — bge-m3 vendoring
  milestone) — historical anchor. **Do not move.**
- `phase7` stays at `99c77f5` (frozen at Phase 7 closure). **Do not move.**
- `phase7.1` stays at `41c2d54` (T2 historical anchor). **Do not move.**

Full commit list lives in `CHANGELOG.md` under `[phase8]`.

### Open Phase 6+ deferrals (unchanged)

- Qdrant index optimization (PD-1)
- Multi-region / replication (PD-2)
- Large-scale batch processing (PD-3)
- Service-to-service authn / mTLS (PD-4)
- audit.log remote archival (PD-5)
- bge-m3 ONNX vendor distribution strategy (PD-6)