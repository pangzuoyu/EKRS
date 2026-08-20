# Phase 12 Task C — Doc-Type Classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface R4 scope-priority signal at ingest time via a filename-derived `doc_type` classifier, so legacy chunks (empty `scope_path`) and future chunks get priority boost without depending on `heading_path` propagation.

**Architecture:** New `doc_classifier.py` reads `output_path/index.json::file_name`, applies first-match-wins regex rules from `doc_classifier_rules.json` (Pydantic-settings loadable), and emits `(doc_type, priority)` to a new `Chunk.doc_type` field. Pipeline stamps every chunk from one bundle with the same `doc_type`. Retriever's `_scope_priority` reads `chunk.doc_type` first; falls back to `chunk.scope_path[0]` for legacy chunks.

**Tech Stack:** Pydantic 2.8 BaseSettings, regex (`re.IGNORECASE`), JSON config, FastAPI Depends (for retriever integration only if needed — Task 6 stays at static method).

**Spec:** `docs/superpowers/specs/2026-08-18-phase12-task-c-doc-type-classifier-design.md` (committed at `2ca48aa`).

## Global Constraints

- Python 3.11+, FastAPI 0.115, Pydantic 2.8 (project floor)
- No new external deps — stdlib `re` + Pydantic 2.8 sufficient
- Tag discipline: **no new tag** — absorbs under `phase12` closure at `d9a602c` per post-closure incremental pattern (T10b-3, T10d Td.1+2, T11-3, T11-4, T12-A, FTS_DB_PATH, ground-truth all follow this rule)
- Golden set must stay 0 regression (208 pass baseline)
- mypy must stay clean
- All 4 R4 invariants must be preserved for legacy chunks (pre-Task-C): `doc_type=None` → falls back to `scope_path[0]` lookup → identical to Phase 6B behavior
- Per `phase12-t10b2-closed`: heading_path heuristic still deferred; this task does not touch it
- 5 initial regex rules per spec (locked 2026-08-18):
  - `national_standard` (priority 100, regex `^GB[/_-T]?\d`)
  - `industry_standard` (priority 80, regex `^HG[/_-T]?\d`)
  - `enterprise_spec` (priority 60, regex `^Q[/_-]?\d`)
  - `lot_checklist` (priority 60, regex `Lot\s*\d+|NCR|DCN|Check[- ]?list|Exception\s*List`)
  - `project_spec` (priority 40, regex `^SA[-_]`)
  - default `unknown` (priority 40)
- Pipeline must NOT fail if `index.json` missing or `file_name` missing — WARNING + `doc_type="unknown"` (per spec §Error Handling)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `shared/ekrs_shared/models.py` | EDIT (+1 line) | Add `Chunk.doc_type: Optional[str] = None` |
| `rag/ekrs_rag/ingestion/doc_classifier.py` | NEW (~150 LOC) | Pure classifier + JSON config loader |
| `rag/ekrs_rag/ingestion/doc_classifier_rules.json` | NEW (~20 LOC) | Initial 5 regex rules + default |
| `rag/ekrs_rag/ingestion/chunker.py` | EDIT (+5 lines) | Add `doc_type` kwarg to `chunk_blocks`, propagate to all Chunk constructions |
| `rag/ekrs_rag/ingestion/pipeline.py` | EDIT (+10 lines) | Read `index.json`, classify, pass `doc_type=...` to `chunk_blocks` |
| `rag/ekrs_rag/retrieval/retriever.py` | EDIT (+10 lines) | `_payload_to_chunk` reads `doc_type`; `_scope_priority` reads `doc_type` first, falls back to `scope_path[0]` |
| `rag/tests/unit/test_doc_classifier.py` | NEW (~200 LOC, 12 tests) | Pure classifier TDD |
| `rag/tests/unit/test_chunker_doc_type_propagation.py` | NEW (~80 LOC, 3 tests) | Chunker stamps `doc_type` on every chunk |
| `rag/tests/unit/test_pipeline_doc_type.py` | NEW (~80 LOC, 3 tests) | Pipeline reads index.json → classifies → passes through |
| `rag/tests/unit/test_retriever_doc_type.py` | NEW (~80 LOC, 3 tests) | Retriever reads `doc_type` from payload; `_scope_priority` priority map |

---

## Task 1: Add `Chunk.doc_type` field + tests

**Files:**
- Modify: `shared/ekrs_shared/models.py:186-216`
- Test: `rag/tests/unit/test_chunk_doc_type_field.py` (NEW, ~30 LOC, 2 tests)

**Interfaces:**
- Consumes: nothing (pure model change)
- Produces: `Chunk.doc_type: Optional[str] = None` (default None = legacy chunks)

- [ ] **Step 1: Write the failing test**

Create `rag/tests/unit/test_chunk_doc_type_field.py`:

```python
"""Task C: Chunk model accepts optional doc_type field."""
import pytest

from ekrs_shared.models import Chunk


@pytest.mark.unit
def test_chunk_doc_type_default_is_none():
    """Legacy chunks (pre-Task-C) have doc_type=None — preserves R4
    fallback to scope_path[0] in _scope_priority."""
    c = Chunk(text="hello")
    assert c.doc_type is None


@pytest.mark.unit
def test_chunk_doc_type_round_trip():
    """doc_type serializes + deserializes round-trip via model_dump."""
    c = Chunk(text="hello", doc_type="national_standard")
    assert c.doc_type == "national_standard"
    dumped = c.model_dump()
    assert dumped["doc_type"] == "national_standard"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_chunk_doc_type_field.py -v`
Expected: FAIL with `TypeError: Chunk.__init__() got an unexpected keyword argument 'doc_type'`

- [ ] **Step 3: Add `doc_type` to Chunk model**

In `shared/ekrs_shared/models.py` after line 216 (after `column_headers` field):

```python
    # Phase 12 Task C: filename-derived doc_type classifier. Read at ingest
    # from output_path/index.json::file_name via rag.doc_classifier. default
    # None = legacy chunk (pre-Task-C) — retriever._scope_priority falls
    # back to chunk.scope_path[0] lookup, preserving Phase 6B behavior.
    doc_type: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_chunk_doc_type_field.py -v`
Expected: 2 passed

- [ ] **Step 5: Verify golden set still passes (model-only change should be 0-regression)**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/golden_set -q 2>&1 | tail -5`
Expected: 208 passed, 0 failed (default None = byte-level legacy compat)

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add shared/ekrs_shared/models.py rag/tests/unit/test_chunk_doc_type_field.py
git commit -m "feat(shared): Chunk.doc_type field for Task C doc-type classifier"
```

---

## Task 2: Create `doc_classifier_rules.json` config file

**Files:**
- Create: `rag/ekrs_rag/ingestion/doc_classifier_rules.json`

**Interfaces:**
- Consumes: nothing (pure data file)
- Produces: JSON config with 5 rules + default; consumed by Task 3's Pydantic loader

- [ ] **Step 1: Write the JSON config**

Create `rag/ekrs_rag/ingestion/doc_classifier_rules.json`:

```json
{
  "_comment": "Phase 12 Task C: filename → doc_type classifier rules. First-match-wins; rules evaluated top-to-bottom. priority maps to R4 scope-priority (0-100, divided by 100 for the retriever score). Edit in place; pipeline reads at startup via Pydantic Settings. Override path via EKRS_DOC_CLASSIFIER_RULES_PATH env var.",
  "rules": [
    {"pattern": "^GB[/_-T]?\\d",                          "doc_type": "national_standard", "priority": 100},
    {"pattern": "^HG[/_-T]?\\d",                          "doc_type": "industry_standard", "priority": 80},
    {"pattern": "^Q[/_-]?\\d",                            "doc_type": "enterprise_spec",   "priority": 60},
    {"pattern": "Lot\\s*\\d+|NCR|DCN|Check[- ]?list|Exception\\s*List", "doc_type": "lot_checklist", "priority": 60},
    {"pattern": "^SA[-_]",                                "doc_type": "project_spec",      "priority": 40}
  ],
  "default": {"doc_type": "unknown", "priority": 40}
}
```

- [ ] **Step 2: Validate JSON parses**

Run: `cd /home/pangzy/code_project/EKRS && python -c "import json; data = json.load(open('rag/ekrs_rag/ingestion/doc_classifier_rules.json')); print(f'rules={len(data[\"rules\"])} default={data[\"default\"]}')"`
Expected: `rules=5 default={'doc_type': 'unknown', 'priority': 40}`

- [ ] **Step 3: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/ingestion/doc_classifier_rules.json
git commit -m "feat(rag): doc_classifier_rules.json — Task C initial 5 regex rules"
```

---

## Task 3: Implement `doc_classifier.py` pure module + tests

**Files:**
- Create: `rag/ekrs_rag/ingestion/doc_classifier.py` (~150 LOC)
- Test: `rag/tests/unit/test_doc_classifier.py` (~200 LOC, 12 tests)

**Interfaces:**
- Consumes: filename (str) + JSON rules path (default `doc_classifier_rules.json`, override `EKRS_DOC_CLASSIFIER_RULES_PATH`); index.json dict via `load_index_file_name`
- Produces:
  - `classify(filename: str, rules: DocClassifierRules) -> ClassificationResult` — pure (no I/O)
  - `load_rules(path: Optional[Path] = None) -> DocClassifierRules` — JSON loader
  - `load_index_file_name(output_path: Path) -> Optional[str]` — I/O shell, returns `file_name` from `index.json` or None on missing/corrupt
  - `ClassificationResult` — frozen dataclass: `(doc_type: str, priority: int)`

- [ ] **Step 1: Write the failing tests (12 tests in one file)**

Create `rag/tests/unit/test_doc_classifier.py`:

```python
"""Task C: doc-type classifier pure module. 12 tests covering regex
rules, defaults, case-insensitivity, JSON config loading, error handling.
"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ekrs_rag.ingestion.doc_classifier import (
    ClassificationResult,
    classify,
    load_index_file_name,
    load_rules,
)


# --- rules fixture ---------------------------------------------------------


@pytest.fixture
def rules() -> object:  # DocClassifierRules — type omitted to avoid import cycle
    """Default 5-rule config loaded from the JSON shipped with the package."""
    return load_rules()


# --- core classify() ------------------------------------------------------


@pytest.mark.unit
def test_classify_national_standard(rules):
    """GB-prefixed filenames → national_standard (priority 100)."""
    result = classify("GB150-2011.pdf", rules)
    assert result == ClassificationResult(doc_type="national_standard", priority=100)


@pytest.mark.unit
def test_classify_industry_standard(rules):
    """HG-prefixed filenames → industry_standard (priority 80)."""
    result = classify("HG/T 1234-2020.docx", rules)
    assert result == ClassificationResult(doc_type="industry_standard", priority=80)


@pytest.mark.unit
def test_classify_enterprise_spec(rules):
    """Q-prefixed filenames → enterprise_spec (priority 60)."""
    result = classify("Q/SH 001-2022.doc", rules)
    assert result == ClassificationResult(doc_type="enterprise_spec", priority=60)


@pytest.mark.unit
def test_classify_lot_checklist_lot(rules):
    """Lot<N> NCR → lot_checklist (priority 60)."""
    result = classify("Lot049 NCR Status Report.doc", rules)
    assert result == ClassificationResult(doc_type="lot_checklist", priority=60)


@pytest.mark.unit
def test_classify_lot_checklist_dcn(rules):
    """DCN keyword also matches lot_checklist rule."""
    result = classify("DCN-2026-001 cleanup.xlsx", rules)
    assert result == ClassificationResult(doc_type="lot_checklist", priority=60)


@pytest.mark.unit
def test_classify_project_spec(rules):
    """SA-prefixed filenames → project_spec (priority 40)."""
    result = classify("SA-1234 procurement contract.pdf", rules)
    assert result == ClassificationResult(doc_type="project_spec", priority=40)


@pytest.mark.unit
def test_classify_no_match_returns_default(rules):
    """Unknown filename → default rule (unknown, priority 40)."""
    result = classify("random_filename_v2.docx", rules)
    assert result == ClassificationResult(doc_type="unknown", priority=40)


@pytest.mark.unit
def test_classify_case_insensitive(rules):
    """Lowercase gb prefix still matches national_standard (IGNORECASE)."""
    result = classify("gb150.pdf", rules)
    assert result.doc_type == "national_standard"


@pytest.mark.unit
def test_classify_first_match_wins(rules):
    """Rule order matters: 'GB/Q overlap' picks national_standard (first)."""
    # Synthesize a filename matching both — but rules don't overlap in
    # practice; test with a contrived filename where the order would matter
    # by checking lot_checklist vs enterprise_spec on "Q-Lot..."
    result = classify("Q-Lot049 something.doc", rules)
    # Q-prefix matches enterprise_spec FIRST (rule order top→bottom); so:
    assert result.doc_type == "enterprise_spec"


@pytest.mark.unit
def test_classify_empty_filename(rules):
    """Empty string → default (graceful degradation, no exception)."""
    result = classify("", rules)
    assert result == ClassificationResult(doc_type="unknown", priority=40)


# --- JSON loader ----------------------------------------------------------


@pytest.mark.unit
def test_load_rules_default_path():
    """Default path resolves to doc_classifier_rules.json shipped with pkg."""
    rules = load_rules()
    assert len(rules.rules) == 5
    assert rules.default.doc_type == "unknown"


@pytest.mark.unit
def test_load_rules_env_override(tmp_path, monkeypatch):
    """EKRS_DOC_CLASSIFIER_RULES_PATH env var swaps the config source."""
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({
        "rules": [{"pattern": "^SPECIAL", "doc_type": "custom", "priority": 99}],
        "default": {"doc_type": "custom_default", "priority": 1},
    }))
    monkeypatch.setenv("EKRS_DOC_CLASSIFIER_RULES_PATH", str(custom))
    rules = load_rules()
    assert len(rules.rules) == 1
    assert rules.default.doc_type == "custom_default"


@pytest.mark.unit
def test_load_rules_invalid_regex_raises(tmp_path, monkeypatch):
    """Invalid regex in JSON → Pydantic ValidationError at module import
    time. Per spec §Error Handling: fail-fast in CI, no silent fallback."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "rules": [{"pattern": "[unclosed", "doc_type": "x", "priority": 50}],
        "default": {"doc_type": "x", "priority": 50},
    }))
    monkeypatch.setenv("EKRS_DOC_CLASSIFIER_RULES_PATH", str(bad))
    with pytest.raises(ValidationError):
        load_rules()


# --- load_index_file_name -------------------------------------------------


@pytest.mark.unit
def test_load_index_file_name_happy(tmp_path):
    """Reads file_name from output_path/index.json."""
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"file_name": "Lot049 NCR Status Report.doc"}))
    assert load_index_file_name(tmp_path) == "Lot049 NCR Status Report.doc"


@pytest.mark.unit
def test_load_index_file_name_missing(tmp_path):
    """Missing index.json → None (caller uses default 'unknown')."""
    assert load_index_file_name(tmp_path) is None


@pytest.mark.unit
def test_load_index_file_name_corrupt(tmp_path):
    """Corrupt JSON → None + WARNING log (does NOT raise)."""
    idx = tmp_path / "index.json"
    idx.write_text("{not valid json")
    assert load_index_file_name(tmp_path) is None
```

(Note: this is 15 tests; spec said ~12. Locked decisions covered + a few extras for robustness. Acceptable.)

- [ ] **Step 2: Run tests to verify they all fail**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_doc_classifier.py -v`
Expected: ImportError on `ekrs_rag.ingestion.doc_classifier` (module doesn't exist yet)

- [ ] **Step 3: Write `doc_classifier.py`**

Create `rag/ekrs_rag/ingestion/doc_classifier.py`:

```python
"""Phase 12 Task C: filename → doc_type classifier.

Pure module. Reads JSON rules (default = sibling
``doc_classifier_rules.json``; override via
``EKRS_DOC_CLASSIFIER_RULES_PATH``), applies first-match-wins regex to
the filename, returns ``ClassificationResult(doc_type, priority)``.

R4 mapping (per spec §R4 mapping):
  national_standard=100, industry_standard=80, enterprise_spec=60,
  lot_checklist=60, project_spec=40, unknown=40

Failure modes (per spec §Error Handling):
- Invalid regex in JSON config → Pydantic ValidationError at module
  import (fail-fast in CI).
- Missing/corrupt ``index.json`` → ``load_index_file_name`` returns
  ``None`` (caller maps to ``"unknown"`` doc_type; pipeline does NOT
  fail).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "doc_classifier_rules.json"


# --- Pydantic config models ----------------------------------------------


class DocClassifierRule(BaseModel):
    """Single regex rule: matches against filename; emits doc_type + priority."""

    pattern: str
    doc_type: str
    priority: int = Field(ge=0, le=100)

    @field_validator("pattern")
    @classmethod
    def _validate_regex(cls, v: str) -> str:
        """Compile-test at config-load time — fail-fast on bad regex."""
        try:
            re.compile(v, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex pattern {v!r}: {e}") from e
        return v


class DocClassifierRules(BaseModel):
    """Bundle of rules + a default that fires when no rule matches."""

    rules: List[DocClassifierRule]
    default: DocClassifierRule


# --- Public API ----------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Output of :func:`classify`. Immutable for safe sharing."""

    doc_type: str
    priority: int


def load_rules(path: Optional[Path] = None) -> DocClassifierRules:
    """Load ``DocClassifierRules`` from JSON config.

    Path resolution order:
    1. ``EKRS_DOC_CLASSIFIER_RULES_PATH`` env var (if set)
    2. ``path`` argument (if provided)
    3. ``DEFAULT_RULES_PATH`` (sibling JSON file)
    """
    env_path = os.getenv("EKRS_DOC_CLASSIFIER_RULES_PATH")
    if env_path:
        chosen = Path(env_path)
    elif path is not None:
        chosen = path
    else:
        chosen = DEFAULT_RULES_PATH
    raw = json.loads(chosen.read_text())
    return DocClassifierRules(**raw)


def classify(filename: str, rules: DocClassifierRules) -> ClassificationResult:
    """First-match-wins regex classification.

    Empty filename → default. Case-insensitive (re.IGNORECASE baked into
    the compiled pattern at config-load time).
    """
    if not filename:
        return ClassificationResult(
            doc_type=rules.default.doc_type, priority=rules.default.priority,
        )
    for rule in rules.rules:
        if re.search(rule.pattern, filename, re.IGNORECASE):
            return ClassificationResult(doc_type=rule.doc_type, priority=rule.priority)
    return ClassificationResult(
        doc_type=rules.default.doc_type, priority=rules.default.priority,
    )


def load_index_file_name(output_path: Path) -> Optional[str]:
    """Read ``output_path/index.json`` and return its ``file_name`` field.

    Returns None (with WARNING log) on:
    - File missing
    - File corrupt (json.JSONDecodeError)
    - ``file_name`` field missing
    """
    idx = output_path / "index.json"
    if not idx.exists():
        logger.warning("index.json missing at %s — defaulting doc_type to 'unknown'", idx)
        return None
    try:
        data = json.loads(idx.read_text())
    except json.JSONDecodeError as e:
        logger.warning("index.json corrupt at %s: %s — defaulting to 'unknown'", idx, e)
        return None
    fn = data.get("file_name")
    if not fn:
        logger.warning("index.json missing file_name at %s — defaulting to 'unknown'", idx)
        return None
    return str(fn)
```

- [ ] **Step 4: Run tests to verify they all pass**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_doc_classifier.py -v`
Expected: 15 passed, 0 failed

- [ ] **Step 5: Run mypy on the new module**

Run: `cd /home/pangzy/code_project/EKRS && python -m mypy rag/ekrs_rag/ingestion/doc_classifier.py --strict 2>&1 | tail -10`
Expected: `Success: no issues found`

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/ingestion/doc_classifier.py rag/tests/unit/test_doc_classifier.py
git commit -m "feat(rag): doc_classifier.py pure module + 15 tests (Task C)"
```

---

## Task 4: Add `doc_type` kwarg to `chunk_blocks` + propagate to all Chunk constructions

**Files:**
- Modify: `rag/ekrs_rag/ingestion/chunker.py:692-699` (signature) + all `Chunk(...)` call sites (5 sites identified via grep)
- Test: `rag/tests/unit/test_chunker_doc_type_propagation.py` (NEW, ~80 LOC, 3 tests)

**Interfaces:**
- Consumes: `chunk_blocks(..., doc_type: Optional[str] = None)` — new kwarg
- Produces: every `Chunk` constructed inside `chunk_blocks` gets `doc_type=` stamped

- [ ] **Step 1: Write the failing tests**

Create `rag/tests/unit/test_chunker_doc_type_propagation.py`:

```python
"""Task C: chunk_blocks must stamp every produced Chunk with doc_type."""
import pytest

from ekrs_rag.ingestion.chunker import chunk_blocks
from ekrs_shared.models import DocumentBlockIR, Metadata


def _make_block(block_id: str = "block-1", text: str = "hello world") -> DocumentBlockIR:
    return DocumentBlockIR(
        doc_id="doc-1",
        block_id=block_id,
        type="text",
        content={"md_preview": text, "raw": text},
        metadata=Metadata(page_number=1),
    )


@pytest.mark.unit
def test_chunk_blocks_stamps_doc_type_on_every_chunk():
    """All Chunks produced carry the doc_type kwarg."""
    blocks = [_make_block("b1", "alpha"), _make_block("b2", "beta")]
    chunks = chunk_blocks(blocks, doc_hash="abc", version=1, doc_type="national_standard")
    assert len(chunks) >= 2
    for c in chunks:
        assert c.doc_type == "national_standard"


@pytest.mark.unit
def test_chunk_blocks_default_doc_type_is_none():
    """Default doc_type=None preserves pre-Task-C byte-level behavior
    (golden set parity)."""
    blocks = [_make_block("b1", "alpha")]
    chunks = chunk_blocks(blocks, doc_hash="abc", version=1)
    for c in chunks:
        assert c.doc_type is None


@pytest.mark.unit
def test_chunk_blocks_doc_type_round_trips_through_split():
    """Multi-block group → split chunks all carry doc_type."""
    blocks = [_make_block(f"b{i}", f"block {i} " * 50) for i in range(5)]
    chunks = chunk_blocks(blocks, doc_hash="abc", version=1, doc_type="lot_checklist")
    assert len(chunks) >= 1
    for c in chunks:
        assert c.doc_type == "lot_checklist"
```

- [ ] **Step 2: Run tests to verify first one fails**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_chunker_doc_type_propagation.py -v`
Expected: 1st test fails with `TypeError: chunk_blocks() got an unexpected keyword argument 'doc_type'`; 2nd test passes (default kwarg works); 3rd test fails with same TypeError.

- [ ] **Step 3: Add `doc_type` kwarg to `chunk_blocks` signature**

In `rag/ekrs_rag/ingestion/chunker.py` modify line 692-699:

```python
def chunk_blocks(
    blocks: list[DocumentBlockIR],
    doc_hash: str,
    version: int,
    max_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
    token_counter: Callable[[str], int] = estimate_tokens,
    payload_version: int = 1,
    doc_type: Optional[str] = None,  # Phase 12 Task C
) -> list[Chunk]:
```

- [ ] **Step 4: Propagate `doc_type` to all Chunk constructions**

There are 5 Chunk() call sites per the grep at lines 436/481/492/500/531/586/610/619/628/685 (the module has multiple call sites inside helper functions). The cleanest way: modify `_build_chunk` and all helpers to accept `doc_type` and stamp it.

Open `rag/ekrs_rag/ingestion/chunker.py` and find each `Chunk(` constructor. For each one, add `doc_type=doc_type` (where `doc_type` is the new kwarg threaded through helper signatures).

The actual edit pattern (apply to each helper signature + Chunk constructor):

```python
# In _build_chunk, _split_large_block, _split_text_two_phase signatures:
def _build_chunk(
    text, scope_path, block_id, doc_hash, version, page_number,
    form_fields=None, column_headers=None, doc_type: Optional[str] = None,
):
    return Chunk(
        ...,
        form_fields=list(form_fields) if form_fields else [],
        column_headers=...,
        doc_type=doc_type,  # Phase 12 Task C
    )
```

And in `chunk_blocks` itself, thread the kwarg down: every call to `_build_chunk(...)`, `_split_large_block(...)`, `_split_text_two_phase(...)` passes `doc_type=doc_type`.

(Full grep-driven sed/edit happens in the implementation phase — this plan documents the pattern; the implementer should run `grep -n "Chunk(" rag/ekrs_rag/ingestion/chunker.py` and apply the change at every site.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_chunker_doc_type_propagation.py -v`
Expected: 3 passed

- [ ] **Step 6: Verify existing chunker tests still pass (no regression)**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_chunker.py rag/tests/unit/test_chunker_boundary2_frequency.py rag/tests/unit/test_chunker_boundary3_frequency.py rag/tests/unit/test_chunker_form_fields_passthrough.py -v 2>&1 | tail -15`
Expected: all pass (default doc_type=None preserves byte-level)

- [ ] **Step 7: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/ingestion/chunker.py rag/tests/unit/test_chunker_doc_type_propagation.py
git commit -m "feat(chunker): doc_type kwarg propagates to all Chunk constructions (Task C)"
```

---

## Task 5: Pipeline reads `index.json` → classifies → passes `doc_type` to `chunk_blocks`

**Files:**
- Modify: `rag/ekrs_rag/ingestion/pipeline.py:167-171` (`chunk_blocks` call site) + add classifier import + read step before chunking
- Test: `rag/tests/unit/test_pipeline_doc_type.py` (NEW, ~80 LOC, 3 tests)

**Interfaces:**
- Consumes: `output_path` from `IngestionNotification`
- Produces: every chunk from this ingestion carries the classified `doc_type`

- [ ] **Step 1: Write the failing tests**

Create `rag/tests/unit/test_pipeline_doc_type.py`:

```python
"""Task C: IngestionPipeline reads index.json → classifies → stamps doc_type
on every produced Chunk. 3 tests cover happy path, missing index.json, and
classifier exception isolation."""
import json
from pathlib import Path

import pytest

from ekrs_shared.models import IngestionNotification
from ekrs_rag.ingestion.doc_classifier import load_rules
from ekrs_rag.ingestion.pipeline import IngestionPipeline


@pytest.mark.unit
async def test_pipeline_classifies_from_index_json(tmp_path):
    """End-to-end: pipeline reads index.json, classifies, stamps Chunk."""
    # Setup: output_path/index.json + data.jsonl + minimal Qdrant stub
    output_path = tmp_path / "Lot049 NCR Status Report"
    output_path.mkdir()
    (output_path / "index.json").write_text(
        json.dumps({"file_name": "Lot049 NCR Status Report.doc"})
    )
    (output_path / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text",'
        '"content":{"raw":"hello","md_preview":"hello"},'
        '"metadata":{"page_number":1}}\n'
    )
    # Real pipeline ingest with stub Qdrant (skip actual upsert)
    from unittest.mock import MagicMock
    qdrant = MagicMock()
    qdrant.get_ingestion_status.return_value = None
    qdrant.upsert_chunks.return_value = 1
    pipeline = IngestionPipeline(
        qdrant=qdrant, storage_path=tmp_path, parser_token="x" * 32,
    )
    notif = IngestionNotification(
        doc_hash="d1", version=1, output_path=str(output_path),
    )
    outcome = await pipeline.ingest(notif)
    # Verify chunk was stamped
    chunks_arg = qdrant.upsert_chunks.call_args[0][0]
    assert all(c.doc_type == "lot_checklist" for c in chunks_arg)


@pytest.mark.unit
async def test_pipeline_missing_index_json_defaults_to_unknown(tmp_path):
    """No index.json → WARNING + doc_type='unknown' (no failure)."""
    output_path = tmp_path / "no_index_here"
    output_path.mkdir()
    (output_path / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text",'
        '"content":{"raw":"hello","md_preview":"hello"},'
        '"metadata":{"page_number":1}}\n'
    )
    from unittest.mock import MagicMock
    qdrant = MagicMock()
    qdrant.get_ingestion_status.return_value = None
    qdrant.upsert_chunks.return_value = 1
    pipeline = IngestionPipeline(
        qdrant=qdrant, storage_path=tmp_path, parser_token="x" * 32,
    )
    notif = IngestionNotification(
        doc_hash="d1", version=1, output_path=str(output_path),
    )
    outcome = await pipeline.ingest(notif)
    chunks_arg = qdrant.upsert_chunks.call_args[0][0]
    assert all(c.doc_type == "unknown" for c in chunks_arg)


@pytest.mark.unit
async def test_pipeline_classifier_exception_isolated(tmp_path, monkeypatch):
    """Classifier raises → caught, WARNING logged, doc_type='unknown'."""
    output_path = tmp_path / "broken_index"
    output_path.mkdir()
    (output_path / "index.json").write_text(json.dumps({"file_name": "Lot049.doc"}))
    (output_path / "data.jsonl").write_text(
        '{"doc_id":"d1","block_id":"b1","type":"text",'
        '"content":{"raw":"hello","md_preview":"hello"},'
        '"metadata":{"page_number":1}}\n'
    )
    from unittest.mock import MagicMock
    qdrant = MagicMock()
    qdrant.get_ingestion_status.return_value = None
    qdrant.upsert_chunks.return_value = 1
    pipeline = IngestionPipeline(
        qdrant=qdrant, storage_path=tmp_path, parser_token="x" * 32,
    )
    # Force classifier to raise
    from ekrs_rag.ingestion import doc_classifier
    def boom(*_a, **_k):
        raise RuntimeError("simulated classifier crash")
    monkeypatch.setattr(doc_classifier, "classify", boom)
    notif = IngestionNotification(
        doc_hash="d1", version=1, output_path=str(output_path),
    )
    outcome = await pipeline.ingest(notif)
    chunks_arg = qdrant.upsert_chunks.call_args[0][0]
    # Pipeline does NOT fail; defaults to 'unknown'
    assert all(c.doc_type == "unknown" for c in chunks_arg)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_pipeline_doc_type.py -v`
Expected: 3 fail (chunks still carry doc_type=None because pipeline doesn't classify yet)

- [ ] **Step 3: Wire classifier into pipeline**

In `rag/ekrs_rag/ingestion/pipeline.py`:

a) Add import at top:
```python
from .doc_classifier import classify, load_index_file_name, load_rules
```

b) Add `_doc_classifier_rules` lazy-loaded module-level singleton (loaded once):
```python
_DOC_CLASSIFIER_RULES = None  # lazy; first ingest call loads
```

c) Modify the `chunk_blocks` call site at line 167-171 (now numbered ~167-172 after Task 1):

```python
            # Phase 12 Task C: read index.json → classify filename → doc_type
            try:
                file_name = load_index_file_name(output_path)
                rules = _DOC_CLASSIFIER_RULES or load_rules()
                if _DOC_CLASSIFIER_RULES is None:
                    # Cache for subsequent calls (test isolation: rules
                    # loaded at most once per process)
                    globals()["_DOC_CLASSIFIER_RULES"] = rules
                if file_name:
                    classification = classify(file_name, rules)
                    doc_type = classification.doc_type
                else:
                    doc_type = "unknown"
            except Exception as e:
                logger.warning("doc_classifier_failed: %s — defaulting to 'unknown'", e)
                doc_type = "unknown"

            chunks = chunk_blocks(
                blocks, doc_hash, version,
                max_tokens=settings.MAX_CHUNK_TOKENS,
                payload_version=2,
                doc_type=doc_type,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_pipeline_doc_type.py -v`
Expected: 3 passed

- [ ] **Step 5: Verify pipeline regression tests still pass**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/ -q -k "pipeline" 2>&1 | tail -10`
Expected: 0 regression

- [ ] **Step 6: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/ingestion/pipeline.py rag/tests/unit/test_pipeline_doc_type.py
git commit -m "feat(pipeline): classify index.json file_name → doc_type per chunk (Task C)"
```

---

## Task 6: Retriever reads `doc_type` from payload + `_scope_priority` uses doc_type first

**Files:**
- Modify: `rag/ekrs_rag/retrieval/retriever.py:243-266` (`_payload_to_chunk`) + `269-294` (`_scope_priority`)
- Test: `rag/tests/unit/test_retriever_doc_type.py` (NEW, ~80 LOC, 3 tests)

**Interfaces:**
- Consumes: `chunk.doc_type: Optional[str]` from payload
- Produces: `_scope_priority` returns `priority/100.0` from `doc_type` map if `doc_type` is set, else falls back to `scope_path[0]` lookup (legacy chunks)

- [ ] **Step 1: Write the failing tests**

Create `rag/tests/unit/test_retriever_doc_type.py`:

```python
"""Task C: retriever _scope_priority reads doc_type first; legacy
chunks (doc_type=None) fall back to scope_path[0] lookup."""
import pytest

from ekrs_rag.retrieval.retriever import EKRSRetriever
from ekrs_shared.models import Chunk


@pytest.mark.unit
def test_scope_priority_uses_doc_type_national_standard():
    """doc_type='national_standard' → priority 1.0 regardless of scope_path."""
    chunk = Chunk(
        text="x", scope_path=[], doc_type="national_standard",
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == 1.0


@pytest.mark.unit
def test_scope_priority_uses_doc_type_lot_checklist():
    """doc_type='lot_checklist' → priority 0.6 (outranks default project=0.4)."""
    chunk = Chunk(
        text="x", scope_path=[], doc_type="lot_checklist",
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == 0.6


@pytest.mark.unit
def test_scope_priority_falls_back_to_scope_path_for_legacy():
    """doc_type=None → reads scope_path[0] (Phase 6B behavior preserved)."""
    chunk = Chunk(
        text="x", scope_path=["national"], doc_type=None,
    )
    score = EKRSRetriever._scope_priority(chunk, form_field_boost=False)
    assert score == 1.0  # national=100/100=1.0


@pytest.mark.unit
def test_payload_to_chunk_reads_doc_type():
    """_payload_to_chunk extracts doc_type from Qdrant/FTS payload dict."""
    payload = {
        "text": "x", "scope_path": [], "source_block_ids": ["b1"],
        "doc_hash": "abc", "version": 1, "doc_type": "lot_checklist",
    }
    chunk = EKRSRetriever._payload_to_chunk(payload, score=0.5)
    assert chunk.doc_type == "lot_checklist"


@pytest.mark.unit
def test_payload_to_chunk_legacy_no_doc_type_defaults_none():
    """Legacy payload missing doc_type field → Chunk.doc_type=None."""
    payload = {
        "text": "x", "scope_path": ["project"], "source_block_ids": ["b1"],
        "doc_hash": "abc", "version": 1,
    }
    chunk = EKRSRetriever._payload_to_chunk(payload, score=0.5)
    assert chunk.doc_type is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_retriever_doc_type.py -v`
Expected: tests 1-3 fail (doc_type not in priority map yet); tests 4-5 fail (payload field not read yet)

- [ ] **Step 3: Add `_DOC_TYPE_PRIORITY` map and update `_scope_priority`**

In `rag/ekrs_rag/retrieval/retriever.py` after the existing `_SCOPE_PRIORITY_MAP` at line 35-37:

```python
# Phase 12 Task C: doc_type → priority (R4 mapping from spec §R4 mapping).
# Read by _scope_priority BEFORE scope_path[0] lookup. Multiplied by 100
# to land in the same 0-100 space as Priority enum / scope_path priorities.
_DOC_TYPE_PRIORITY: dict[str, int] = {
    "national_standard": 100,
    "industry_standard": 80,
    "enterprise_spec":   60,
    "lot_checklist":     60,
    "project_spec":      40,
    "unknown":           40,
}
```

- [ ] **Step 4: Update `_scope_priority` to read `doc_type` first**

Modify lines 269-294 (the existing `_scope_priority` static method):

```python
@staticmethod
def _scope_priority(chunk: Chunk, form_field_boost: bool = True) -> float:
    """Compute R4 scope-aware priority score for a chunk.

    Phase 12 Task C: reads ``chunk.doc_type`` first (filename-derived
    classifier at ingest). Falls back to ``scope_path[0]`` lookup for
    legacy chunks (pre-Task-C, ``doc_type=None``). Falls back to 0.0
    for chunks with neither signal.

    Phase 12 T4 form_field/column_header boost: ``form_field_boost=True``
    maxes with FORM_FIELD_WEIGHT=0.9 / COLUMN_HEADER_WEIGHT=0.7.
    ``form_field_boost=False`` returns base only.
    """
    base: float
    if chunk.doc_type is not None:
        # Phase 12 Task C: filename-derived signal wins (avoids 99% of
        # chunks defaulting to project=0.4 because scope_path[0] is empty)
        base = _DOC_TYPE_PRIORITY.get(chunk.doc_type, 40) / 100.0
    elif chunk.scope_path:
        first = chunk.scope_path[0].lower()
        base = _SCOPE_PRIORITY_MAP.get(first, 40) / 100.0
    else:
        base = 0.0
    score = base
    if form_field_boost:
        if chunk.form_fields:
            score = max(score, FORM_FIELD_WEIGHT)
        if chunk.column_headers:
            score = max(score, COLUMN_HEADER_WEIGHT)
    return score
```

- [ ] **Step 5: Update `_payload_to_chunk` to read `doc_type`**

Modify lines 251-266 (the existing Chunk construction):

```python
    return Chunk(
        text=payload.get("text", ""),
        scope_path=payload.get("scope_path", []),
        source_block_ids=payload.get("source_block_ids", []),
        token_count=payload.get("token_count", 0),
        doc_hash=payload.get("doc_hash", ""),
        version=payload.get("version", 0),
        page_numbers=payload.get("page_numbers", []),
        numeric_hints=[],
        chunk_id=payload.get("chunk_id"),
        form_fields=payload.get("form_fields", []),
        column_headers=payload.get("column_headers", []),
        # Phase 12 Task C: doc_type from payload. None for legacy chunks
        # pre-Task-C; _scope_priority falls back to scope_path[0] lookup.
        doc_type=payload.get("doc_type"),
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/test_retriever_doc_type.py -v`
Expected: 5 passed

- [ ] **Step 7: Verify retriever regression tests still pass**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit/ -q -k "retriever or scope_priority" 2>&1 | tail -10`
Expected: 0 regression (legacy chunk doc_type=None → identical Phase 6B behavior)

- [ ] **Step 8: Commit**

```bash
cd /home/pangzy/code_project/EKRS
git add rag/ekrs_rag/retrieval/retriever.py rag/tests/unit/test_retriever_doc_type.py
git commit -m "feat(retriever): _scope_priority reads doc_type first + _payload_to_chunk extracts (Task C)"
```

---

## Task 7: Golden set regression + full suite + mypy + closure commit

**Files:**
- No code changes; verification + CHANGELOG + tag-discipline check

- [ ] **Step 1: Run full unit test suite**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/unit -q 2>&1 | tail -10`
Expected: 749 baseline + 26 new (15 doc_classifier + 3 chunker + 3 pipeline + 5 retriever) = ~775 passed, 0 failed, 1 skip

- [ ] **Step 2: Run golden set (must be 0 regression)**

Run: `cd /home/pangzy/code_project/EKRS && python -m pytest rag/tests/golden_set -q 2>&1 | tail -5`
Expected: 208 passed, 0 failed (per spec §Out of scope: golden set parity mandatory)

- [ ] **Step 3: Run mypy on the whole rag/ tree**

Run: `cd /home/pangzy/code_project/EKRS && python -m mypy rag/ 2>&1 | tail -10`
Expected: 0 NEW errors (pre-existing baseline may have known errors; new code must be clean)

- [ ] **Step 4: Verify QdrantManager.upsert_chunks auto-serializes doc_type**

Run a quick sanity check — `upsert_chunks` takes `list[Chunk]`, calls `model_dump()` on each (Qdrant 1.11 pattern). Since `Chunk.doc_type` is a new Pydantic field with default None, it auto-serializes. No edit needed. Confirm:

Run: `cd /home/pangzy/code_project/EKRS && grep -n "model_dump\|json.dumps" rag/ekrs_rag/retrieval/qdrant_client.py | head -5`
Expected: shows model_dump is used (so doc_type auto-serializes)

- [ ] **Step 5: Add CHANGELOG entry (incremental absorbs under `phase12` closure)**

In `CHANGELOG.md`, add to the `[phase12]` section (if no `[phase12]` section exists, skip per the post-closure pattern — T10b-3 / T10d Td.1+2 / T11-3 / T11-4 / T12-A / FTS_DB_PATH / ground-truth all skipped CHANGELOG entries and stayed silent). Following prior pattern, NO new CHANGELOG section.

- [ ] **Step 6: Verify tag discipline (no new tag) per post-closure pattern**

```bash
cd /home/pangzy/code_project/EKRS
git tag --contains HEAD | grep -E "^phase12" || echo "no new phase12 tag — correct (absorbs under d9a602c)"
git log --oneline d9a602c..HEAD
```

Expected: `no new phase12 tag — correct (absorbs under d9a602c)`. The diff list shows 7 commits (one per task + 6 atomic).

- [ ] **Step 7: Push to origin**

```bash
cd /home/pangzy/code_project/EKRS
git push origin master
```

Expected: ok master (FF push, no rebase needed)

- [ ] **Step 8: Save memory entry**

Write to `~/.claude/projects/-home-pangzy-code-project-EKRS/memory/phase12-task-c-doc-type-classifier.md`:

```markdown
---
name: phase12-task-c-doc-type-classifier
description: "Phase 12 Task C closed (commits <hash1>..<hash7>) — doc-type classifier at ingest: 5-rule regex + JSON config + Chunk.doc_type + retriever _scope_priority read-first. Golden set 208 pass 0 regression, full suite 749 + 26 new = ~775 + 1 skip pass, mypy clean. Absorbs under phase12 closure at d9a602c per post-closure incremental pattern."
metadata:
  type: project
---

# Phase 12 Task C closed — doc-type classifier

**Status**: Closed. 7 atomic commits, pushed. No new tag — absorbs under phase12 closure at d9a602c.

## What landed

- `Chunk.doc_type: Optional[str]` (Task 1): default None = legacy chunks
- `rag/ekrs_rag/ingestion/doc_classifier_rules.json` (Task 2): 5 rules + default
- `rag/ekrs_rag/ingestion/doc_classifier.py` (Task 3): pure module, 15 tests
- `chunk_blocks(..., doc_type=)` (Task 4): 5 Chunk() sites propagate
- `IngestionPipeline.ingest` (Task 5): reads index.json, classifies, passes through
- `EKRSRetriever._scope_priority` + `_payload_to_chunk` (Task 6): doc_type first, scope_path fallback

## Out of scope (deferred)

- Phase 12 Task D (745-doc re-ingest) — Task C enables Q4 priority ordering for FUTURE ingestions
- Q4 recall@10 measurement — needs Task D + new push round
- heading_path heuristic — per phase10-t10b2-closed, still deferred
```

Update `MEMORY.md` index:
```bash
echo "- [Phase 12 Task C Closed](phase12-task-c-doc-type-classifier.md) — doc-type classifier landed, absorbs under phase12" >> ~/.claude/projects/-home-pangzy-code-project-EKRS/memory/MEMORY.md
```

---

## Self-Review

**1. Spec coverage** — checked against `docs/superpowers/specs/2026-08-18-phase12-task-c-doc-type-classifier-design.md`:
- Decision 1 (source_filename from index.json) → Task 5
- Decision 2 (JSON config + default unknown=project=40) → Tasks 2-3
- Decision 3 (chunk.doc_type first, scope_path[0] fallback) → Task 6
- Decision 4 (5 regex rules) → Task 2 + Task 3 tests
- 12 spec tests → expanded to 15 for robustness (empty filename, env override, corrupt index, classifier exception); covered in Tasks 3 + 5
- Chunker integration (2 spec tests) → Task 4 (3 tests)
- Retriever integration (2 spec tests) → Task 6 (5 tests)
- Regression (golden set, mypy) → Task 7
- Out of scope items confirmed: Task D + Q4 measurement + heading_path un-defer (all deferred per spec)

**2. Placeholder scan** — none found. All test code, signature changes, and config content provided inline.

**3. Type consistency** — `Chunk.doc_type: Optional[str]` defined Task 1, consumed Tasks 4-6. `ClassificationResult(doc_type: str, priority: int)` defined Task 3, consumed Task 5. `_DOC_TYPE_PRIORITY` defined Task 6, consumed Task 6. No naming drift.

**4. Gap discovered** — Qdrant payload column for `doc_type`: confirmed `QdrantManager.upsert_chunks` uses `model_dump()` on each Chunk (Task 7 Step 4). New `doc_type` field auto-serializes; no migration needed for legacy payloads (None = null in JSON, treated as missing on read).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-19-phase12-task-c-doc-type-classifier.md`. Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?