"""aiosqlite tasks 表 — 任务状态 + 幂等.

使用 sqlite3 同步接口实现（aiosqlite 在 0.20+ 移除了同步入口，
仅保留 async context-manager；本类只做同步 CRUD，不涉及事件循环）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  request_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at);
"""


class TaskRepo:
    """同步 sqlite3 包装 — 任务状态记录 + 幂等去重."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _c(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    def try_insert(self, request_id: str, doc_id: str) -> bool:
        now = time.time()
        try:
            self._c().execute(
                "INSERT INTO tasks(request_id, doc_id, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'PENDING', 0, ?, ?)",
                (request_id, doc_id, now, now),
            )
            self._c().commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_status(self, request_id: str, status: str, error: str | None = None) -> None:
        self._c().execute(
            "UPDATE tasks SET status=?, last_error=?, updated_at=? WHERE request_id=?",
            (status, error, time.time(), request_id),
        )
        self._c().commit()

    def mark_running(self, request_id: str) -> None:
        self.mark_status(request_id, "RUNNING")

    def increment_attempts(self, request_id: str) -> int:
        cur = self._c().execute(
            "UPDATE tasks SET attempts=attempts+1, updated_at=? WHERE request_id=?",
            (time.time(), request_id),
        )
        self._c().commit()
        row = self._c().execute(
            "SELECT attempts FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._c().execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def pending_tasks_older_than(self, seconds: float) -> list[dict[str, Any]]:
        threshold = time.time() - seconds
        rows = self._c().execute(
            "SELECT * FROM tasks WHERE status IN ('PENDING','FAILED') AND updated_at < ?",
            (threshold,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
