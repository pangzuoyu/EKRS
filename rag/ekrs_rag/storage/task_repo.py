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
        # check_same_thread=False: SQLite is shared across FastAPI worker threads
        # (route handlers and BackgroundTasks). SQLite serializes writes via its
        # own file lock. Concurrent reads may be inconsistent, which is
        # acceptable for an idempotency/status table that is rarely read.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
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

    def claim_for_retry(
        self,
        request_id: str,
        max_attempts: int,
        threshold_sec: float,
    ) -> bool:
        """原子地将任务置为 RUNNING 并增加 attempts, 不更新 updated_at.

        SQL 内同时校验 status / attempts / updated_at, 杜绝两个并发 scan 拿到
        同一行都进入 handler 路径 (双 claim). 返回 rowcount>0 表示成功, 调用方
        拿到 False 应当当作 "输掉竞争 / 行已不在窗口内" 而跳过该任务.

        不更新 updated_at 的关键原因: 失败时 mark_failed_with_error 也不会更新
        updated_at, 这样下次 scan() 通过 updated_at < now - threshold 仍能挑出
        该任务, 真正实现 max_attempts 次重试. 一次 SQL = 一次事务, 避免
        mark_running 与 increment_attempts 之间的崩溃导致任务永久 RUNNING.
        """
        threshold = time.time() - threshold_sec
        cur = self._c().execute(
            "UPDATE tasks SET status='RUNNING', attempts=attempts+1 "
            "WHERE request_id=? "
            "AND status IN ('PENDING','FAILED') "
            "AND attempts < ? "
            "AND updated_at < ?",
            (request_id, max_attempts, threshold),
        )
        self._c().commit()
        return cur.rowcount > 0

    def mark_failed_with_error(self, request_id: str, error: str) -> None:
        """记录失败, 追加错误到 last_error (保留历史), 不刷新 updated_at.

        updated_at 不刷新, 是为了让 pending_tasks_older_than 的窗口过滤
        能继续命中此任务以触发重试. 不会覆盖已有 last_error, 用 " | " 拼接.
        """
        row = self._c().execute(
            "SELECT last_error FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        prior = row["last_error"] if row and row["last_error"] else ""
        merged = f"{prior} | {error}" if prior else error
        self._c().execute(
            "UPDATE tasks SET status='FAILED', last_error=? WHERE request_id=?",
            (merged, request_id),
        )
        self._c().commit()

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
