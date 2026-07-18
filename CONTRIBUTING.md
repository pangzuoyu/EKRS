# Contributing

> How to extend EKRS safely. Every change must respect the eight Iron Rules
> (see `ekrs-handbook.md` §Iron Rules). This guide walks through the three
> extension points that touch production behavior.

---

## Before you open a PR

EKRS enforces a three-gate CI pipeline. All three must be green:

| Gate | Command | Purpose |
|------|---------|---------|
| Static analysis | `make lint` | flake8 (max-line=120) + mypy on `shared/` and `rag/` |
| Unit + integration | `make test-cov` | pytest with coverage report; **gate ≥ 85%** |
| Heavy (nightly) | `make heavy-test` | Real bge-m3 model load; gated by Python 3.11 runners |

Local equivalents:

```bash
make lint
make test-cov
cd rag && pytest tests/ -m heavy -v   # requires Python 3.11
```

Heavy tests are excluded from PR CI by default (model load is slow and needs
the bge-m3 ONNX vendor tree). They run on every nightly against the tip of
`master`.

---

## Extension 1 — Adding a new Hint extraction pattern

The constraint engine extracts numeric hints from chunk text via
`rag/ekrs_rag/constraint_engine/parser.py`. To add a new pattern:

1. **Read the existing patterns first.** `parser.py` uses regex-based
   `HintExtractor` classes — one per operator family (`range`, `gt`, `lt`,
   `scalar`). Match that style.
2. **Cover all three units** if the pattern is numeric: emit `unit` on the
   `NumericHint`; the solver's normalizer handles affine conversion (F→C is
   affine, **not** scalar).
3. **Add a fixture to the golden set** under `rag/tests/golden_set/v2/` —
   one `.json` per case, following the existing schema
   (`query`, `expected_branches`, `note`).
4. **Wire the new pattern through three-gate pipeline** (recall → extract →
   solve) — see `ekrs-handbook.md` R3. The pipeline must still block when
   any gate fails; do not short-circuit on recall success.
5. **Write a unit test** under `rag/tests/unit/test_parser.py` that loads
   the new fixture and asserts the `Hint` shape; **then** an integration test
   in `tests/integration/` that exercises the full pipeline with one chunk.
6. **Verify priority dedup** still works (R7): dedup key is
   `(parameter, operator, value, unit)` — `scope_path` does **not** enter the
   key.

Checklist:

- [ ] `make test-cov` still ≥ 85%
- [ ] Golden set: `make golden-test` passes (includes your new case)
- [ ] No `except Exception` swallow in production code paths (Iron Rule R3)
- [ ] Audit event emitted for any new failure branch (Phase 5 observability)

---

## Extension 2 — Adding a new Qdrant index field

Index payloads live in two places that **must stay in sync**:

| File | Role |
|------|------|
| `shared/ekrs_shared/models.py` | `Chunk` / `NumericHint` Pydantic models |
| `rag/ekrs_rag/retrieval/qdrant_client.py` | `QdrantManager.upsert_chunks` payload assembly |

Procedure:

1. **Add the field to the Pydantic model first** (`Chunk` or `NumericHint`).
   Use a non-required type with default to preserve back-compat with
   already-ingested chunks.
2. **Update `upsert_chunks`** in `qdrant_client.py` to write the field into
   the Qdrant payload dict. Payloads are flat (no nested objects); use
   `_flatten_payload_for_qdrant()` helper.
3. **If the field is filterable**, register it in
   `QdrantManager.ensure_collection` via `create_payload_index(...)`. Note:
   Phase 6B **does not** auto-create indexes — manual call is required.
4. **Update `retriever.py`** if the field changes scoring or filtering
   (scope-priority composite is in `retriever.py:_rank_by_scope`).
5. **Write a QdrantManager unit test** under
   `rag/tests/unit/test_qdrant_client.py` that exercises
   `upsert_chunks → search → assert payload`.

Iron Rule R8 reminder: the **index layer may only filter illegal status**
(e.g., `lifecycle.status == 'illegal'`); it must never trim `authority` /
`scope_path` priority.

If the new field changes the embedding vector dimension (e.g., switching
back from 1024d to 384d), see `docs/DEPLOYMENT.md` §Embedding dim migration
and the rollback section of `docs/CHANGELOG.md` — **dim changes are NOT
backward-compatible**.

---

## Extension 3 — Adding a new audit event

The audit event registry is **frozen at 16 schemas** (handbook §16). Adding
a new event name is forbidden; broadening the semantics of an existing event
is allowed.

If you need a new event:

1. Check whether an existing event already covers your case (e.g.,
   `qdrant_write_failed` broadened in Phase 6B to cover read/write/delete via
   an `operation` field — use it, don't add `qdrant_read_failed`).
2. If broadening is the right move: extend the schema in
   `shared/ekrs_shared/audit.py`, document the back-compat note in
   `ekrs-handbook.md` §16, and bump the schema version in the audit writer.
3. If a brand-new event is unavoidable (rare): requires spec review; the
   Iron Rules section explicitly forbids growth without handbook amendment.

Adding an optional **field** to an existing event (e.g., Phase 6A's
`lineage_snapshot`, `conflict_details`) is allowed and back-compat — use
`_PHASE6A_OPTIONAL` spread in the base audit writer.

---

## Code conventions

- Python 3.11+ required (see `docs/DEPLOYMENT.md` §Python version).
- `portion.Interval` uses **factory functions** (`portion.closedopen`,
  `portion.openclosed`, `portion.open`) — never `Interval(left=, right=)`.
- All logs are structured JSON via `python-json-logger` (handbook §12).
- Mutations forbidden: always return new objects (immutability rule).
- Functions ≤ 50 lines; files ≤ 800 lines.

---

## Commit messages

Conventional Commits:

```
<type>(<scope>): <subject>

<optional body>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`.

Attribution is disabled globally in `~/.claude/settings.json`.

---

## Review checklist (for the author before requesting review)

- [ ] `make lint` clean
- [ ] `make test-cov` ≥ 85%
- [ ] No hardcoded secrets (no tokens, no `PARSER_TOKEN` literals)
- [ ] No `console.log` / debug prints (use `logger.debug` under `EKRS_DEBUG`)
- [ ] No Iron Rule violations (see handbook §Iron Rules)
- [ ] Audit event emitted for any new failure branch
- [ ] Docs updated (README, USAGE, ARCHITECTURE, CHANGELOG as applicable)
- [ ] If behavior change: spec amendment in `ekrs-handbook.md`
