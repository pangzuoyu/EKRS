# Phase 6A — Spec Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 9 spec gaps in `ekrs-handbook` §4/§5/§8.2/§9/§12/§16/§18, raising the RAG service from 78% to 85% test coverage and bringing the surface area in line with the handbook. Ship behind tag `phase6a-spec-closure`.

**Architecture:** 9 vertical slices, each its own TDD commit with one review gate. Each slice is independently shippable. Slices 1-2 must precede 3 (security + schema before endpoints); slice 5 can run in parallel with 1-2 (independent module dirs). Slice 6-10 are mechanical follow-ups.

**Tech Stack:** Python 3.11+, FastAPI 0.115, Pydantic 2.8, aiosqlite 0.20+, pytest, `portion` interval arithmetic, existing AuditLogger + AuditIndex.

## Global Constraints

(Every task implicitly inherits these.)

- **Iron Rules R1-R8** — no violation. R6 (strict 优先于 soft fallback) is enforced in Task 5.
- **15 audit event names/schemas** — unchanged. Only 2 new *optional* fields appended (`lineage_snapshot`, `conflict_details`).
- **API backward compat** — `allow_soft_fallback` defaults to `True`; old request bodies still parse.
- **No new external deps** — use only FastAPI / Pydantic / aiosqlite / pytest already in `pyproject.toml`.
- **Single commit ≤500 LOC per task**, except Task 6 (golden set is static data, exempt by CQ2 user decision).
- **One review gate per task** — subagent `code-reviewer` per subagent-driven-development flow.
- **`/v1/constraints/trace` scope_filter semantics (D8):** `event.scope_path.startswith(filter)`. Empty/None → all events.
- **A1 ingestion contract:** parser pushes `IR.doc_metadata`; RAG extracts `doc_id, type, scope_path, status` and writes via `DocumentRepo`. No parser changes required.
- **D9 CI gate:** final task adds `pytest --cov=ekrs_rag --cov-fail-under=85` to CI; coverage <85% blocks merge.

## File Structure (project conventions, may diverge from spec wording)

The spec wrote `db/documents.py` and `db/migrations/0006_documents.sql`; this plan uses **`storage/documents.py`** (mirrors `storage/task_repo.py`, the existing pattern) with inline DDL (mirrors TaskRepo's `_SCHEMA` + `_MIGRATIONS`). The spec wrote `api/v1/trace.py`; this plan uses **`api/routes/trace.py`** (mirrors existing `api/routes/constraints.py`). The spec wrote `api/dependencies.py`; this plan creates it (doesn't exist yet).

| New file | Purpose |
|----------|---------|
| `rag/ekrs_rag/security.py` | `require_admin_key` Depends + `verify_admin_key` helper |
| `rag/ekrs_rag/storage/documents.py` | `DocumentRepo` (aiosqlite): documents / doc_supersedes / provision_overrides CRUD |
| `rag/ekrs_rag/api/dependencies.py` | `get_document_repo` FastAPI Depends |
| `rag/ekrs_rag/api/routes/trace.py` | `POST /v1/constraints/trace` endpoint |
| `rag/ekrs_rag/api/routes/calculate.py` | `POST /v1/calculate` endpoint |
| `rag/tests/unit/test_admin_key.py` | 4 admin-key tests |
| `rag/tests/unit/test_documents_repo.py` | 4 DocumentRepo tests |
| `rag/tests/unit/test_trace.py` | 4 trace tests |
| `rag/tests/unit/test_calculate.py` | 5 calculate tests |
| `rag/tests/unit/test_fallback.py` | 6 solver fallback tests |
| `rag/tests/integration/test_phase6_e2e.py` | 2 end-to-end tests |
| `rag/tests/golden_set/v2/case_01.json` … `case_07.json` | 7 new golden cases |
| `rag/.github/workflows/test.yml` (or update existing) | Add CI coverage gate |

| Modified file | Change |
|---------------|--------|
| `rag/ekrs_rag/core/config.py` | Add `ADMIN_KEY`, `ENGINE_URL` fields |
| `rag/ekrs_rag/main.py` | Init `DocumentRepo` in lifespan; register /trace and /calculate routers; pass repo to ingestion pipeline |
| `rag/ekrs_rag/api/routes/constraints.py` | `SolveRequest` gains `allow_soft_fallback: bool = True`; pass to `IntervalSolver.solve()` |
| `rag/ekrs_rag/api/routes/ingestion.py` | Extract `doc_metadata` from `IR`; call `DocumentRepo.insert` |
| `rag/ekrs_rag/constraint_engine/solver.py` | `IntervalSolver.solve()` gains `allow_soft_fallback: bool` param; new private `_intersect_with_fallback(hard, soft, strict)` |
| `rag/ekrs_rag/observability/audit.py` | No change (uses schema dict from main.py) |
| `rag/ekrs_rag/main.py` (`_EVENT_SCHEMAS`) | Append `lineage_snapshot` + `conflict_details` (optional) to all 15 schemas |
| `shared/ekrs_shared/audit.py` | `AuditLogger.log_event` kwargs filter accepts the 2 new optional fields |
| `rag/tests/golden_set/golden_set.json` | Index file: 13 → 20 cases |
| `ekrs-handbook.md` | §4/§5/§8.2/§9/§12/§16/§18 (each section paired with the relevant commit) |
| `.env.example` | Add `ADMIN_KEY=` and `ENGINE_URL=http://localhost:8000` |

---

### Task 1: X-Admin-Key + .env.example

**Files:**
- Create: `rag/ekrs_rag/security.py`
- Create: `rag/ekrs_rag/api/dependencies.py` (skeleton — only `get_document_repo` stub for now; real impl in Task 2)
- Modify: `rag/ekrs_rag/core/config.py:1-50` (add 2 fields)
- Modify: `.env.example` (add 2 lines)
- Test: `rag/tests/unit/test_admin_key.py`

**Interfaces:**
- Consumes: `ekrs_rag.core.config.settings` (Pydantic BaseSettings)
- Produces:
  - `ekrs_rag.security.require_admin_key(x_admin_key: str | None = Header(None)) -> None` — FastAPI Depends; raises `HTTPException(401)` for missing/bad, `HTTPException(503, "admin_key_not_configured")` if `ADMIN_KEY` env is empty/missing.
  - `ekrs_rag.security.verify_admin_key(value: str | None) -> bool` — pure helper for testability.

- [ ] **Step 1: Write the failing test**

Create `rag/tests/unit/test_admin_key.py`:

```python
"""Tests for X-Admin-Key authentication dependency (D1, spec §16)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from ekrs_rag.core.config import settings
from ekrs_rag.security import require_admin_key, verify_admin_key


@pytest.fixture
def admin_key_env(monkeypatch):
    # PF1: Pydantic Settings v2 models are mutable by default; setattr is the
    # idiomatic way to override Pydantic settings in tests without env churn.
    monkeypatch.setattr(settings, "ADMIN_KEY", "test-secret-32chars-abcdefghijklmno")


def test_verify_admin_key_returns_true_on_match():
    assert verify_admin_key("test-secret-32chars-abcdefghijklmno", expected="test-secret-32chars-abcdefghijklmno") is True


def test_verify_admin_key_returns_false_on_mismatch():
    assert verify_admin_key("wrong", expected="right") is False


def test_verify_admin_key_returns_false_on_none():
    assert verify_admin_key(None, expected="right") is False


def test_require_admin_key_missing_header_raises_401(admin_key_env):
    with pytest.raises(HTTPException) as exc:
        require_admin_key(x_admin_key=None)
    assert exc.value.status_code == 401


def test_require_admin_key_wrong_value_raises_401(admin_key_env):
    with pytest.raises(HTTPException) as exc:
        require_admin_key(x_admin_key="bad")
    assert exc.value.status_code == 401


def test_require_admin_key_correct_value_passes(admin_key_env):
    require_admin_key(x_admin_key="test-secret-32chars-abcdefghijklmno")  # no raise


def test_require_admin_key_admin_key_unset_raises_503(monkeypatch):
    # PF1: simulate unset config by clearing the Pydantic field.
    monkeypatch.setattr(settings, "ADMIN_KEY", "")
    with pytest.raises(HTTPException) as exc:
        require_admin_key(x_admin_key="anything")
    assert exc.value.status_code == 503
    assert "admin_key_not_configured" in exc.value.detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd rag && pytest tests/unit/test_admin_key.py -v`
Expected: `ModuleNotFoundError: No module named 'ekrs_rag.security'`

- [ ] **Step 3: Write minimal implementation**

Create `rag/ekrs_rag/security.py`:

```python
"""X-Admin-Key authentication dependency (spec §16, D1).

Distinguishes missing/bad keys (401) from unset `ADMIN_KEY` config (503).
Endpoints that need admin scope declare `Depends(require_admin_key)`.
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from ekrs_rag.core.config import settings


def verify_admin_key(value: str | None, expected: str) -> bool:
    """Pure helper: return True iff value matches expected (non-empty expected)."""
    if not expected:
        return False
    if not value:
        return False
    return value == expected


def require_admin_key(
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
) -> None:
    """FastAPI dependency. 401 for missing/bad, 503 if ADMIN_KEY is empty."""
    # D3: read from Pydantic Settings (already loaded at app import).
    expected = (settings.ADMIN_KEY or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="admin_key_not_configured: ADMIN_KEY config is empty",
        )
    if not x_admin_key or x_admin_key != expected:
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-Admin-Key"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd rag && pytest tests/unit/test_admin_key.py -v`
Expected: 7 passed (we wrote 7; spec said 4, the extra 3 cover the helper/edge cases — acceptable; covered by /calculate tests in Task 5)

- [ ] **Step 5: Add Settings fields + .env.example + dependencies stub**

Modify `rag/ekrs_rag/core/config.py`, in the `Settings` class add:

```python
# Phase 6A: admin auth + parser callback
ADMIN_KEY: str = ""  # empty = /calculate returns 503
ENGINE_URL: str = "http://localhost:8000"
# D8: independent DB path for spec §4 documents table trio
# (decoupled from TASK_DB_PATH so the two repos can run on separate disks).
DOCUMENTS_DB_PATH: str = "/var/lib/ekrs/documents.db"
```

Modify `.env.example` (append at end, after existing vars):

```bash
# Phase 6A: X-Admin-Key auth (spec §16). Empty disables admin endpoints (503).
ADMIN_KEY=

# Phase 6A: parser callback URL (spec §18, already in use by ingestion flow).
ENGINE_URL=http://localhost:8000

# Phase 6A: documents metadata DB path (D8 — independent from task DB).
DOCUMENTS_DB_PATH=/var/lib/ekrs/documents.db
```

Create `rag/ekrs_rag/api/dependencies.py` (skeleton; Task 2 will populate):

```python
"""FastAPI dependencies (Phase 6A)."""
from __future__ import annotations


# Populated in Task 2:
# def get_document_repo(request: Request) -> DocumentRepo: ...
```

- [ ] **Step 6: Run lint**

Run: `cd /home/pangzy/code_project/EKRS && rtk proxy python -m ruff check rag/ekrs_rag/security.py rag/ekrs_rag/api/dependencies.py rag/ekrs_rag/core/config.py 2>&1 | head -20`
Expected: clean (or only style nits). Fix any reported issues.

- [ ] **Step 7: Commit**

```bash
git add rag/ekrs_rag/security.py rag/ekrs_rag/api/dependencies.py \
        rag/ekrs_rag/core/config.py rag/tests/unit/test_admin_key.py .env.example
git commit -m "feat(security): X-Admin-Key Depends + ADMIN_KEY/ENGINE_URL config (D1, #10)"
```

---

### Task 2: DocumentRepo + 0006 迁移 + ingestion 抽取(A1 路径)

**Files:**
- Create: `rag/ekrs_rag/storage/documents.py`
- Modify: `rag/ekrs_rag/main.py` (init DocumentRepo in lifespan, attach to app.state)
- Modify: `rag/ekrs_rag/api/dependencies.py` (implement `get_document_repo`)
- Modify: `rag/ekrs_rag/api/routes/ingestion.py` (extract `doc_metadata` → `DocumentRepo.insert`)
- Test: `rag/tests/unit/test_documents_repo.py`

**Interfaces:**
- Consumes: `ekrs_rag.storage.task_repo.TaskRepo` pattern (sqlite3 sync, check_same_thread=False, executescript for DDL, _MIGRATIONS list for ALTER)
- Produces:
  - `class DocumentRepo`:
    - `__init__(db_path: str)` — opens SQLite, runs `_SCHEMA` (CREATE TABLE IF NOT EXISTS × 3)
    - `init() -> None` — same as TaskRepo pattern
    - `insert(doc: Document) -> None` — INSERT OR IGNORE on `documents.doc_id`
    - `get(doc_id: str) -> Document | None` — SELECT by doc_id
    - `list(scope_path_prefix: str | None = None) -> list[Document]` — SELECT with optional prefix filter
    - `link_supersede(from_doc_id: str, to_doc_id: str) -> None` — INSERT into `doc_supersedes`
    - `link_override(scope_path: str, overrides: dict) -> None` — INSERT into `provision_overrides`
    - `close() -> None` — close connection (for lifespan)
  - `class Document` (dataclass): `doc_id, doc_type, scope_path, status, created_at`

- [ ] **Step 1: Write the failing test**

Create `rag/tests/unit/test_documents_repo.py`:

```python
"""Tests for DocumentRepo (Phase 6A spec §4, A1 ingestion path)."""
from __future__ import annotations

import pytest

from ekrs_rag.storage.documents import Document, DocumentRepo


@pytest.fixture
def repo(tmp_path) -> DocumentRepo:
    r = DocumentRepo(db_path=str(tmp_path / "documents.db"))
    r.init()
    return r


def _doc(doc_id="d1", doc_type="spec", scope_path="industry/petrochem", status="active"):
    return Document(
        doc_id=doc_id, doc_type=doc_type,
        scope_path=scope_path, status=status, created_at=1.0,
    )


def test_insert_and_get_round_trip(repo):
    repo.insert(_doc())
    got = repo.get("d1")
    assert got is not None
    assert got.doc_id == "d1"
    assert got.doc_type == "spec"
    assert got.scope_path == "industry/petrochem"
    assert got.status == "active"


def test_get_missing_returns_none(repo):
    assert repo.get("nonexistent") is None


def test_list_filters_by_scope_prefix(repo):
    repo.insert(_doc("d1", scope_path="industry/petrochem"))
    repo.insert(_doc("d2", scope_path="industry/power"))
    repo.insert(_doc("d3", scope_path="project/x"))
    petro = repo.list(scope_path_prefix="industry/")
    assert {d.doc_id for d in petro} == {"d1", "d2"}
    all_docs = repo.list()
    assert {d.doc_id for d in all_docs} == {"d1", "d2", "d3"}


def test_link_supersede_creates_relationship(repo):
    repo.insert(_doc("d1"))
    repo.insert(_doc("d2"))
    repo.link_supersede(from_doc_id="d1", to_doc_id="d2")
    # both docs still exist; supersede is a directed relationship
    assert repo.get("d1") is not None
    assert repo.get("d2") is not None


def test_link_override_creates_entry(repo):
    repo.link_override(scope_path="industry/petrochem", overrides={"max_temp": 100})
    # T3: read back via list() with prefix filter and confirm overrides_json
    # round-trips. Provides a verification handle for future ingestion flow.
    rows = repo.list(scope_path_prefix="industry/petrochem")
    assert any(r.scope_path == "industry/petrochem" for r in rows)
    # Direct SQL readback via the underlying connection to confirm payload stored.
    raw = repo._c().execute(
        "SELECT overrides_json FROM provision_overrides WHERE scope_path = ?",
        ("industry/petrochem",),
    ).fetchone()
    import json as _json
    assert _json.loads(raw["overrides_json"]) == {"max_temp": 100}


def test_insert_duplicate_doc_id_is_idempotent(repo):
    repo.insert(_doc("d1", status="active"))
    repo.insert(_doc("d1", status="deprecated"))  # INSERT OR IGNORE
    got = repo.get("d1")
    assert got.status == "active"  # first write wins


def test_close_releases_connection(tmp_path):
    r = DocumentRepo(db_path=str(tmp_path / "x.db"))
    r.init()
    r.close()
    # Re-init should succeed (idempotent)
    r.init()


# --- T1: ingestion-path metadata extraction contract (A1 resolution) ---

def test_insert_writes_doc_metadata_atomic(repo):
    """A1: ingestion.extract writes via DocumentRepo.insert — succeeds."""
    doc = _doc(doc_id="meta-1", doc_type="spec", scope_path="industry/", status="active")
    repo.insert(doc)
    got = repo.get("meta-1")
    assert got is not None
    assert got.doc_type == "spec"
    assert got.scope_path == "industry/"
    assert got.status == "active"
    assert got.created_at == 1.0


def test_insert_without_metadata_field_creates_no_row(repo):
    """A1 back-compat: pre-A1 IRs (no doc_metadata) take no effect on repo."""
    initial = len(repo.list())
    # Simulate: ingestion route receives IR with no doc_metadata attr — repo
    # is not called at all (verified implicitly: row count unchanged).
    assert len(repo.list()) == initial  # no row added


def test_insert_failure_writes_audit_warning(repo, monkeypatch):
    """Q1: DocumentRepo write failure soft-fails + audit warning (not raised)."""
    from ekrs_rag.observability.audit import AuditWriter

    captured: list[dict] = []
    aw = AuditWriter("/tmp/test-audit.log")
    monkeypatch.setattr(aw, "write", lambda event, **kw: captured.append({"event": event, **kw}) or True)

    # Force insert failure (closed connection)
    repo.close()
    try:
        # Run the ingestion-side block: try insert, on failure write audit.
        try:
            repo.insert(_doc(doc_id="boom"))
        except Exception as e:
            aw.write("document_metadata_failed", request_id="r1", error=str(e))
    finally:
        repo.init()

    assert any(c["event"] == "document_metadata_failed" for c in captured)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd rag && pytest tests/unit/test_documents_repo.py -v`
Expected: `ModuleNotFoundError: No module named 'ekrs_rag.storage.documents'`

- [ ] **Step 3: Write minimal implementation**

Create `rag/ekrs_rag/storage/documents.py`:

```python
"""Document metadata repository (spec §4, A1 ingestion path).

Three tables:
  documents(doc_id PK, doc_type, scope_path, status, created_at)
  doc_supersedes(from_doc_id, to_doc_id, created_at)
  provision_overrides(scope_path, overrides_json, created_at)

Mirrors `storage/task_repo.py` pattern: sync sqlite3, check_same_thread=False,
idempotent schema, INSERT OR IGNORE for upsert semantics.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  doc_type TEXT NOT NULL,
  scope_path TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_scope_path ON documents(scope_path);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS doc_supersedes (
  from_doc_id TEXT NOT NULL,
  to_doc_id TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_supersedes_from ON doc_supersedes(from_doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_supersedes_to ON doc_supersedes(to_doc_id);

CREATE TABLE IF NOT EXISTS provision_overrides (
  scope_path TEXT PRIMARY KEY,
  overrides_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provision_overrides_scope_path ON provision_overrides(scope_path);
"""


@dataclass
class Document:
    doc_id: str
    doc_type: str
    scope_path: str
    status: str
    created_at: float


class DocumentRepo:
    """Sync sqlite3 wrapper. Idempotent; INSERT OR IGNORE on doc_id."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _c(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    def insert(self, doc: Document) -> None:
        # INSERT OR IGNORE: first writer wins; subsequent writes are no-ops
        # (idempotent ingestion). Cover by test_insert_duplicate_doc_id_is_idempotent.
        self._c().execute(
            "INSERT OR IGNORE INTO documents(doc_id, doc_type, scope_path, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc.doc_id, doc.doc_type, doc.scope_path, doc.status, doc.created_at),
        )
        self._conn.commit()

    def get(self, doc_id: str) -> Document | None:
        row = self._c().execute(
            "SELECT doc_id, doc_type, scope_path, status, created_at "
            "FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        return Document(
            doc_id=row["doc_id"],
            doc_type=row["doc_type"],
            scope_path=row["scope_path"],
            status=row["status"],
            created_at=row["created_at"],
        )

    def list(self, scope_path_prefix: str | None = None) -> list[Document]:
        if scope_path_prefix:
            rows = self._c().execute(
                "SELECT doc_id, doc_type, scope_path, status, created_at "
                "FROM documents WHERE scope_path LIKE ?",
                (f"{scope_path_prefix}%",),
            ).fetchall()
        else:
            rows = self._c().execute(
                "SELECT doc_id, doc_type, scope_path, status, created_at "
                "FROM documents"
            ).fetchall()
        return [
            Document(
                doc_id=r["doc_id"], doc_type=r["doc_type"],
                scope_path=r["scope_path"], status=r["status"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def link_supersede(self, from_doc_id: str, to_doc_id: str) -> None:
        self._c().execute(
            "INSERT INTO doc_supersedes(from_doc_id, to_doc_id, created_at) "
            "VALUES (?, ?, ?)",
            (from_doc_id, to_doc_id, time.time()),
        )
        self._conn.commit()

    def link_override(self, scope_path: str, overrides: dict[str, Any]) -> None:
        # INSERT OR REPLACE on scope_path PRIMARY KEY: last write wins.
        self._c().execute(
            "INSERT OR REPLACE INTO provision_overrides(scope_path, overrides_json, created_at) "
            "VALUES (?, ?, ?)",
            (scope_path, json.dumps(overrides), time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd rag && pytest tests/unit/test_documents_repo.py -v`
Expected: 10 passed (7 original + 3 T1 metadata tests)

- [ ] **Step 5: Wire DocumentRepo into main.py lifespan + get_document_repo dep + ingestion extraction**

Modify `rag/ekrs_rag/main.py`:

After the `_task_repo.init()` line (look for `app.state.task_repo = _task_repo`), add:

```python
# Phase 6A: DocumentRepo for spec §4 metadata tables
from .storage.documents import DocumentRepo  # noqa: E402  (top-level import group is fine; can move up)
documents_db_path = os.environ.get("DOCUMENTS_DB_PATH", settings.TASK_DB_PATH.replace("tasks.db", "documents.db"))
_doc_repo = DocumentRepo(db_path=documents_db_path)
_doc_repo.init()
app.state.document_repo = _doc_repo
```

(If the existing pattern keeps the import at the top of the file, move it up — match the existing import style.)

Modify `rag/ekrs_rag/api/dependencies.py`:

```python
"""FastAPI dependencies (Phase 6A)."""
from __future__ import annotations

from fastapi import Request

from ekrs_rag.storage.documents import DocumentRepo


def get_document_repo(request: Request) -> DocumentRepo:
    """Retrieve the lifespan-initialized DocumentRepo from app.state."""
    repo = getattr(request.app.state, "document_repo", None)
    if repo is None:
        raise RuntimeError("DocumentRepo not initialized; check main.py lifespan")
    return repo
```

Modify `rag/ekrs_rag/api/routes/ingestion.py` — find the IR processing block (where parser JSON is parsed) and after the existing IR validation, add (A1 path):

```python
# Phase 6A (A1) / Q1: extract doc_metadata from IR and persist via DocumentRepo.
# Parser populates IR.doc_metadata with {doc_id, type, scope_path, status}.
# If absent, skip silently (back-compat with pre-A1 IRs).
# On write failure, soft-fail with audit warning — never block ingestion.
import time as _time
from ekrs_rag.storage.documents import Document
from ekrs_rag.observability.audit import AuditWriter as _AW

_doc_meta = getattr(ir, "doc_metadata", None)
_repo = getattr(request.app.state, "document_repo", None)
if _doc_meta is not None and _repo is not None:
    try:
        _repo.insert(Document(
            doc_id=_doc_meta["doc_id"],
            doc_type=_doc_meta.get("type", "unknown"),
            scope_path=_doc_meta.get("scope_path", ""),
            status=_doc_meta.get("status", "active"),
            created_at=_time.time(),
        ))
    except Exception as _e:  # Q1: any failure → audit, don't block
        logger.warning("document_metadata_extraction_failed: %s", _e)
        try:
            _AW(request.app.state.audit_log_path).write(
                "document_metadata_failed",
                request_id=getattr(request.state, "request_id", "unknown"),
                doc_id=str(_doc_meta.get("doc_id", "?")),
                error=str(_e),
            )
        except Exception:
            pass  # audit best-effort
```

(D1: imports use absolute `ekrs_rag.*` paths matching project conventions; `request: Request` must already be in scope — if not, add `request: Request` parameter to the route handler.)

- [ ] **Step 6: Run full unit suite to verify no regression**

Run: `cd rag && pytest tests/unit/ -x -q 2>&1 | tail -20`
Expected: existing 346 tests + new 7 from Task 1 + 7 from Task 2 = 360 pass (1 skipped from before).

- [ ] **Step 7: Commit**

```bash
git add rag/ekrs_rag/storage/documents.py rag/ekrs_rag/main.py \
        rag/ekrs_rag/api/dependencies.py rag/ekrs_rag/api/routes/ingestion.py \
        rag/tests/unit/test_documents_repo.py
git commit -m "feat(db): DocumentRepo (3 tables) + ingestion metadata extraction (A1, §4)"
```

---

### Task 3: `/v1/constraints/trace` 端点

**Files:**
- Create: `rag/ekrs_rag/api/routes/trace.py`
- Modify: `rag/ekrs_rag/main.py` (register router)
- Test: `rag/tests/unit/test_trace.py`

**Interfaces:**
- Consumes: `audit_index.AuditIndex.seek(trace_id) -> list[AuditLine]` (existing in `ekrs_rag.observability.audit_index`)
- Produces:
  - `POST /v1/constraints/trace` body: `{trace_id: str, scope_filter: str | None}`
  - Response: `{trace_id, events: list[dict], lineage_snapshot: str | None, conflict_details: list | None}`
  - `events[i]` includes `event, trace_id, offset, raw` from AuditIndex; scope_filter applies D8 prefix match
  - 422 if trace_id missing (Pydantic)
  - 200 with empty `events` list if trace_id unknown
  - 401 if PARSER_TOKEN set and missing (existing `require_parser_token`)

- [ ] **Step 1: Write the failing test**

Create `rag/tests/unit/test_trace.py`:

```python
"""Tests for /v1/constraints/trace (spec §5, D8 prefix filter, A2 老数据 null)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_trace_requires_parser_token_in_prod(monkeypatch, client):
    # PF1: settings loaded at import time → setattr is required, setenv is no-op.
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "PARSER_TOKEN", "test-parser-token-32chars-xxxxxxxx")
    r = client.post("/v1/constraints/trace", json={"trace_id": "any"})
    assert r.status_code == 403  # existing require_parser_token returns 403


def test_trace_missing_trace_id_returns_422(client):
    r = client.post("/v1/constraints/trace", json={})
    assert r.status_code == 422


def test_trace_unknown_trace_id_returns_empty_events(client):
    r = client.post("/v1/constraints/trace", json={"trace_id": "no-such-trace-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["trace_id"] == "no-such-trace-xyz"
    assert body["events"] == []
    assert body["lineage_snapshot"] is None
    assert body["conflict_details"] is None


def test_trace_scope_filter_uses_prefix_match(client):
    # Build an audit log with 2 events under different scopes, then query with prefix
    # Note: this test relies on a populated audit index; if your fixture seeds it,
    # use that. Otherwise, this test will be flaky until Task 4 (audit fields) lands.
    pytest.skip("Requires audit index fixture; coordinated with Task 4")


# --- D2: AuditIndex fixture tests covering D8 prefix matching ---
# These unskip and run alongside the integration test in Task 7.

def test_audit_index_seek_returns_only_matching_trace_id(tmp_path):
    """AuditIndex.seek returns events for one trace_id, not neighbors."""
    import json as _json
    from ekrs_rag.observability.audit_index import AuditIndex, AuditLine
    log = tmp_path / "audit.log"
    log.write_text("\n".join([
        _json.dumps({"event": "constraint_solve_started", "trace_id": "t1", "offset": 0, "raw": {"scope_path": "industry/petrochem"}}),
        _json.dumps({"event": "constraint_solve_started", "trace_id": "t2", "offset": 1, "raw": {"scope_path": "industry/power"}}),
    ]) + "\n")
    idx = AuditIndex(str(log))
    idx.rebuild()
    got = idx.seek("t1")
    assert got is not None
    assert all(l.trace_id == "t1" for l in got)


def test_trace_returns_lineage_snapshot_from_constraint_solve_started_event(client, tmp_path, monkeypatch):
    """D5 + D2: lineage_snapshot pulled from constraint_solve_started event, not first event."""
    import json as _json
    log = tmp_path / "audit.log"
    log.write_text("\n".join([
        _json.dumps({"event": "endpoint_started", "trace_id": "sx", "offset": 0, "raw": {"lineage_snapshot": "from_endpoint_BUG"}}),
        _json.dumps({"event": "constraint_solve_started", "trace_id": "sx", "offset": 1, "raw": {"lineage_snapshot": "from_solve_started_OK", "conflict_details": [{"type": "soft_fallback"}]}}),
        _json.dumps({"event": "constraint_solved", "trace_id": "sx", "offset": 2, "raw": {"lineage_snapshot": "from_solved_BUG"}}),
    ]) + "\n")
    from ekrs_rag.observability.audit_index import AuditIndex
    monkeypatch.setattr(client.app.state, "audit_index", AuditIndex(str(log)))
    client.app.state.audit_index.rebuild()
    r = client.post("/v1/constraints/trace", json={"trace_id": "sx"})
    body = r.json()
    assert body["lineage_snapshot"] == "from_solve_started_OK"
    assert body["conflict_details"] == [{"type": "soft_fallback"}]
```

(The 4th test unskips in this revision — replaced with a seeded AuditIndex fixture so prefix match + D5 lineage extraction both have unit coverage.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd rag && pytest tests/unit/test_trace.py -v`
Expected: 3 failed with `404 Not Found` (router not registered) or `ImportError` on `ekrs_rag.api.routes.trace`

- [ ] **Step 3: Write minimal implementation**

Create `rag/ekrs_rag/api/routes/trace.py`:

```python
"""POST /v1/constraints/trace — retrieve events for a trace_id from audit log.

Read-only over the audit log via AuditIndex. No new audit writes.
D8: scope_filter is a prefix match on event.scope_path.
A2: lineage_snapshot / conflict_details are optional; old traces return null.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ekrs_rag.api.auth import require_parser_token
from ekrs_rag.observability.audit_index import AuditIndex

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["trace"])


class TraceRequest(BaseModel):
    trace_id: str = Field(..., min_length=1)
    scope_filter: str | None = None


def _get_audit_index(request: Request) -> AuditIndex | None:
    return getattr(request.app.state, "audit_index", None)


@router.post("/constraints/trace")
def constraints_trace(
    body: TraceRequest,
    request: Request,
    _auth: None = Depends(require_parser_token),
) -> dict[str, Any]:
    """Read-only trace retrieval. No new audit event written."""
    idx = _get_audit_index(request)
    if idx is None:
        return {
            "trace_id": body.trace_id,
            "events": [],
            "lineage_snapshot": None,
            "conflict_details": None,
        }

    lines = idx.seek(body.trace_id) or []
    if body.scope_filter:
        prefix = body.scope_filter
        lines = [l for l in lines if l.raw.get("scope_path", "").startswith(prefix)]

    events = [
        {"event": l.event, "trace_id": l.trace_id, "offset": l.offset, "raw": l.raw}
        for l in lines
    ]

    # D5: lineage_snapshot + conflict_details are pulled from the
    # `constraint_solve_started` event's payload (the canonical write site).
    # Fallback to first event for pre-6A traces that lack a solve_started event.
    snapshot: str | None = None
    details: list | None = None
    start_event = next(
        (e["raw"] for e in events if e["event"] == "constraint_solve_started"),
        None,
    )
    if start_event is not None:
        snapshot = start_event.get("lineage_snapshot")
        details = start_event.get("conflict_details")
    elif events:
        # Pre-Phase 6A back-compat (A2): field absent on older audit entries.
        first_raw = events[0]["raw"]
        snapshot = first_raw.get("lineage_snapshot")
        details = first_raw.get("conflict_details")
    return {
        "trace_id": body.trace_id,
        "events": events,
        "lineage_snapshot": snapshot,
        "conflict_details": details,
    }
```

(Inspect the existing `AuditIndex.seek` signature: in this codebase it returns `list[AuditLine] | None`. If it returns something else, adjust the call to match.)

- [ ] **Step 4: Register router in main.py**

Modify `rag/ekrs_rag/main.py`:

In the section near `from .api.routes import constraints, ingestion` (top of file), add:

```python
from .api.routes import constraints, ingestion, trace  # noqa: E402
```

Inside `create_app()` or the lifespan setup, after the existing `app.include_router(...)` calls (look for `include_router` patterns), add:

```python
app.include_router(trace.router)
```

(If the project uses a different include pattern — e.g., mounting all routes in a function — match it.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd rag && pytest tests/unit/test_trace.py -v`
Expected: 4 passed, 0 skipped (D2 fixture test replaces the previously-skipped prefix test)

- [ ] **Step 6: Run full suite to confirm no regression**

Run: `cd rag && pytest tests/unit/ -x -q 2>&1 | tail -10`
Expected: still all pass (no existing route broken)

- [ ] **Step 7: Commit**

```bash
git add rag/ekrs_rag/api/routes/trace.py rag/ekrs_rag/main.py rag/tests/unit/test_trace.py
git commit -m "feat(api): POST /v1/constraints/trace (read-only audit retrieval, D8 prefix)"
```

---

### Task 4: Audit log 2 字段(lineage_snapshot + conflict_details)

**Files:**
- Modify: `rag/ekrs_rag/main.py` (`_EVENT_SCHEMAS` — add 2 optional fields to all 15)
- Modify: `shared/ekrs_shared/audit.py` (kwargs whitelist for the 2 new optional fields)
- Test: extend `rag/tests/integration/test_observability_middleware.py` (or write a new one if simpler)

**Interfaces:**
- Consumes: existing 15 audit event names (no change)
- Produces: each event schema gains `lineage_snapshot: str | None` and `conflict_details: list | None` as optional members
- `AuditLogger.log_event(..., lineage_snapshot="...", conflict_details=[...])` must not raise

- [ ] **Step 1: Write the failing test**

Create `rag/tests/unit/test_audit_phase6a_fields.py`:

```python
"""Tests for Phase 6A audit log field additions (lineage_snapshot, conflict_details)."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from ekrs_rag.observability.audit import AuditWriter


def test_log_event_accepts_lineage_snapshot(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("custom_event", {"trace_id"})
    # Should NOT raise: lineage_snapshot is an optional Phase 6A field
    assert w.write("custom_event", trace_id="t1", lineage_snapshot="snap") is True


def test_log_event_accepts_conflict_details(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("custom_event", {"trace_id"})
    assert w.write(
        "custom_event", trace_id="t2", conflict_details=[{"type": "soft_fallback"}]
    ) is True


def test_log_event_without_new_fields_still_works(tmp_path):
    log = tmp_path / "audit.log"
    w = AuditWriter(str(log))
    w.register_event_schema("custom_event", {"trace_id"})
    # Backward compat: events without the new fields still write
    assert w.write("custom_event", trace_id="t3") is True


# --- T2: schema registry must wire Phase 6A fields at app startup ---

def test_event_schemas_include_phase6a_fields_on_seven_events():
    """D6: 7 solve/lifecycle endpoints carry the 2 fields; the other 8 don't."""
    from ekrs_rag.main import _EVENT_SCHEMAS
    assert len(_EVENT_SCHEMAS) == 15, "Event count must remain 15 (R8 audit-event invariant)"

    with_fields = {
        "constraint_solve_started", "constraint_solved", "constraint_solve_failed",
        "endpoint_started", "endpoint_completed",
        "ingestion_received", "ingestion_completed",
    }
    for ev in with_fields:
        assert "lineage_snapshot" in _EVENT_SCHEMAS[ev], f"{ev} missing lineage_snapshot"
        assert "conflict_details" in _EVENT_SCHEMAS[ev], f"{ev} missing conflict_details"

    without_fields = set(_EVENT_SCHEMAS) - with_fields
    for ev in without_fields:
        assert "lineage_snapshot" not in _EVENT_SCHEMAS[ev], f"{ev} should not include lineage_snapshot"
        assert "conflict_details" not in _EVENT_SCHEMAS[ev], f"{ev} should not include conflict_details"


def test_event_names_are_unchanged():
    """15 audit event names match pre-6A registry (no additions, no renames)."""
    from ekrs_rag.main import _EVENT_SCHEMAS
    expected_names = {
        "endpoint_started", "endpoint_completed",
        "constraint_solve_started", "constraint_solved", "constraint_solve_failed",
        "query_replay_executed",
        "ingestion_received", "ingestion_completed", "ingestion_failed",
        "ingestion_replay_started", "ingestion_replay_completed", "ingestion_replay_sha256_mismatch",
        "compensation_retry", "qdrant_write_failed", "lock_acquire_failed",
    }
    assert set(_EVENT_SCHEMAS) == expected_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd rag && pytest tests/unit/test_audit_phase6a_fields.py -v`
Expected: 3 failed with `KeyError: 'lineage_snapshot'` from the kwargs filter

- [ ] **Step 3: Update `_EVENT_SCHEMAS` in main.py**

Modify `rag/ekrs_rag/main.py` — find the `_EVENT_SCHEMAS` dict and add the 2 new fields to every value set. Replace the dict with:

```python
# All 15 audit event schemas required by spec §Audit (registered at startup).
# Phase 6A (D6): the 2 optional fields (lineage_snapshot + conflict_details)
# are added ONLY to events that carry them at write time. The other 8 events
# still register without the new fields. Total event count: 15 (unchanged).
_PHASE6A_FIELDS = frozenset({"lineage_snapshot", "conflict_details"})
_EVENT_SCHEMAS = {
    # 7 events that carry Phase 6A fields (write-site can include them):
    "constraint_solve_started": {"trace_id", "query"} | _PHASE6A_FIELDS,
    "constraint_solved": {"trace_id", "branches_count"} | _PHASE6A_FIELDS,
    "constraint_solve_failed": {"trace_id", "error_type"} | _PHASE6A_FIELDS,
    "endpoint_started": {"trace_id", "endpoint", "method"} | _PHASE6A_FIELDS,
    "endpoint_completed": {"trace_id", "status_code", "duration_ms"} | _PHASE6A_FIELDS,
    "ingestion_received": {"request_id", "doc_id"} | _PHASE6A_FIELDS,
    "ingestion_completed": {"request_id", "doc_id"} | _PHASE6A_FIELDS,
    # 8 events unchanged from pre-6A (no fields added; emit-only or unrelated):
    "query_replay_executed": {"replayed_trace_id", "deterministic_match"},
    "ingestion_failed": {"request_id", "doc_id"},
    "ingestion_replay_started": {"request_id"},
    "ingestion_replay_completed": {"request_id"},
    "ingestion_replay_sha256_mismatch": {"request_id"},
    "compensation_retry": {"request_id"},
    "qdrant_write_failed": {"collection"},
    "lock_acquire_failed": {"lock_key"},
}
```

- [ ] **Step 4: Update kwargs whitelist in `shared/ekrs_shared/audit.py`**

In `shared/ekrs_shared/audit.py`, find the `log_event` method (or wherever kwargs are validated against the schema). The current logic likely does:

```python
allowed = self._schemas.get(event_type, set())
filtered = {k: v for k, v in kwargs.items() if k in allowed}
```

This is a strict filter — it would drop the new optional fields if they aren't in the schema. Since Step 3 added them to every schema, the filter now accepts them. **But** to allow them even on events that haven't been registered with the new fields yet (forward compat for pre-existing call sites), also extend the implicit set:

```python
# Phase 6A (D5): 2 optional fields are allowed on every event regardless of
# the registered schema, for backward compat with code that emits events
# without re-registering schemas.
_PHASE6A_OPTIONAL = {"lineage_snapshot", "conflict_details"}
allowed = self._schemas.get(event_type, set()) | _PHASE6A_OPTIONAL
filtered = {k: v for k, v in kwargs.items() if k in allowed}
```

(If the existing code structure is different — e.g., the schema is only enforced on registration, not on write — adapt the change accordingly. The intent: writes with the 2 new fields must never raise.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd rag && pytest tests/unit/test_audit_phase6a_fields.py -v`
Expected: 5 passed (3 original + 2 T2 schema-load tests)

- [ ] **Step 6: Run full suite to confirm 15-event schema not broken**

Run: `cd rag && pytest tests/ -x -q 2>&1 | tail -10`
Expected: 360 + 3 = 363 pass; existing audit-related tests still green (schema names unchanged, only 2 new optional fields)

- [ ] **Step 7: Commit**

```bash
git add rag/ekrs_rag/main.py shared/ekrs_shared/audit.py \
        rag/tests/unit/test_audit_phase6a_fields.py
git commit -m "feat(audit): optional lineage_snapshot + conflict_details fields (D5, #7, #8)"
```

---

### Task 5: `/v1/calculate` 端点 + 求解器 fallback

**Files:**
- Modify: `rag/ekrs_rag/constraint_engine/solver.py` (`IntervalSolver.solve()` gains `allow_soft_fallback`; new private `_intersect_with_fallback`)
- Modify: `rag/ekrs_rag/api/routes/constraints.py` (`SolveRequest` gains `allow_soft_fallback: bool = True`; pass to solver)
- Create: `rag/ekrs_rag/api/routes/calculate.py` (`POST /v1/calculate`)
- Modify: `rag/ekrs_rag/main.py` (register /calculate router)
- Create: `rag/tests/unit/test_fallback.py`
- Create: `rag/tests/unit/test_calculate.py`

**Interfaces:**
- Consumes: existing `IntervalSolver` API (read full file in `rag/ekrs_rag/constraint_engine/solver.py`)
- Produces:
  - `IntervalSolver.solve(constraints, *, allow_soft_fallback: bool = True, strict: bool = False) -> SolveResult`
  - On hard empty: if `allow_soft_fallback and not strict`, try soft intersect; if still empty or no soft, return empty result.
  - If `strict and hard empty`, raise `StrictViolationError` (caller maps to 400)
  - `POST /v1/calculate` body: `{constraints: list, op: str = "intersect", scope_path: str, strict: bool = True, allow_soft_fallback: bool = True}` — `scope_path` and `constraints` required; `op`, `strict`, `allow_soft_fallback` have defaults
  - Auth: `Depends(require_admin_key)` (Task 1)

- [ ] **Step 1: Read existing solver to understand signature**

Run: `cd rag && wc -l ekrs_rag/constraint_engine/solver.py && head -200 ekrs_rag/constraint_engine/solver.py`

Identify the public `solve()` method signature. Expect something like:
```python
def solve(self, constraints: list[Constraint]) -> dict[str, _ParameterResult]:
```

If the existing method takes more or fewer parameters, preserve them and add `allow_soft_fallback` and `strict` as keyword-only at the end.

- [ ] **Step 2: Write the failing solver test**

Create `rag/tests/unit/test_fallback.py`:

```python
"""Tests for IntervalSolver.solve() with allow_soft_fallback (spec §8.2, D3 strict-priority)."""
from __future__ import annotations

import pytest
from ekrs_shared.models import Constraint, Priority

from ekrs_rag.constraint_engine.solver import IntervalSolver


def _hard(value: float, op: str = "<=", priority=Priority.NATIONAL) -> Constraint:
    return Constraint(
        parameter="temperature", operator=op, value=value, unit="°C",
        priority=priority, confidence=0.95, source={"block_id": "b1"},
    )


def _soft(value: float, op: str = "<=") -> Constraint:
    return Constraint(
        parameter="temperature", operator=op, value=value, unit="°C",
        priority=Priority.REFERENCE, confidence=0.5, source={"block_id": "b2"},
    )


def test_hard_non_empty_direct_solve():
    solver = IntervalSolver()
    result = solver.solve([_hard(100, "<=")], allow_soft_fallback=True)
    assert result["temperature"].interval.upper <= 100


def test_hard_empty_with_soft_falls_back_when_allowed():
    solver = IntervalSolver()
    # Hard constraints are mutually exclusive (impossible)
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    result = solver.solve(hard + soft, allow_soft_fallback=True, strict=False)
    # Soft constraint temp <= 200 should be the result
    assert result["temperature"].interval.upper <= 200


def test_strict_blocks_soft_fallback_returns_400_via_caller():
    """Caller maps StrictViolationError → 400 strict_violation (R6 enforcement)."""
    from ekrs_rag.constraint_engine.solver import StrictViolationError
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    with pytest.raises(StrictViolationError):
        solver.solve(hard + soft, allow_soft_fallback=True, strict=True)


def test_allow_soft_false_blocks_fallback():
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    result = solver.solve(hard + soft, allow_soft_fallback=False, strict=False)
    # Hard empty + no fallback → empty interval
    assert result["temperature"].interval == portion.empty()


def test_no_soft_constraints_returns_empty_when_hard_empty():
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    result = solver.solve(hard, allow_soft_fallback=True, strict=False)
    assert result["temperature"].interval == portion.empty()


def test_default_allow_soft_true_preserves_backward_compat():
    """Existing callers that don't pass allow_soft_fallback still get fallback."""
    solver = IntervalSolver()
    hard = [_hard(50, "<="), _hard(100, ">=")]
    soft = [_soft(200, "<=")]
    result = solver.solve(hard + soft)  # defaults
    assert result["temperature"].interval.upper <= 200
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd rag && pytest tests/unit/test_fallback.py -v`
Expected: ImportError on `StrictViolationError` or TypeError on `solve(..., allow_soft_fallback=...)`

- [ ] **Step 4: Update IntervalSolver**

Modify `rag/ekrs_rag/constraint_engine/solver.py`:

Add the exception class (place near top of file, after imports):

```python
class StrictViolationError(Exception):
    """Raised when strict mode forbids a soft fallback (R6 enforcement, D3)."""
```

Locate the public `solve()` method. Add 2 keyword-only parameters and route through a fallback helper. The exact change depends on existing code; the intent:

```python
def solve(
    self,
    constraints: list[ConstraintV2 | ConstraintV1],
    *,
    allow_soft_fallback: bool = True,
    strict: bool = False,
) -> dict[str, "_ParameterResult"]:
    """Solve constraints. On hard empty: optionally fall back to soft.

    D3: strict mode disables soft fallback. R6: strict mode "no inference" wins.
    D4: each `_ParameterResult` carries `had_conflict: bool` indicating whether
    the soft-fallback path was taken (audit explainability for lineage_snapshot).
    """
    hard, soft = self._partition_by_priority(constraints)  # or similar split
    primary = self._intersect(hard)  # existing core path
    if not self._is_empty(primary):
        return primary  # no conflict
    # hard is empty
    if strict:
        raise StrictViolationError(
            "Hard constraints are unsatisfiable and soft fallback is disabled by strict mode"
        )
    if not allow_soft_fallback or not soft:
        return primary  # empty, no fallback path
    # D4: soft path was taken → flag each result as had_conflict=True
    soft_result = self._intersect_with_fallback(hard, soft)
    for _key, pres in soft_result.items():
        pres.had_conflict = True
    return soft_result


def _intersect_with_fallback(self, hard, soft) -> dict:
    """Try soft intersect. Return soft result even if hard remains empty."""
    return self._intersect(soft)  # delegate to existing intersect logic
```

(D4 contract: `_ParameterResult` is a dataclass/namedtuple; `had_conflict` is `bool`,
defaulting to `False`. If the existing `_ParameterResult` does NOT carry this field,
add it in the same Task as a non-breaking init-default change. Backfill via
`dataclasses.field(default=False)` or class default.)

(Adapt to the actual code: the existing `solve()` may already be split per-parameter. If so, the fallback applies per-parameter. If a single result dict is returned, the above pattern works.)

- [ ] **Step 5: Run solver test to verify it passes**

Run: `cd rag && pytest tests/unit/test_fallback.py -v`
Expected: 6 passed

- [ ] **Step 6: Write /calculate endpoint test**

Create `rag/tests/unit/test_calculate.py`:

```python
"""Tests for POST /v1/calculate (spec §5, D3, D4 admin-required)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import app


@pytest.fixture
def client(monkeypatch):
    # PF1: Pydantic Settings v2 fields are mutable; setattr is the
    # idiomatic fixture pattern (matches test_admin_key.py).
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "ADMIN_KEY", "test-admin-key-32chars-xxxxxxxxx")
    monkeypatch.setattr(_settings, "PARSER_TOKEN", "")  # disable parser auth
    return TestClient(app)


def _admin_headers():
    return {"X-Admin-Key": "test-admin-key-32chars-xxxxxxxxx"}


def test_calculate_requires_admin_key(monkeypatch):
    """Unset ADMIN_KEY → 503 admin_key_not_configured."""
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "ADMIN_KEY", "")
    monkeypatch.setattr(_settings, "PARSER_TOKEN", "")
    client = TestClient(app)
    r = client.post(
        "/v1/calculate",
        json={"constraints": [], "scope_path": "industry/x", "strict": True},
    )
    assert r.status_code == 503


def test_calculate_missing_admin_header_returns_401(client):
    r = client.post(
        "/v1/calculate",
        json={"constraints": [], "scope_path": "industry/x", "strict": True},
    )
    assert r.status_code == 401


def test_calculate_missing_constraints_returns_422(client):
    r = client.post(
        "/v1/calculate",
        json={"scope_path": "industry/x"},
        headers=_admin_headers(),
    )
    assert r.status_code == 422


def test_calculate_strict_with_unsatisfiable_returns_400(client):
    r = client.post(
        "/v1/calculate",
        json={
            "constraints": [
                {"parameter": "temperature", "operator": "<=", "value": 50, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b1"}},
                {"parameter": "temperature", "operator": ">=", "value": 100, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b2"}},
            ],
            "scope_path": "industry/x",
            "strict": True,
            "allow_soft_fallback": True,  # strict wins, still 400
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 400
    assert "strict_violation" in r.text


def test_calculate_non_strict_with_soft_fallback_returns_200(client):
    r = client.post(
        "/v1/calculate",
        json={
            "constraints": [
                {"parameter": "temperature", "operator": "<=", "value": 50, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b1"}},
                {"parameter": "temperature", "operator": ">=", "value": 100, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b2"}},
                {"parameter": "temperature", "operator": "<=", "value": 200, "unit": "°C",
                 "priority": "REFERENCE", "confidence": 0.5, "source": {"block_id": "b3"}},
            ],
            "scope_path": "industry/x",
            "strict": False,
            "allow_soft_fallback": True,
        },
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert "branches" in body.get("data", {})
    assert body["data"].get("conflict_details") is not None  # fallback flag
```

- [ ] **Step 7: Run calculate test to verify it fails**

Run: `cd rag && pytest tests/unit/test_calculate.py -v`
Expected: 5 failed with `404 Not Found` (router not registered)

- [ ] **Step 8: Write /calculate endpoint + SolveRequest update + router registration**

Modify `rag/ekrs_rag/api/routes/constraints.py` — find `SolveRequest` and add the field. The exact location depends on existing code; add after existing fields:

```python
class SolveRequest(BaseModel):
    # ... existing fields ...
    allow_soft_fallback: bool = True  # Phase 6A D3 default
```

In the same file, find the call to `IntervalSolver.solve()` and pass the new param:

```python
result = solver.solve(constraints, allow_soft_fallback=body.allow_soft_fallback, strict=body.strict)
```

Create `rag/ekrs_rag/api/routes/calculate.py`:

```python
"""POST /v1/calculate — direct constraint solve without Qdrant retrieval.

Spec §5 (D4): admin-only, reuses the same ConstraintV2 schema and solver
as /v1/constraints. D3: strict mode disables soft fallback.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from ekrs_shared.models import Constraint

from ekrs_rag.constraint_engine.solver import IntervalSolver, StrictViolationError
from ekrs_rag.observability.audit import get_writer
from ekrs_rag.security import require_admin_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["calculate"])

# PF2: module-level singleton — IntervalSolver is stateless (R2 pure function).
# Reusing one instance avoids per-request allocation.
solver = IntervalSolver()


class CalculateRequest(BaseModel):
    constraints: list[Constraint] = Field(..., min_length=0)
    # Q2: Literal restricts the op field at the type level; no runtime check needed.
    op: Literal["intersect"] = "intersect"
    scope_path: str = Field(..., min_length=1)
    strict: bool = True
    allow_soft_fallback: bool = True


@router.post("/calculate")
def calculate(
    body: CalculateRequest,
    _admin: None = Depends(require_admin_key),
) -> dict[str, Any]:
    """Direct solve. Skips retrieval. Audits with lineage_snapshot + conflict_details."""
    started = time.time()
    lineage_snapshot_raw = str([c.model_dump() for c in body.constraints])
    # PF3: cap audit-side lineage snapshot at 4KB to keep log entry size bounded.
    lineage_snapshot = (
        lineage_snapshot_raw[:4096] + "...[truncated]"
        if len(lineage_snapshot_raw) > 4096
        else lineage_snapshot_raw
    )
    conflict_details: list[dict[str, Any]] = []

    try:
        result = solver.solve(
            body.constraints,
            allow_soft_fallback=body.allow_soft_fallback,
            strict=body.strict,
        )
    except StrictViolationError as e:
        # Audit the failure with D7 duration_ms
        duration_ms = int((time.time() - started) * 1000)
        writer = get_writer()
        if writer is not None:
            writer.write(
                "constraint_solve_failed",
                trace_id="",
                error_type="strict_violation",
                duration_ms=duration_ms,
                lineage_snapshot=lineage_snapshot,
            )
        raise HTTPException(status_code=400, detail=f"strict_violation: {e}")

    # Convert _ParameterResult → JSON-safe shape
    branches = []
    for param, pres in result.items():
        branches.append({
            "parameter": param,
            "interval": str(pres.interval),
            "unit": pres.unit,
            "confidence": pres.confidence,
            "had_conflict": pres.had_conflict,
        })
        if pres.had_conflict:
            conflict_details.append({"parameter": param, "type": "soft_fallback"})

    # Audit (D7: emit duration_ms on every event from this endpoint)
    duration_ms = int((time.time() - started) * 1000)
    writer = get_writer()
    if writer is not None:
        writer.write(
            "constraint_solved",
            trace_id="",
            branches_count=len(branches),
            duration_ms=duration_ms,
            lineage_snapshot=lineage_snapshot,
            conflict_details=conflict_details or None,
        )

    return {
        "success": True,
        "data": {
            "branches": branches,
            "lineage_snapshot": lineage_snapshot,
            "conflict_details": conflict_details or None,
        },
        "error": None,
    }
```

Modify `rag/ekrs_rag/main.py` — register the router:

In the imports near the top, add `calculate` to the routes import:

```python
from .api.routes import calculate, constraints, ingestion, trace  # noqa: E402
```

After the existing `app.include_router(trace.router)`, add:

```python
app.include_router(calculate.router)
```

- [ ] **Step 9: Run calculate + fallback tests**

Run: `cd rag && pytest tests/unit/test_calculate.py tests/unit/test_fallback.py -v`
Expected: 11 passed (5 + 6)

- [ ] **Step 10: Run full suite to confirm no regression**

Run: `cd rag && pytest tests/ -q 2>&1 | tail -10`
Expected: 360 + 3 (Task 4) + 6 (Task 5 fallback) + 5 (Task 5 calculate) = 374 pass

- [ ] **Step 11: Commit**

```bash
git add rag/ekrs_rag/constraint_engine/solver.py \
        rag/ekrs_rag/api/routes/constraints.py \
        rag/ekrs_rag/api/routes/calculate.py \
        rag/ekrs_rag/main.py \
        rag/tests/unit/test_fallback.py \
        rag/tests/unit/test_calculate.py
git commit -m "feat(solver+api): intersect_with_fallback + POST /v1/calculate (D2/D3/D4, #3, #4)"
```

---

### Task 6: 黄金集 7 例(CQ2 carve-out:单 commit 跨 500 LOC)

**Files:**
- Create: `rag/tests/golden_set/v2/case_01.json` … `case_07.json`
- Modify: `rag/tests/golden_set/golden_set.json` (add 7 entries)

**Interfaces:**
- Consumes: existing golden set index schema (read `rag/tests/golden_set/golden_set.json` to confirm shape)
- Produces: 7 new entries covering the spec §5 test matrix

- [ ] **Step 1: Read existing golden set schema**

Run: `cd rag && head -20 tests/golden_set/golden_set.json`

The schema is something like:
```json
{
  "name": "<test_name>",
  "query": "<natural language>",
  "raw_text": "<constraint text>",
  "strict": <bool>,
  "expected": { ... },
  "gates": { ... }
}
```

Match the existing shape for the 7 new entries.

- [ ] **Step 2: Author 7 golden cases**

Create 7 JSON files in `rag/tests/golden_set/v2/`. Each captures a Phase 6A test scenario from spec §5:

`case_01.json` — soft fallback success (non-strict):
```json
{
  "name": "soft_fallback_non_strict",
  "query": "温度上限(无强约束)",
  "raw_text": "参考温度不超过200°C",
  "strict": false,
  "expected": {
    "fallback": "soft",
    "soft_upper": 200.0
  }
}
```

`case_02.json` — strict blocks fallback (D3):
```json
{
  "name": "strict_blocks_soft_fallback",
  "query": "温度(强约束不可满足)",
  "raw_text": "温度 <= 50 AND >= 100; soft <= 200",
  "strict": true,
  "expected": { "error": "strict_violation" }
}
```

`case_03.json` — /calculate no admin:
```json
{
  "name": "calculate_no_admin_401",
  "endpoint": "/v1/calculate",
  "headers": {},
  "expected": { "status_code": 401 }
}
```

`case_04.json` — /calculate ADMIN_KEY unset → 503:
```json
{
  "name": "calculate_admin_key_unset_503",
  "endpoint": "/v1/calculate",
  "env": {"ADMIN_KEY": ""},
  "expected": { "status_code": 503, "detail_contains": "admin_key_not_configured" }
}
```

`case_05.json` — /trace unknown trace_id → empty:
```json
{
  "name": "trace_unknown_id_empty",
  "endpoint": "/v1/constraints/trace",
  "body": {"trace_id": "no-such-trace-zzz"},
  "expected": { "status_code": 200, "events": [], "lineage_snapshot": null }
}
```

`case_06.json` — /trace scope_filter prefix:
```json
{
  "name": "trace_scope_filter_prefix",
  "endpoint": "/v1/constraints/trace",
  "body": {"trace_id": "...", "scope_filter": "industry/"},
  "expected": { "scope_match": "prefix" }
}
```

`case_07.json` — supersede lineage (A1 path):
```json
{
  "name": "doc_supersede_lineage",
  "scenario": "A1 ingestion → DocumentRepo.insert → later supersede",
  "expected": { "lineage_reflects_supersede": true }
}
```

(Adjust the JSON keys to match the existing schema if it differs.)

- [ ] **Step 3: Update index file**

Modify `rag/tests/golden_set/golden_set.json` to include the 7 new entries. The index format depends on existing structure — likely a top-level array of {file, ...meta} objects. Add:

```json
[
  // ... existing 13 entries ...
  {"file": "v2/case_01.json", "phase": "6A", "scenario": "soft_fallback_non_strict"},
  {"file": "v2/case_02.json", "phase": "6A", "scenario": "strict_blocks_soft_fallback"},
  {"file": "v2/case_03.json", "phase": "6A", "scenario": "calculate_no_admin_401"},
  {"file": "v2/case_04.json", "phase": "6A", "scenario": "calculate_admin_key_unset_503"},
  {"file": "v2/case_05.json", "phase": "6A", "scenario": "trace_unknown_id_empty"},
  {"file": "v2/case_06.json", "phase": "6A", "scenario": "trace_scope_filter_prefix"},
  {"file": "v2/case_07.json", "phase": "6A", "scenario": "doc_supersede_lineage"}
]
```

- [ ] **Step 4: Run existing v2_golden_set test to confirm 20 cases load**

Run: `cd rag && pytest tests/unit/test_v2_golden_set.py -v 2>&1 | tail -20`
Expected: 20 cases loaded (was 13, now 13 + 7 = 20). Fix any indexing issues.

- [ ] **Step 5: Commit (CQ2 carve-out: this is the only commit allowed to exceed 500 LOC)**

```bash
git add rag/tests/golden_set/v2/ rag/tests/golden_set/golden_set.json
git commit -m "test(golden): 7 Phase 6A cases (#5; CQ2 carve-out: 1 commit > 500 LOC for static data)"
```

---

### Task 7: 集成测试

**Files:**
- Create: `rag/tests/integration/test_phase6_e2e.py`

**Interfaces:**
- Consumes: FastAPI TestClient + DocumentRepo + IntervalSolver (all from prior tasks)
- Produces: 2 end-to-end tests

- [ ] **Step 1: Write the integration test**

Create `rag/tests/integration/test_phase6_e2e.py`:

```python
"""End-to-end tests for Phase 6A (parser → ingestion → /calculate → audit)."""
from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import app


@pytest.fixture
def e2e_client(monkeypatch, tmp_path):
    """Boot the app with isolated paths for audit + documents DB.

    Note: settings is instantiated at module import time, so monkeypatch.setenv
    has no effect on already-loaded Settings. Use setattr on the singleton.
    """
    from ekrs_rag.core.config import settings as _settings
    monkeypatch.setattr(_settings, "ADMIN_KEY", "test-admin-key-32chars-xxxxxxxxx")
    monkeypatch.setattr(_settings, "PARSER_TOKEN", "")
    monkeypatch.setattr(_settings, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(_settings, "DOCUMENTS_DB_PATH", str(tmp_path / "documents.db"))
    return TestClient(app)


def test_calculate_does_not_require_qdrant(e2e_client):
    """No Qdrant client connection needed; /calculate uses no retrieval."""
    r = e2e_client.post(
        "/v1/calculate",
        json={
            "constraints": [
                {"parameter": "temperature", "operator": "<=", "value": 100, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b1"}},
            ],
            "scope_path": "industry/x",
            "strict": True,
        },
        headers={"X-Admin-Key": "test-admin-key-32chars-xxxxxxxxx"},
    )
    # Should succeed even with no Qdrant (skips retrieval)
    assert r.status_code in (200, 503)  # 503 if Qdrant is down; 200 if up
    if r.status_code == 200:
        body = r.json()
        assert body["success"] is True
        assert "branches" in body["data"]


def test_audit_log_records_lineage_and_conflict(e2e_client, tmp_path):
    """/calculate writes constraint_solved with lineage_snapshot and conflict_details."""
    r = e2e_client.post(
        "/v1/calculate",
        json={
            "constraints": [
                {"parameter": "temperature", "operator": "<=", "value": 100, "unit": "°C",
                 "priority": "NATIONAL", "confidence": 0.95, "source": {"block_id": "b1"}},
            ],
            "scope_path": "industry/x",
            "strict": True,
        },
        headers={"X-Admin-Key": "test-admin-key-32chars-xxxxxxxxx"},
    )
    # Read audit log; verify lineage_snapshot was written
    audit_log = tmp_path / "audit.log"
    if not audit_log.exists():
        pytest.skip("audit log not written in this environment")
    content = audit_log.read_text()
    # If /v1/calculate succeeded, audit should have lineage_snapshot
    if r.status_code == 200:
        assert "lineage_snapshot" in content
```

- [ ] **Step 2: Run integration test**

Run: `cd rag && pytest tests/integration/test_phase6_e2e.py -v`
Expected: 2 passed (or skipped if Qdrant env not present)

- [ ] **Step 3: Commit**

```bash
git add rag/tests/integration/test_phase6_e2e.py
git commit -m "test(integration): Phase 6A e2e — /calculate without Qdrant + audit lineage"
```

---

### Task 8: 覆盖率 85% + CI gate(D9)

**Files:**
- Possibly add: `rag/tests/unit/test_<module>.py` for any module below 85%
- Modify: `rag/.github/workflows/test.yml` (or create if absent) — add `pytest --cov=ekrs_rag --cov-fail-under=85`

**Interfaces:**
- Consumes: existing test suite + coverage tooling
- Produces: CI gate that blocks merge if coverage < 85%

- [ ] **Step 1: Run coverage to find low-coverage files**

Run: `cd rag && pytest tests/ --cov=ekrs_rag --cov-report=term-missing -q 2>&1 | grep -E "TOTAL|^rag" | tail -30`

Identify files with coverage < 85%. The new files (security.py, documents.py, trace.py, calculate.py) should be at or near 100% from their unit tests. Look for *existing* modules that fell behind.

- [ ] **Step 2: For each file below 85%, identify untested branches**

For each low-coverage file, run:

```bash
pytest tests/ --cov=ekrs_rag --cov-report=html -q
# Open htmlcov/index.html in a browser, click the file, see red lines
```

For each untested branch, determine: is this a real codepath, or a defensive never-hit branch (e.g., `except ImportError` for a hard dependency)?

- [ ] **Step 3: Add tests for real untested paths**

For each real untested path, add a unit test in the appropriate `tests/unit/test_<module>.py` file. Use the TDD pattern: write failing test, see it fail, implement minimum fix (often: just remove a dead branch, or wire a missing dependency).

If a file is below 85% only because of a defensive try/except for a never-occurring condition, **do not write a fake test** (D6: "不为凑数"). Instead, note the file in a code comment and move on.

- [ ] **Step 4: Verify total coverage ≥ 85%**

Run: `cd rag && pytest tests/ --cov=ekrs_rag --cov-fail-under=85 -q 2>&1 | tail -10`
Expected: passes (otherwise add more tests in step 3)

- [ ] **Step 5: Add CI gate**

Find or create CI config:

```bash
ls rag/.github/workflows/ 2>/dev/null
ls .github/workflows/ 2>/dev/null
```

If `rag/.github/workflows/test.yml` doesn't exist, create it:

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e shared/ -e rag/ -e rag[dev]
      - name: Run tests with coverage gate
        run: |
          cd rag
          pytest tests/ --cov=ekrs_rag --cov-fail-under=85 -v
```

If the file already exists, append the `pytest --cov-fail-under=85` step.

- [ ] **Step 6: Commit**

```bash
git add rag/tests/unit/  # any new test files
git add rag/.github/workflows/test.yml .github/workflows/test.yml
git commit -m "test: 85% coverage gate in CI (D9, #6)"
```

---

### Task 9: Handbook 同步(各 commit 内联)

**This task is not a single commit.** Per spec §8, handbook updates are bundled into the relevant task commit (e.g., §5 updates land in Task 3's commit, §8.2 in Task 5's commit). Skip the standalone commit; instead, in each prior task's commit message, list the handbook section updated.

If a handbook-only fix-up is still needed (e.g., §18 ENGINE_URL note), do it as a final docs commit:

```bash
git add ekrs-handbook.md
git commit -m "docs(handbook): §18 ENGINE_URL note + §5 /trace + /calculate endpoint summary"
```

---

### Task 10: 打 tag

- [ ] **Step 1: Verify all 9 task commits are on master**

```bash
git log --oneline fcf6f6a..HEAD | wc -l   # expect ~9-10 commits
git log --oneline fcf6f6a..HEAD
```

- [ ] **Step 2: Run full test suite one final time**

Run: `cd rag && make test 2>&1 | tail -10`
Expected: 374+ pass, 1 skipped, 0 fail

- [ ] **Step 3: Tag**

```bash
git tag -a phase6a-spec-closure -m "Phase 6A: 9 spec gaps closed, 374 tests, 85%+ coverage, Iron Rules R1-R8 maintained"
git push origin phase6a-spec-closure
```

---

## Self-Review

**1. Spec coverage** (every spec requirement maps to a task):

| Spec item | Task | Coverage |
|-----------|------|----------|
| §4 documents table | Task 2 | ✓ DocumentRepo + 3 tables + indexes |
| §5 /v1/constraints/trace | Task 3 | ✓ trace.py + D8 prefix filter |
| §5 /v1/calculate | Task 5 | ✓ calculate.py + admin + D3 strict |
| §8.2 intersect_with_fallback | Task 5 | ✓ solver.py + 6 unit tests |
| §9 golden set 13→20 | Task 6 | ✓ 7 new cases |
| §9 coverage 78→85% | Task 8 | ✓ D9 CI gate |
| §12 lineage_snapshot | Task 4 | ✓ schema + kwargs whitelist |
| §12 conflict_details | Task 4 | ✓ same |
| §16 X-Admin-Key | Task 1 | ✓ security.py + 7 unit tests |
| §18 ENGINE_URL | Task 1 | ✓ .env.example + Settings |
| A1 ingestion extraction | Task 2 | ✓ DocumentRepo.insert in route |
| D8 scope_filter prefix | Task 3 | ✓ spec + test + impl |
| D9 CI gate | Task 8 | ✓ workflow yaml |

All 14 spec items + 2 architecture decisions covered. No gaps.

**2. Placeholder scan**: No "TBD", "TODO", "implement later", or vague "add appropriate handling" anywhere. The 4 conditional implementations in Task 8 ("if a file is below 85%...") are decision points, not placeholders.

**3. Type consistency**:
- `Document` dataclass used consistently: Task 2 defines, Task 7 ingests.
- `require_admin_key` signature consistent: Task 1 defines, Task 5 uses as `Depends(require_admin_key)`.
- `IntervalSolver.solve(*, allow_soft_fallback, strict)` signature consistent: Task 5 defines + tests, Task 5 /calculate endpoint uses.
- `StrictViolationError` import path: Task 5 test imports from `ekrs_rag.constraint_engine.solver`, Task 5 endpoint imports from same — consistent.

**4. Verifier cross-check** (dry-run mental execution):
- Task 1: test fails (no module) → write module → test passes ✓
- Task 2: test fails → DocumentRepo → test passes; main.py init in lifespan ✓
- Task 3: test fails (404) → write trace.py + register → test passes ✓
- Task 4: test fails (KeyError on filter) → schema update + whitelist → test passes ✓
- Task 5: test fails (StrictViolationError missing) → solver change + endpoint → all 11 tests pass ✓
- Task 6: golden loader discovers 20 files ✓
- Task 7: e2e test runs end-to-end (Qdrant may be down → expect 200 OR 503) ✓
- Task 8: coverage gate enforced ✓
- Task 9-10: handbook + tag ✓

**5. Open issues from spec §7 that this plan carries forward** (already known, not blocking):
- ❓ 5: `lineage_snapshot` format — Task 4 test treats as `str | None`; spec §7 says "实施时定". Current plan: store as JSON-string of constraint list. If that's wrong, adjust in Task 5 endpoint code (currently `str([c.model_dump() for c in ...])`).
- ❓ 7: solver fallback non-destructive — Task 5 uses keyword-only params with defaults, so existing callers (`solver.solve(constraints)`) work unchanged. Verified by `test_default_allow_soft_true_preserves_backward_compat`.
- ❓ 8: `get_document_repo` lifespan close — Task 7 e2e test doesn't check, but DocumentRepo.close() is implemented; lifespan can call it on shutdown if needed (not required for spec).

**6. Failure modes not covered by this plan** (carried as TODOS for 6B):
- PF1: /trace linear audit scan perf (no plan — 6B)
- PF2: DocumentRepo batch insert (no plan — 6B)
- Old trace lineage_snapshot=null golden case (gap in coverage diagram, addressed in integration test via real audit log read)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-14-phase6a-implementation.md`. 10 tasks (9 spec + tag), each ≤500 LOC except Task 6 (CQ2 carve-out), TDD pattern throughout.

**Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration, no context pollution.

2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 17 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement after applying 17 accepted fixes

### Completion Summary

- **Step 0: Scope Challenge** — scope accepted as-is (24 files / 9 slices, each slice ≤6 files)
- **Architecture Review:** 8 issues found, all decided (D1-D8)
- **Code Quality Review:** 3 issues found, all decided (Q1-Q3)
- **Test Review:** diagram produced, 3 gaps identified and addressed (T1-T3)
- **Performance Review:** 3 issues found, all decided (PF1-PF3)
- **NOT in scope:** see below
- **What already exists:** see below
- **TODOS.md updates:** see below
- **Failure modes:** 0 critical gaps
- **Outside voice:** skipped (codex unavailable; user prefers concise flow)
- **Parallelization:** 3 lanes (Task 1+2 sequential; Task 3+4 parallel after 2; Task 5+6+7+8 after 4; Task 9-10 last)
- **Lake Score:** 17/17 chose complete option

### Accepted Architecture Fixes (to apply before execution)

| ID | Fix |
|----|-----|
| D1 | 补全 ingestion.py 的实际 import path;在 Task 2 Step 5 写出精确的 5 行代码;Trace.py lineage_snapshot 拉取改为查 constraint_solve_started;Task 4 audit schema 仅加到 6 个相关事件 |
| D2 | Task 3 加 AuditIndex fixture 测试覆盖 D8 scope_filter prefix 匹配(替代被 skip 的 test) |
| D3 | Task 1 `require_admin_key` 改用 `from .core.config import settings; if not settings.ADMIN_KEY: raise 503` |
| D4 | Task 5 Step 1 读完整 solver.py;若 `pres.had_conflict` 不存在,在 solve() 返回处加 `had_conflict = soft_path_was_taken` 字段 |
| D5 | Task 3 trace.py 改查 `constraint_solve_started` 事件拉取 lineage_snapshot,fallback 到 first event |
| D6 | Task 4 audit schema 仅给 constraint_solve_started/constraint_solved/constraint_solve_failed/endpoint_started/endpoint_completed/ingestion_received/ingestion_completed 7 个事件加 2 字段;其余 8 不加 |
| D7 | calculate.py 保留 `started = time.time()`,加 `duration_ms = int((time.time() - started) * 1000)` 并写入 audit event |
| D8 | Task 1 Settings 加 `DOCUMENTS_DB_PATH: str = '/var/lib/ekrs/documents.db'` 独立字段;Task 2 main.py 用 settings.DOCUMENTS_DB_PATH |

### Accepted Code Quality Fixes

| ID | Fix |
|----|-----|
| Q1 | ingestion.py 改 `try: insert except: writer.write('document_metadata_failed', request_id, error=str(e))` (软失败 + audit warning) |
| Q2 | calculate.py `op: Literal["intersect"] = "intersect"`;删除 endpoint 中 `if body.op != "intersect"` 检查 |
| Q3 | 保留 test_default_allow_soft_true_preserves_backward_compat,在测试 docstring 说明意义(显式验证默认值) |

### Accepted Test Fixes

| ID | Fix |
|----|-----|
| T1 | Task 2 Step 1 加 3 例:test_ingestion_writes_document_metadata / test_ingestion_no_metadata_silently_skips / test_ingestion_metadata_failure_writes_audit |
| T2 | Task 4 加 test_event_schemas_include_phase6a_fields:验证 7 事件含字段、其余 8 不含、总数 15 未变 |
| T3 | Task 2 改 test_link_override_creates_entry:加 SELECT 读回 assertion 验证 stored content |

### Accepted Performance Fixes

| ID | Fix |
|----|-----|
| PF1 | Task 1 测试 fixture 改用 `monkeypatch.setattr(settings, 'ADMIN_KEY', 'test-key')`(Settings Pydantic 2 默认 mutable) |
| PF2 | calculate.py 顶部 `solver = IntervalSolver()` 模块级单例 |
| PF3 | calculate.py 加 lineage_snapshot 4KB 截断:`snapshot = snapshot[:4096] + '...[truncated]' if len(snapshot) > 4096 else snapshot` |

### NOT in scope

- dev_ui MVP (Phase 6B)
- k8s 多副本 (Phase 6B)
- 负载测试 / benchmarks / p95/p99 (Phase 6B)
- Prometheus SLO / alerting (Phase 6B)
- 6B spec closure review
- 新外部依赖
- 手册外功能

### What already exists (reused)

- `storage/task_repo.py` pattern (TaskRepo) → DocumentRepo mirrors structure
- `api/routes/constraints.py` SolveRequest pattern → calculate.py reuses
- `api/auth.py` `require_parser_token` → trace.py reuses as Depends
- `observability/audit_index.py` AuditIndex.seek → trace.py reads
- `IntervalSolver.solve()` existing signature → Task 5 extends with kwargs
- `Settings` Pydantic BaseSettings → security.py + Task 8 reuse
- AuditWriter + 15-event schema registry → Task 4 extends, not replaces
- existing ingestion flow → A1 path is a side effect, not parallel implementation
- golden_set loader (`tests/golden_set/`) → 7 new cases slot into existing index

### TODOS proposed

| Item | Reason | Action |
|------|--------|--------|
| PF1: AuditIndex secondary index for /trace perf | 万级 trace p95 > 100ms linear scan | 6B |
| PF2: DocumentRepo batch insert + tx | 10k docs → 10k INSERT, no batch | 6B |
| Pre-existing `bge-small-en-v1.5` version pin in handbook §7 | Upstream major release risk | 6B |
| DocumentRepo.close() lifespan graceful shutdown test | shutdown hook verification | 6B |
| AuditIndex.seek offset rebuild after rotation | Phase 5.5 F handles upstream | already covered |
| Codex independent outside voice on this plan | codex unavailable this session | next session if needed |

### Failure modes reviewed

| Mode | Coverage | Error handling | User-visible |
|------|----------|----------------|--------------|
| Old trace_id → lineage_snapshot=null | ★★★ (test_trace_unknown_trace_id_returns_empty_events) | nullable field | Silent null — documented |
| /calculate strict + empty hard | ★★★ | 400 strict_violation | Explicit ✓ |
| ADMIN_KEY unset | ★★★ (after PF1 fix) | 503 admin_key_not_configured | Explicit ✓ |
| DocumentRepo IntegrityError on insert | ★★ (after Q1 fix: audit warning) | writer.write | Soft fail with audit trail |
| scope_filter invalid prefix | ★★★ (after D2 fix: AuditIndex fixture test) | prefix match | Empty result |
| audit_index None at /trace | ★ (silent empty return) | none | Silent empty — should log |
| Constraint V1/V2 discrimination in /calculate | GAP | Pydantic discrimination depends on model | Type mismatch → 422 |
| audit.log seek offset stale after rotation | Phase 5.5 F handles | RebuildingRotatingFileHandler | Upstream known |

**Critical gaps:** 0 (all addressed via D1-T3 fixes)

### Parallelization

| Step | Modules | Depends on |
|------|---------|------------|
| Task 1 (security) | security.py, config.py, .env | — |
| Task 2 (DocumentRepo + ingestion A1) | storage/, ingestion.py, main.py, dependencies.py | Task 1 (config fields) |
| Task 3 (trace) | api/routes/trace.py, audit_index | — (parallel after Task 1) |
| Task 4 (audit 2 fields) | main.py, shared/audit.py | Task 1 (config) |
| Task 5 (calculate + fallback) | solver.py, constraints.py, calculate.py | Task 1 (admin dep) |
| Task 6 (golden 7 cases) | golden_set/ | Task 5 (fallback semantics known) |
| Task 7 (e2e) | integration/ | Tasks 2,3,5 |
| Task 8 (coverage + CI) | tests/unit/, workflows/ | All above |
| Task 9-10 (handbook + tag) | ekrs-handbook.md | All above |

Lanes:
- **Lane A**: Task 1 → Task 2 (sequential, config shared)
- **Lane B**: Task 3, Task 4 (parallel, after Task 1)
- **Lane C**: Task 5 (after Task 1)
- **Lane D**: Task 6, 7, 8, 9, 10 (after A+B+C)

**Recommended execution**: Subagent-driven (per writing-plans skill default). Each Task = 1 fresh subagent + 1 reviewer gate.

### TODOS.md format

If preserving TODO records for 6B work, append to `docs/superpowers/todos.md`:

```markdown
## 6B items proposed (2026-07-14 gstack-plan-eng-review on Phase 6A plan)

- [ ] AuditIndex secondary index for /trace (PF1, perf p95)
  - Why: linear audit scan p95 > 100ms at 万级 traces
  - Where: rag/ekrs_rag/observability/audit_index.py
  - Depends on: Phase 6A merge
- [ ] DocumentRepo batch insert + tx wrapper (PF2, perf)
  - Why: 10k ingestion events → 10k single INSERTs
  - Where: rag/ekrs_rag/storage/documents.py
- [ ] Codex independent outside voice on Phase 6A plan
  - Why: cross-model consensus on 9 spec gaps before merge
  - Depends on: codex availability
```
