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
