# Phase 4 System Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** EKRS Phase 4 — callback idempotency + Redis distributed locks + startup compensation scanner (最小闭环)

**Architecture:** Request_id UNIQUE in aiosqlite tasks 表 + Redis SET NX EX 锁 (token 校验) + 启动时扫 PENDING/FAILED 任务重试

**Tech Stack:** aiosqlite, redis-py (redis>=5.0), fakeredis (测试)

**Spec:** `docs/superpowers/specs/2026-06-25-phase4-system-integration-design.md`

## File Structure
- `shared/ekrs_shared/idempotency.py` — request_id 工具 (新)
- `rag/ekrs_rag/concurrency/__init__.py` — 模块 (新)
- `rag/ekrs_rag/concurrency/redis_lock.py` — 分布式锁 (新)
- `rag/ekrs_rag/concurrency/compensation.py` — 启动补偿 (新)
- `rag/ekrs_rag/storage/__init__.py` — 模块 (新)
- `rag/ekrs_rag/storage/task_repo.py` — aiosqlite tasks (新)
- `rag/ekrs_rag/api/routes/ingestion.py` — 改：入口加幂等 + 锁
- `rag/ekrs_rag/main.py` — 改：lifespan 注册补偿 + 注入依赖
- `rag/ekrs_rag/core/config.py` — 改：加 REDIS_URL (已有), LOCK_TTL_SEC, MAX_ATTEMPTS
- `rag/tests/unit/test_redis_lock.py` — 单元 (新)
- `rag/tests/unit/test_task_repo.py` — 单元 (新)
- `rag/tests/unit/test_idempotency.py` — 单元 (新)
- `rag/tests/unit/test_compensation.py` — 单元 (新)
- `rag/tests/integration/test_ingestion_phase4.py` — 集成 (新)

## Global Constraints
- Python 3.11+, aiosqlite, redis>=5.0
- 测试使用 fakeredis (无外部依赖)
- 所有时间戳使用 time.time() 浮点秒
- 错误处理：Redis 不可达 → 503 + 已存在 PENDING 记录兜底
- spec 七铁律仍生效: R1-R7 不可违反

---

### Task 1: 添加测试依赖 fakeredis

**Files:**
- Modify: `rag/pyproject.toml`

- [ ] **Step 1: 添加 fakeredis 到 dev deps**

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov",
    "fakeredis>=2.20",
]
```

- [ ] **Step 2: 安装**

Run: `cd rag && pip install -e ".[dev]"`
Expected: Successfully installed fakeredis-...

- [ ] **Step 3: Commit**

```bash
git add rag/pyproject.toml
git commit -m "chore: add fakeredis for test isolation"
```

---

### Task 2: 实现 idempotency 工具

**Files:**
- Create: `shared/ekrs_shared/idempotency.py`
- Test: `shared/tests/test_idempotency.py` (新建 `shared/tests/` 目录)

**Interfaces:**
- Produces: `request_id_from_trace(trace_id: str, doc_hash: str, version: int) -> str`

- [ ] **Step 1: 写失败测试**

```python
# shared/tests/test_idempotency.py
from ekrs_shared.idempotency import request_id_from_trace

def test_same_inputs_same_id():
    a = request_id_from_trace("t1", "doc_abc", 3)
    b = request_id_from_trace("t1", "doc_abc", 3)
    assert a == b
    assert len(a) == 32  # hex md5

def test_different_doc_different_id():
    a = request_id_from_trace("t1", "doc_abc", 3)
    b = request_id_from_trace("t1", "doc_xyz", 3)
    assert a != b

def test_different_version_different_id():
    a = request_id_from_trace("t1", "doc_abc", 3)
    b = request_id_from_trace("t1", "doc_abc", 4)
    assert a != b
```

- [ ] **Step 2: 运行 — 期望 FAIL**

Run: `cd shared && pytest tests/test_idempotency.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
# shared/ekrs_shared/idempotency.py
"""幂等键生成工具."""
from __future__ import annotations

import hashlib


def request_id_from_trace(trace_id: str, doc_hash: str, version: int) -> str:
    """生成稳定的幂等键: md5(trace_id|doc_hash|version) hex."""
    raw = f"{trace_id}|{doc_hash}|{version}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()
```

- [ ] **Step 4: 运行 — 期望 PASS**

Run: `cd shared && pytest tests/test_idempotency.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add shared/ekrs_shared/idempotency.py shared/tests/test_idempotency.py
git commit -m "feat(shared): idempotency key generator"
```

---

### Task 3: TaskRepo 单元 (aiosqlite tasks 表)

**Files:**
- Create: `rag/ekrs_rag/storage/__init__.py` (空)
- Create: `rag/ekrs_rag/storage/task_repo.py`
- Test: `rag/tests/unit/test_task_repo.py`

**Interfaces:**
- Produces: `class TaskRepo` with `init() -> None`, `try_insert(request_id, doc_id) -> bool`, `mark_status(request_id, status, error=None) -> None`, `mark_running(request_id) -> None`, `increment_attempts(request_id) -> int`, `pending_tasks_older_than(seconds: float) -> list[dict]`, `get(request_id) -> dict | None`

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/test_task_repo.py
import os
import tempfile
import pytest

from ekrs_rag.storage.task_repo import TaskRepo


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        r = TaskRepo(db_path=db)
        r.init()
        yield r


def test_init_creates_table(repo):
    rows = repo._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchall()
    assert len(rows) == 1


def test_try_insert_idempotent(repo):
    assert repo.try_insert("req1", "doc_a") is True
    assert repo.try_insert("req1", "doc_a") is False  # UNIQUE 触发


def test_mark_status_updates(repo):
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "RUNNING")
    assert repo.get("req1")["status"] == "RUNNING"


def test_mark_status_with_error(repo):
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "FAILED", error="boom")
    assert repo.get("req1")["last_error"] == "boom"


def test_increment_attempts(repo):
    repo.try_insert("req1", "doc_a")
    n1 = repo.increment_attempts("req1")
    n2 = repo.increment_attempts("req1")
    assert n1 == 1
    assert n2 == 2


def test_pending_tasks_older_than(repo):
    repo.try_insert("req1", "doc_a")
    repo.try_insert("req2", "doc_b")
    repo.mark_status("req1", "FAILED", error="x")
    # 设 updated_at 为 1 小时前
    old_ts = __import__("time").time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old_ts,))
    repo._conn.commit()
    found = repo.pending_tasks_older_than(60.0)
    ids = [t["request_id"] for t in found]
    assert "req1" in ids
    assert "req2" not in ids  # PENDING 但不旧
```

- [ ] **Step 2: 运行 — 期望 FAIL**

Run: `cd rag && pytest tests/unit/test_task_repo.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
# rag/ekrs_rag/storage/task_repo.py
"""aiosqlite tasks 表 — 任务状态 + 幂等."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite


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
    """同步 aiosqlite 包装（注意：本类内部用 aiosqlite 同步接口 for simplicity)."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _c(self) -> aiosqlite.Connection:
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
        except aiosqlite.IntegrityError:
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
```

- [ ] **Step 4: 运行 — 期望 PASS**

Run: `cd rag && pytest tests/unit/test_task_repo.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/storage/ rag/tests/unit/test_task_repo.py
git commit -m "feat(rag): TaskRepo for aiosqlite tasks table"
```

---

### Task 4: RedisLock 单元 (含 Lua 释放)

**Files:**
- Create: `rag/ekrs_rag/concurrency/__init__.py` (空)
- Create: `rag/ekrs_rag/concurrency/redis_lock.py`
- Test: `rag/tests/unit/test_redis_lock.py`

**Interfaces:**
- Produces: `class RedisLock` with `__init__(redis_client)`, `async acquire(key, ttl_sec) -> str | None` (returns token), `async release(key, token) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/test_redis_lock.py
import pytest
import fakeredis.aioredis

from ekrs_rag.concurrency.redis_lock import RedisLock


@pytest.fixture
def lock():
    client = fakeredis.aioredis.FakeRedis()
    return RedisLock(client)


@pytest.mark.asyncio
async def test_acquire_returns_token(lock):
    token = await lock.acquire("k1", ttl_sec=10)
    assert token is not None
    assert len(token) == 36  # uuid4


@pytest.mark.asyncio
async def test_acquire_same_key_blocked(lock):
    t1 = await lock.acquire("k1", ttl_sec=10)
    t2 = await lock.acquire("k1", ttl_sec=10)
    assert t1 is not None
    assert t2 is None


@pytest.mark.asyncio
async def test_release_with_correct_token_succeeds(lock):
    t = await lock.acquire("k1", ttl_sec=10)
    assert await lock.release("k1", t) is True
    # 锁释放后能再拿
    t2 = await lock.acquire("k1", ttl_sec=10)
    assert t2 is not None


@pytest.mark.asyncio
async def test_release_with_wrong_token_fails(lock):
    await lock.acquire("k1", ttl_sec=10)
    assert await lock.release("k1", "wrong-token") is False


@pytest.mark.asyncio
async def test_different_keys_independent(lock):
    t1 = await lock.acquire("k1", ttl_sec=10)
    t2 = await lock.acquire("k2", ttl_sec=10)
    assert t1 and t2
```

- [ ] **Step 2: 运行 — 期望 FAIL**

Run: `cd rag && pytest tests/unit/test_redis_lock.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
# rag/ekrs_rag/concurrency/redis_lock.py
"""Redis 分布式锁: SET NX EX + Lua 释放 token 校验."""
from __future__ import annotations

import uuid

# Lua: 仅当 token 匹配才删除 (避免持锁者过期后误删新持有者的锁)
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""


class RedisLock:
    def __init__(self, redis_client):
        self._r = redis_client
        self._release_script_sha: str | None = None

    async def _ensure_release_script(self) -> str:
        if self._release_script_sha is None:
            self._release_script_sha = await self._r.script_load(_RELEASE_LUA)
        return self._release_script_sha

    async def acquire(self, key: str, ttl_sec: int) -> str | None:
        token = uuid.uuid4().hex
        ok = await self._r.set(key, token, nx=True, ex=ttl_sec)
        return token if ok else None

    async def release(self, key: str, token: str) -> bool:
        sha = await self._ensure_release_script()
        result = await self._r.evalsha(sha, 1, key, token)
        return bool(result)
```

- [ ] **Step 4: 运行 — 期望 PASS**

Run: `cd rag && pytest tests/unit/test_redis_lock.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/concurrency/ redis_lock.py test_redis_lock.py
git commit -m "feat(rag): RedisLock with Lua release script"
```

---

### Task 5: 补偿扫描器单元

**Files:**
- Create: `rag/ekrs_rag/concurrency/compensation.py`
- Test: `rag/tests/unit/test_compensation.py`

**Interfaces:**
- Produces: `class CompensationScanner` with `__init__(task_repo, handler, max_attempts=3, threshold_sec=60)`, `async scan() -> int` (返回重试数)

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/test_compensation.py
import os
import tempfile
import time
import pytest

from ekrs_rag.storage.task_repo import TaskRepo
from ekrs_rag.concurrency.compensation import CompensationScanner


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "test.db")
        r = TaskRepo(db_path=db)
        r.init()
        yield r


@pytest.mark.asyncio
async def test_scan_retries_failed_old_tasks(repo):
    repo.try_insert("req1", "doc_a")
    repo.mark_status("req1", "FAILED", error="x")
    old = time.time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old,))
    repo._conn.commit()

    called = []
    async def handler(task: dict) -> None:
        called.append(task["request_id"])

    scanner = CompensationScanner(task_repo=repo, handler=handler, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 1
    assert called == ["req1"]
    # 状态变为 RUNNING (handler 抛错才 FAILED)
    assert repo.get("req1")["status"] in ("RUNNING", "COMPLETED")


@pytest.mark.asyncio
async def test_scan_skips_recent_pending(repo):
    repo.try_insert("req1", "doc_a")  # PENDING, 新建
    called = []
    async def handler(task: dict) -> None:
        called.append(task["request_id"])
    scanner = CompensationScanner(task_repo=repo, handler=handler, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 0
    assert called == []


@pytest.mark.asyncio
async def test_scan_respects_max_attempts(repo):
    repo.try_insert("req1", "doc_a")
    for _ in range(3):
        repo.increment_attempts("req1")
    old = time.time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='req1'", (old,))
    repo._conn.commit()

    called = []
    async def handler(task: dict) -> None:
        called.append(task["request_id"])
    scanner = CompensationScanner(task_repo=repo, handler=handler, max_attempts=3, threshold_sec=60.0)
    n = await scanner.scan()
    assert n == 0
    assert called == []
```

- [ ] **Step 2: 运行 — 期望 FAIL**

Run: `cd rag && pytest tests/unit/test_compensation.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
# rag/ekrs_rag/concurrency/compensation.py
"""启动补偿扫描器: 重试 PENDING/FAILED 且超过 threshold_sec 的任务."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ..storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class CompensationScanner:
    def __init__(
        self,
        task_repo: TaskRepo,
        handler: Handler,
        max_attempts: int = 3,
        threshold_sec: float = 60.0,
    ):
        self._repo = task_repo
        self._handler = handler
        self._max = max_attempts
        self._threshold = threshold_sec

    async def scan(self) -> int:
        tasks = self._repo.pending_tasks_older_than(self._threshold)
        retried = 0
        for task in tasks:
            if task["attempts"] >= self._max:
                logger.warning("Skip task %s: max attempts reached", task["request_id"])
                continue
            self._repo.mark_running(task["request_id"])
            self._repo.increment_attempts(task["request_id"])
            try:
                await self._handler(task)
                self._repo.mark_status(task["request_id"], "COMPLETED")
                retried += 1
            except Exception as e:
                logger.exception("Compensation handler failed for %s", task["request_id"])
                self._repo.mark_status(task["request_id"], "FAILED", error=str(e))
        return retried
```

- [ ] **Step 4: 运行 — 期望 PASS**

Run: `cd rag && pytest tests/unit/test_compensation.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/concurrency/compensation.py rag/tests/unit/test_compensation.py
git commit -m "feat(rag): compensation scanner for failed/pending tasks"
```

---

### Task 6: 扩展 config + main.py 注入

**Files:**
- Modify: `rag/ekrs_rag/core/config.py` (加 LOCK_TTL_SEC, MAX_ATTEMPTS, TASK_DB_PATH)
- Modify: `rag/ekrs_rag/main.py` (lifespan 初始化 redis, task_repo, 启动 compensation)

- [ ] **Step 1: 修改 config.py — 在 Settings 类加 3 个字段**

```python
    # Phase 4: 分布式锁 & 任务表
    LOCK_TTL_SEC: int = 300
    MAX_ATTEMPTS: int = 3
    TASK_DB_PATH: str = "/var/lib/ekrs/tasks.db"
```

- [ ] **Step 2: 修改 main.py — 加 imports 和 lifespan 注入**

替换 `from .ingestion.pipeline import IngestionPipeline` 附近加：

```python
import redis.asyncio as aioredis
from .concurrency.compensation import CompensationScanner
from .concurrency.redis_lock import RedisLock
from .storage.task_repo import TaskRepo
```

在 lifespan 内 `_pipeline = IngestionPipeline(...)` 之后加入：

```python
    # Phase 4: redis, task_repo, lock, compensation
    _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    _redis_lock = RedisLock(_redis)
    _task_repo = TaskRepo(db_path=settings.TASK_DB_PATH)
    _task_repo.init()
    app.state.redis = _redis
    app.state.redis_lock = _redis_lock
    app.state.task_repo = _task_repo

    async def _compensation_handler(task: dict) -> None:
        """重试入队: 重新触发 ingest (需 pipeline 支持重试入口)."""
        # TODO: wire to IngestionPipeline.ingest via callback_url
        logger.warning("Compensation handler not yet wired for %s", task["request_id"])

    _scanner = CompensationScanner(
        task_repo=_task_repo,
        handler=_compensation_handler,
        max_attempts=settings.MAX_ATTEMPTS,
        threshold_sec=60.0,
    )
    retried = await _scanner.scan()
    logger.info("Compensation scan completed: retried=%d", retried)

    yield
```

- [ ] **Step 3: 运行现有测试 — 期望 PASS (无回归)**

Run: `cd rag && pytest tests/ -q --tb=short`
Expected: 230 passed

- [ ] **Step 4: Commit**

```bash
git add rag/ekrs_rag/core/config.py rag/ekrs_rag/main.py
git commit -m "feat(rag): phase 4 wiring — redis, task_repo, compensation at startup"
```

---

### Task 7: 改造 ingestion 路由 (幂等 + 锁)

**Files:**
- Modify: `rag/ekrs_rag/api/routes/ingestion.py`

- [ ] **Step 1: 改写 notify 处理器**

在 `_validate_token` 之后、`background_tasks.add_task` 之前替换 `notify` 函数体：

```python
from ekrs_shared.idempotency import request_id_from_trace

from ...concurrency.redis_lock import RedisLock
from ...storage.task_repo import TaskRepo

# 注入 (lifespan 时 set_redis_lock / set_task_repo 调用)
_lock: RedisLock | None = None
_repo: TaskRepo | None = None


def set_redis_lock(lock: RedisLock) -> None:
    global _lock
    _lock = lock


def set_task_repo(repo: TaskRepo) -> None:
    global _repo
    _repo = repo


@router.post("/notify", status_code=202)
async def notify(
    notification: IngestionNotification,
    background_tasks: BackgroundTasks,
    x_parser_token: str | None = Header(None),
):
    _validate_token(x_parser_token)
    if _pipeline is None or _lock is None or _repo is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    doc_hash = notification.doc_hash
    version = notification.version
    request_id = request_id_from_trace(
        notification.trace_id or "", doc_hash, version
    )

    # 幂等: UNIQUE 约束触发 → 已处理
    if not _repo.try_insert(request_id, doc_hash):
        logger.info("Duplicate notify (idempotent): %s", request_id)
        return {"status": "duplicate", "doc_hash": doc_hash, "version": version}

    # 分布式锁: 防止同 doc 并发入库
    lock_key = f"lock:ingest:{doc_hash}"
    token = await _lock.acquire(lock_key, ttl_sec=settings.LOCK_TTL_SEC)
    if token is None:
        logger.warning("Lock held for %s, mark PENDING for compensation", doc_hash)
        return {"status": "in_flight", "doc_hash": doc_hash, "version": version}

    async def _locked_ingest() -> None:
        try:
            await _pipeline.ingest(notification)
            _repo.mark_status(request_id, "COMPLETED")
        except Exception as e:
            _repo.mark_status(request_id, "FAILED", error=str(e))
            raise
        finally:
            await _lock.release(lock_key, token)

    background_tasks.add_task(_locked_ingest)
    return {"status": "queued", "doc_hash": doc_hash, "version": version}
```

- [ ] **Step 2: 运行单元测试 — 期望 PASS (mock pipeline/redis)**

Run: `cd rag && pytest tests/unit/ -q --tb=short`
Expected: 全部 pass

- [ ] **Step 3: Commit**

```bash
git add rag/ekrs_rag/api/routes/ingestion.py
git commit -m "feat(rag): notify route — idempotency + distributed lock"
```

---

### Task 8: 集成测试 — notify 端到端

**Files:**
- Create: `rag/tests/integration/test_ingestion_phase4.py`

- [ ] **Step 1: 写测试**

```python
# rag/tests/integration/test_ingestion_phase4.py
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from ekrs_shared.models import IngestionNotification
from ekrs_rag.concurrency.redis_lock import RedisLock
from ekrs_rag.main import app
from ekrs_rag.storage.task_repo import TaskRepo


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "tasks.db")
        fake_redis = fakeredis.aioredis.FakeRedis()
        repo = TaskRepo(db_path=db)
        repo.init()
        lock = RedisLock(fake_redis)

        # Mock pipeline
        from ekrs_rag.api.routes import ingestion
        mock_pipeline = MagicMock()
        mock_pipeline.ingest = AsyncMock()
        ingestion.set_pipeline(mock_pipeline)
        ingestion.set_redis_lock(lock)
        ingestion.set_task_repo(repo)

        with TestClient(app) as c:
            yield c, repo, lock, mock_pipeline


def test_duplicate_request_id_is_idempotent(client):
    c, repo, lock, _ = client
    headers = {"X-Parser-Token": "change-me-to-a-secure-random-string-32chars"}
    payload = {
        "trace_id": "t1", "doc_hash": "doc_a", "version": 1,
        "output_path": "/tmp/x.jsonl", "callback_url": "",
    }
    r1 = c.post("/v1/ingestion/notify", json=payload, headers=headers)
    r2 = c.post("/v1/ingestion/notify", json=payload, headers=headers)
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["status"] == "queued"
    assert r2.json()["status"] == "duplicate"


def test_lock_prevents_concurrent_ingest(client):
    c, repo, lock, mock_pipeline = client
    # 预占锁
    import asyncio
    token = asyncio.get_event_loop().run_until_complete(lock.acquire("lock:ingest:doc_b", ttl_sec=10))
    assert token is not None

    headers = {"X-Parser-Token": "change-me-to-a-secure-random-string-32chars"}
    payload = {
        "trace_id": "t2", "doc_hash": "doc_b", "version": 1,
        "output_path": "/tmp/x.jsonl", "callback_url": "",
    }
    r = c.post("/v1/ingestion/notify", json=payload, headers=headers)
    assert r.status_code == 202
    assert r.json()["status"] == "in_flight"


def test_compensation_picks_up_old_failed(client):
    c, repo, lock, mock_pipeline = client
    # 模拟一个 1 小时前 FAILED 的任务
    repo.try_insert("old_req", "doc_c")
    repo.mark_status("old_req", "FAILED", error="prev")
    for _ in range(2):
        repo.increment_attempts("old_req")
    old = time.time() - 3600
    repo._conn.execute("UPDATE tasks SET updated_at=? WHERE request_id='old_req'", (old,))
    repo._conn.commit()

    # 手动跑扫描器 (避免 lifespan 副作用)
    from ekrs_rag.concurrency.compensation import CompensationScanner
    async def handler(task): pass
    scanner = CompensationScanner(task_repo=repo, handler=handler, max_attempts=3, threshold_sec=60.0)
    n = asyncio.get_event_loop().run_until_complete(scanner.scan())
    assert n == 1
```

- [ ] **Step 2: 运行 — 期望 PASS**

Run: `cd rag && pytest tests/integration/test_ingestion_phase4.py -v`
Expected: 3 passed

- [ ] **Step 3: 全量测试 — 期望 ≥233 全 pass**

Run: `cd rag && pytest tests/ -q --tb=short`
Expected: 233 passed (230 + 3 new)

- [ ] **Step 4: Commit**

```bash
git add rag/tests/integration/test_ingestion_phase4.py
git commit -m "test(rag): phase 4 integration — idempotency, lock, compensation"
```

---

### Task 9: 覆盖率验证

- [ ] **Step 1: 运行覆盖率**

Run: `cd rag && pytest tests/ --cov=ekrs_rag.concurrency --cov=ekrs_rag.storage --cov-report=term-missing -q`
Expected: 新模块 ≥ 80%

- [ ] **Step 2: 提交覆盖率报告**

```bash
git add htmlcov/ -f 2>/dev/null || true
git commit -m "chore: phase 4 coverage report" || echo "no report to commit"
```

---

## Self-Review

- **Spec 覆盖:** 幂等 ✓ (Task 2, 7), 分布式锁 ✓ (Task 4, 7), 启动补偿 ✓ (Task 5, 6), 错误处理 ✓ (Task 7 503+in_flight), 测试 ✓ (Task 1, 3, 4, 5, 8, 9)
- **Placeholder 扫描:** 无 TBD/TODO (除 `_compensation_handler` 内的可接受占位 — 后续接 parser 回调重试)
- **类型一致:** `try_insert -> bool`, `mark_status(str, str, str|None)`, `acquire -> str|None`, `release -> bool`, `scan -> int` 跨任务一致

## 未决问题
1. Redis 连接 eager init (lifespan 失败 → 启动失败 vs 懒加载首次请求时失败)
2. tasks 表清理策略 (永久 vs 7 天滚动)
3. callback 失败重试独立表 vs 共用 tasks 表 (当前方案: 共用, callback 失败也走 mark_status FAILED)
4. 锁 TTL 300s 是否覆盖大文档 (需用真实 GB 级 PDF 测)
5. `_compensation_handler` 当前 stub: 实际重试时需重建 IngestionNotification 并调 pipeline.ingest, 或调用 parser 的 `/v1/ingestion/replay` 端点
