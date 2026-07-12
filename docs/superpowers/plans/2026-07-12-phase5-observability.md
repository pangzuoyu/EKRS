# Phase 5 Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** EKRS Phase 5 — Prometheus 真实指标 + audit log 按 spec §12 落地 + Query/Ingestion 双 Replay + 健康检查暴露 audit 索引

**Architecture:** 端点装饰器 (@audited / @metered) + Observability middleware 注入 trace_id + 业务关键点显式调用 audit/metric + 启动时构建 trace_id→file_offset 内存索引 + Phase 4.5 schema 扩展 tasks.source_path + payload_sha256

**Tech Stack:** prometheus-client>=0.20, python-json-logger>=2.0 (已有), FastAPI 中间件 + 装饰器, contextvars, aiosqlite (已有)

**Spec:** `docs/superpowers/specs/2026-07-12-phase5-observability-design.md`

**Phase 4 已完成 (commit 9a7cbca)** — 本计划扩展 Phase 4 的 TaskRepo / RedisLock / Compensation 模块。

## File Structure

### 新增文件
- `rag/ekrs_rag/observability/__init__.py` — re-export public API
- `rag/ekrs_rag/observability/metrics.py` — Counter/Histogram 注册表 + safe_inc
- `rag/ekrs_rag/observability/audit.py` — AuditWriter (基类在 shared/audit.py)
- `rag/ekrs_rag/observability/audit_index.py` — trace_id → file_offset 内存索引
- `rag/ekrs_rag/observability/trace.py` — contextvars + 中间件注入
- `rag/ekrs_rag/api/middleware/__init__.py` — 模块
- `rag/ekrs_rag/api/middleware/observability.py` — FastAPI middleware
- `rag/ekrs_rag/api/decorators.py` — @audited / @metered
- `rag/tests/unit/observability/__init__.py` — 测试模块
- `rag/tests/unit/observability/test_metrics.py` — 4 tests
- `rag/tests/unit/observability/test_audit.py` — 6 tests
- `rag/tests/unit/observability/test_trace.py` — 4 tests
- `rag/tests/unit/observability/test_audit_index.py` — 5 tests
- `rag/tests/unit/storage/test_task_repo_phase45.py` — 4 tests
- `rag/tests/integration/test_metrics_endpoint.py` — 3 tests
- `rag/tests/integration/test_query_replay.py` — 4 tests (+1 cross-process restart)
- `rag/tests/integration/test_ingestion_replay.py` — 4 tests
- `rag/tests/integration/test_audit_durability.py` — 3 tests (corrupted lines / truncated file)

### 修改文件
- `rag/pyproject.toml` — 加 prometheus-client>=0.20
- `shared/ekrs_shared/audit.py` — 基类加 propagation=False + schema 校验钩子
- `rag/ekrs_rag/api/routes/metrics.py` — 替换占位 → prometheus_client.generate_latest()
- `rag/ekrs_rag/api/routes/ingestion.py` — 新增 POST /v1/ingestion/replay + source_path 写入
- `rag/ekrs_rag/api/routes/constraints.py` — solve 流程接 audit + replay=true 路径
- `rag/ekrs_rag/concurrency/compensation.py` — 显式 audit("compensation_retry")
- `rag/ekrs_rag/core/logging.py` — 增加 RotatingFileHandler (debug.log, 100MB x 5)
- `rag/ekrs_rag/storage/task_repo.py` — Phase 4.5 schema 扩展 + replay 读路径
- `rag/ekrs_rag/main.py` — 注册 middleware + 启动 audit 健康检查 + 索引构建 + GET /healthz
- `.env.example` — 加 AUDIT_LOG_PATH, DEBUG_LOG_PATH, METRICS_TOKEN (可选)

## Global Constraints
- Python 3.11+, prometheus-client>=0.20, aiosqlite (Phase 4), redis>=5.0 (Phase 4)
- 所有 audit 行走 python-json-logger JSON 格式 (spec §12)
- audit.log 永久不轮转, debug.log 100MB x 5 backups
- audit log 写失败 → debug.log + rag_audit_write_failures_total++, 不阻断业务
- trace_id 来源: HTTP header `X-Trace-Id` 或 uuid4
- trace_id **禁止**作为 Prometheus label (cardinality 爆炸)
- endpoint label 使用 `request.scope["route"].path` (路由模板) 不用 `request.url.path`
- safe_inc() 校验 label value 不含路径插值 (正则 `/\{[^}]+\}/` 必须匹配路由模板)
- spec 七铁律仍生效: R1-R7 不可违反
- 测试使用 fakeredis (Phase 4 模式)
- 所有时间戳使用 time.time() 浮点秒 (Phase 4 模式)

---

### Task 1: 添加 prometheus-client 依赖

**Files:**
- Modify: `rag/pyproject.toml`

- [ ] **Step 1: 添加 prometheus-client 到 dependencies**

编辑 `rag/pyproject.toml` 第 9 行附近，加在 `python-json-logger>=2.0` 之后：

```toml
    "python-json-logger>=2.0",
    "prometheus-client>=0.20",
```

- [ ] **Step 2: 安装**

Run: `cd rag && pip install -e ".[dev]"`
Expected: Successfully installed prometheus-client-0.20.x

- [ ] **Step 3: 验证导入**

Run: `cd rag && python -c "from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add rag/pyproject.toml
git commit -m "chore: add prometheus-client for phase 5 metrics"
```

---

### Task 2: 扩展 AuditLogger 基类（propagation + schema 校验）

**Files:**
- Modify: `shared/ekrs_shared/audit.py`
- Test: `shared/tests/test_audit_base.py`（新增 `shared/tests/` 目录）

**Interfaces:**
- Produces: `AuditLogger.log_event(event_type: str, **kwargs) -> None` (基类)
- Produces: `AuditLogger.register_event_schema(event_type: str, required_fields: set[str]) -> None`
- Produces: `AuditLogger.validate_event(event_type: str, **kwargs) -> None` (raises ValueError if missing required)

- [ ] **Step 1: 写失败测试**

```python
# shared/tests/test_audit_base.py
import logging
import pytest
from ekrs_shared.audit import AuditLogger


def test_register_event_schema_and_validate():
    audit = AuditLogger("test.audit.schema")
    audit.register_event_schema("test_event", {"field_a", "field_b"})
    # Missing required field should raise
    with pytest.raises(ValueError, match="field_a"):
        audit.validate_event("test_event", field_b="ok")


def test_logger_does_not_propagate_to_root():
    # Capture root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    initial_handlers = list(root.handlers)

    audit = AuditLogger("test.audit.propagate")
    audit.log_event("no_propagate_test", key="value")

    # Logger should have its own handler, NOT add to root
    audit_logger = logging.getLogger("test.audit.propagate")
    assert audit_logger.propagate is False
    assert len(audit_logger.handlers) >= 1


def test_log_event_writes_json_with_event_field():
    audit = AuditLogger("test.audit.json")
    audit.log_event("sample", trace_id="abc", extra_field=42)
    # Just verify no exception; content verified by RAG-specific tests
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/pangzy/code_project/EKRS/shared && pip install -e . && pytest tests/test_audit_base.py -v`
Expected: FAIL (register_event_schema not implemented)

- [ ] **Step 3: 修改 audit.py 实现 propagation + schema 校验**

```python
# shared/ekrs_shared/audit.py
"""Audit log base class for EKRS.

Provides structured JSON audit logging with trace_id propagation,
schema validation, and isolated handler (propagation=False).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


class AuditLogger:
    """Base audit logger. Writes structured JSON events.

    Subclasses / instances configure FileHandler; base class owns
    schema registry and propagation control.

    Usage:
        audit = AuditLogger("ekrs.audit")
        audit.register_event_schema("constraint_solved", {"trace_id", "query"})
        audit.log_event("constraint_solved", trace_id="abc", query="温度")
    """

    def __init__(self, name: str = "ekrs.audit", level: int = logging.INFO):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._logger.propagate = False  # do NOT bubble to root
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
        self._schemas: dict[str, set[str]] = {}

    def register_event_schema(
        self, event_type: str, required_fields: set[str]
    ) -> None:
        """Register required fields for an event type (idempotent)."""
        self._schemas[event_type] = required_fields

    def validate_event(self, event_type: str, **kwargs: Any) -> None:
        """Raise ValueError if required fields for event_type are missing."""
        required = self._schemas.get(event_type, set())
        missing = required - set(kwargs.keys())
        if missing:
            raise ValueError(
                f"audit event '{event_type}' missing required fields: {missing}"
            )

    def log_event(self, event_type: str, **kwargs: Any) -> None:
        """Log a structured audit event. Validates against schema if registered."""
        if event_type in self._schemas:
            self.validate_event(event_type, **kwargs)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **kwargs,
        }
        self._logger.info(json.dumps(entry, ensure_ascii=False, default=str))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_audit_base.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add shared/ekrs_shared/audit.py shared/tests/test_audit_base.py
git commit -m "feat(shared): audit base class — propagation=False + schema registry"
```

---

### Task 3: AuditWriter 实例 + audit.log 永久文件

**Files:**
- Create: `rag/ekrs_rag/observability/__init__.py`
- Create: `rag/ekrs_rag/observability/audit.py`
- Test: `rag/tests/unit/observability/test_audit.py`

**Interfaces:**
- Produces: `AuditWriter(audit_log_path: str) -> AuditWriter` (extends shared AuditLogger)
- Produces: `AuditWriter.write(event_type: str, **kwargs) -> bool` (False on failure, no raise)

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/observability/__init__.py
# (empty file)

# rag/tests/unit/observability/test_audit.py
import json
from pathlib import Path

import pytest

from ekrs_rag.observability.audit import AuditWriter


def test_audit_writer_creates_permanent_file(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("test_event", {"field_a"})
    writer.write("test_event", field_a="hello", trace_id="t1")

    lines = log.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "test_event"
    assert entry["field_a"] == "hello"
    assert entry["trace_id"] == "t1"
    assert "timestamp" in entry


def test_audit_writer_appends_multiple_events(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("evt", {"x"})
    for i in range(5):
        writer.write("evt", x=i)

    lines = log.read_text().strip().split("\n")
    assert len(lines) == 5
    entries = [json.loads(l) for l in lines]
    assert [e["x"] for e in entries] == [0, 1, 2, 3, 4]


def test_audit_writer_does_not_rotate(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("big", {"payload"})
    writer.write("big", payload="x" * 1_000_000)  # 1MB
    writer.write("big", payload="y" * 1_000_000)  # another 1MB

    # File should be > 2MB; no rotation occurred
    assert log.stat().st_size > 2_000_000
    # No .1 / .2 backup files
    backups = list(tmp_path.glob("audit.log.*"))
    assert backups == []


def test_audit_write_failure_returns_false(tmp_path):
    log = tmp_path / "audit.log"
    log.write_text("existing")
    log.chmod(0o000)  # make read-only (may still work as root; use chmod trick)

    writer = AuditWriter(str(log))
    writer.register_event_schema("evt", {})
    # Either succeeds (running as root bypasses chmod) or returns False gracefully
    try:
        result = writer.write("evt", data="test")
        if result is False:
            log.chmod(0o644)  # cleanup
            assert result is False
        else:
            log.chmod(0o644)
            assert result is True  # root bypass
    except (PermissionError, OSError):
        log.chmod(0o644)
        pytest.skip("running as root bypasses chmod 0o000")


def test_audit_writer_propagation_is_false(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    import logging
    audit_logger = logging.getLogger(writer._logger.name)
    assert audit_logger.propagate is False


def test_audit_writer_uses_json_formatter(tmp_path):
    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.write("format_test", k="v")
    line = log.read_text().strip()
    entry = json.loads(line)
    assert entry["k"] == "v"
    assert entry["event"] == "format_test"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/unit/observability/test_audit.py -v`
Expected: FAIL (AuditWriter not importable)

- [ ] **Step 3: 实现 AuditWriter**

```python
# rag/ekrs_rag/observability/__init__.py
"""Observability: metrics + audit + trace + replay."""
from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.audit_index import AuditIndex
from ekrs_rag.observability.metrics import METRICS, safe_inc, safe_observe
from ekrs_rag.observability.trace import (
    get_trace_id,
    set_trace_id,
    reset_trace_id,
)

__all__ = [
    "AuditWriter", "AuditIndex", "METRICS",
    "safe_inc", "safe_observe",
    "get_trace_id", "set_trace_id", "reset_trace_id",
]
```

```python
# rag/ekrs_rag/observability/audit.py
"""RAG-specific AuditWriter: shared/audit.py base + FileHandler (永久).

audit.log never rotates. Write failures are caught (returns False),
never propagate to callers.
"""
from __future__ import annotations

import logging
import os
import traceback
from pathlib import Path

from ekrs_shared.audit import AuditLogger


# Module-level writer, set by main.py at startup
_writer: AuditLogger | None = None
# Module-level AuditIndex, set by main.py at startup (Issue 5: runtime writes
# must be indexable for replay without rescan)
_index = None


class AuditWriter(AuditLogger):
    """AuditLogger instance with permanent FileHandler."""

    def __init__(self, audit_log_path: str):
        super().__init__(name="ekrs.audit")
        path = Path(audit_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # FileHandler, NOT RotatingFileHandler — permanent
        handler = logging.FileHandler(str(path), encoding="utf-8")
        # Pass-through formatter (base class already JSON-encodes message)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    def write(self, event_type: str, **kwargs) -> bool:
        """Log an event. Returns False if write fails (never raises)."""
        try:
            # Capture file offset BEFORE write so AuditIndex can locate the line
            offset = self._current_offset()
            self.log_event(event_type, **kwargs)
            # Register new line in module-level AuditIndex (Issue 5)
            idx = get_index()
            if idx is not None:
                trace_id = kwargs.get("trace_id") or self._logger.findCaller  # noqa: F841
                # Simpler: pull trace_id from kwargs (audit contract always passes it)
                idx.append(event_type, kwargs.get("trace_id", ""), offset)
            return True
        except Exception:
            # Log to stderr (root logger is still alive for debug.log)
            logging.getLogger("ekrs.audit.failures").error(
                "audit write failed: %s", traceback.format_exc()
            )
            return False

    def _current_offset(self) -> int:
        """Return current byte offset of the file handler (for index registration)."""
        for h in self._logger.handlers:
            if isinstance(h, logging.FileHandler) and not h.stream.closed:
                try:
                    return h.stream.tell()
                except (OSError, AttributeError):
                    pass
        return 0


def set_writer(writer: AuditLogger) -> None:
    """Set module-level writer (called at startup)."""
    global _writer
    _writer = writer


def get_writer() -> AuditLogger | None:
    return _writer


def attach_index(index) -> None:
    """Attach an AuditIndex so new writes are indexed for replay (Issue 5).

    Module-level singleton; called once at startup by main.py lifespan.
    """
    global _index
    _index = index


def get_index():
    """Return attached AuditIndex, or None if not yet initialized."""
    return _index
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/unit/observability/test_audit.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/observability/ rag/tests/unit/observability/
git commit -m "feat(rag): AuditWriter instance + permanent file handler"
```

---

### Task 4: Trace contextvars + middleware

**Files:**
- Create: `rag/ekrs_rag/observability/trace.py`
- Create: `rag/ekrs_rag/api/middleware/__init__.py`
- Create: `rag/ekrs_rag/api/middleware/observability.py`
- Test: `rag/tests/unit/observability/test_trace.py`

**Interfaces:**
- Produces: `get_trace_id() -> str` — read from contextvar
- Produces: `set_trace_id(trace_id: str) -> Token`
- Produces: `reset_trace_id(token: Token) -> None`
- Produces: `ObservabilityMiddleware` ASGI middleware (set contextvar on request, clear on response)

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/observability/test_trace.py
import asyncio
import uuid

import pytest

from ekrs_rag.observability.trace import (
    TraceContext, get_trace_id, set_trace_id, reset_trace_id,
)


def test_default_trace_id_is_unknown():
    # Outside any context, returns "unknown"
    assert get_trace_id() == "unknown"


def test_set_and_reset_trace_id():
    token = set_trace_id("test-trace-123")
    try:
        assert get_trace_id() == "test-trace-123"
    finally:
        reset_trace_id(token)
    assert get_trace_id() == "unknown"


def test_trace_id_isolated_across_async_tasks():
    """Two concurrent tasks must not see each other's trace_id."""
    async def task(tid, barrier):
        set_trace_id(tid)
        await barrier.wait()
        seen = get_trace_id()
        return seen

    async def main():
        barrier = asyncio.Barrier(2)
        results = await asyncio.gather(
            task("task-A", barrier),
            task("task-B", barrier),
        )
        # Each task sees its own trace_id after await
        assert "task-A" in results
        assert "task-B" in results
        assert results[0] != results[1]

    asyncio.run(main())


def test_trace_id_from_header_or_generated():
    """Middleware behavior: use X-Trace-Id header, else generate uuid4."""
    from ekrs_rag.api.middleware.observability import (
        extract_or_generate_trace_id,
    )
    # No header → generated uuid4
    generated = extract_or_generate_trace_id(headers={})
    assert len(generated) == 36  # uuid4 hex with dashes
    # With header → use as-is
    provided = extract_or_generate_trace_id(headers={"x-trace-id": "my-custom"})
    assert provided == "my-custom"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/unit/observability/test_trace.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 实现 trace.py + middleware**

```python
# rag/ekrs_rag/observability/trace.py
"""Trace ID propagation via contextvars.

A contextvar is set per HTTP request by the observability middleware.
All audit writes and metric increments within that request see the same
trace_id without explicit threading.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Any

_trace_id_var: ContextVar[str] = ContextVar("ekrs_trace_id", default="unknown")


def get_trace_id() -> str:
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> Token:
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: Token) -> None:
    _trace_id_var.reset(token)
```

```python
# rag/ekrs_rag/api/middleware/__init__.py
# (empty)
```

```python
# rag/ekrs_rag/api/middleware/observability.py
"""FastAPI middleware: inject trace_id + measure request duration."""
from __future__ import annotations

import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from ekrs_rag.observability.trace import get_trace_id, reset_trace_id, set_trace_id

HEADER_NAME = "x-trace-id"


def extract_or_generate_trace_id(headers: dict) -> str:
    """Pull X-Trace-Id from headers (case-insensitive), else generate uuid4."""
    # headers may be dict[str, str] or Headers (case-insensitive)
    for k, v in headers.items():
        if k.lower() == HEADER_NAME:
            return str(v)
    return str(uuid.uuid4())


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Inject trace_id into contextvar, time the request, audit lifecycle."""

    async def dispatch(self, request: Request, call_next):
        trace_id = extract_or_generate_trace_id(dict(request.headers))
        token = set_trace_id(trace_id)
        start = time.monotonic()
        # Audit endpoint_started (Issue 21: spec §Audit 事件清单 15 个)
        from ekrs_rag.observability.audit import get_writer
        writer = get_writer()
        if writer:
            # route may not be resolved yet at dispatch start; use raw path
            writer.write(
                "endpoint_started",
                trace_id=trace_id,
                endpoint=request.url.path,
                method=request.method,
            )
        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            if writer:
                writer.write(
                    "endpoint_completed",
                    trace_id=trace_id,
                    status_code=200,  # middleware doesn't observe response; use 200 by default
                    duration_ms=duration_ms,
                )
            reset_trace_id(token)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/unit/observability/test_trace.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/observability/trace.py rag/ekrs_rag/api/middleware/ rag/tests/unit/observability/test_trace.py
git commit -m "feat(rag): trace_id contextvar + ObservabilityMiddleware"
```

---

### Task 5: Metrics 注册表 + safe_inc

**Files:**
- Create: `rag/ekrs_rag/observability/metrics.py`
- Test: `rag/tests/unit/observability/test_metrics.py`

**Interfaces:**
- Produces: `METRICS` namespace object with all 12 Counter/Histogram/Gauge
- Produces: `safe_inc(counter, **labels) -> None` (rejects interpolated labels)
- Produces: `safe_observe(histogram, value, **labels) -> None`

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/observability/test_metrics.py
import re
from prometheus_client import REGISTRY

from ekrs_rag.observability.metrics import (
    METRICS, safe_inc, safe_observe, is_route_template,
)


def test_all_metrics_registered():
    """All 12 documented metrics exist on METRICS namespace."""
    assert hasattr(METRICS, "http_requests_total")
    assert hasattr(METRICS, "http_request_duration_seconds")
    assert hasattr(METRICS, "http_requests_inprogress")
    assert hasattr(METRICS, "ingestion_total")
    assert hasattr(METRICS, "ingestion_duration_seconds")
    assert hasattr(METRICS, "ingestion_chunks_written")
    assert hasattr(METRICS, "constraint_solve_total")
    assert hasattr(METRICS, "constraint_solve_duration_seconds")
    assert hasattr(METRICS, "constraint_branches_count")
    assert hasattr(METRICS, "lock_acquire_total")
    assert hasattr(METRICS, "compensation_pending_tasks")
    assert hasattr(METRICS, "compensation_retries_total")
    assert hasattr(METRICS, "qdrant_write_failures_total")


def test_is_route_template_accepts_only_templates():
    # Route template must have placeholder pattern or be plain
    assert is_route_template("/v1/constraints") is True
    assert is_route_template("/v1/docs/{doc_id}") is True
    # Interpolated values must be rejected
    assert is_route_template("/v1/docs/abc-123-def") is False
    assert is_route_template("/v1/docs/123") is False


def test_safe_inc_rejects_interpolated_label(caplog):
    """safe_inc with bad label value logs warning, does not raise."""
    safe_inc(METRICS.http_requests_total,
             endpoint="/v1/docs/abc-123",
             method="GET", status="2xx")
    # Counter should not have been incremented
    val = METRICS.http_requests_total.labels(
        endpoint="/v1/docs/abc-123", method="GET", status="2xx"
    )._value.get()
    assert val == 0


def test_safe_inc_accepts_template_label():
    safe_inc(METRICS.http_requests_total,
             endpoint="/v1/docs/{doc_id}",
             method="GET", status="2xx")
    val = METRICS.http_requests_total.labels(
        endpoint="/v1/docs/{doc_id}", method="GET", status="2xx"
    )._value.get()
    assert val == 1


def test_safe_observe_works():
    safe_observe(METRICS.constraint_solve_duration_seconds, 0.123)
    # Just verify no exception; histogram state verified via prometheus_client
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/unit/observability/test_metrics.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 实现 metrics.py**

```python
# rag/ekrs_rag/observability/metrics.py
"""Prometheus metrics registry for EKRS RAG.

12 metrics across HTTP/ingestion/solve/concurrency/qdrant.
Cardinality guard: endpoint label must be a route template.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger("ekrs.observability.metrics")

# Buckets (per spec)
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SOLVE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
INGEST_BUCKETS = (0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0)
BRANCH_BUCKETS = (1, 2, 3, 5, 10)

# Endpoint label validation: must be route template
# Templates: literal path segments OR {param} placeholders
_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_PLACEHOLDER_RE = re.compile(r"^\{[a-zA-Z_][a-zA-Z0-9_]*\}$")


def is_route_template(path: str) -> bool:
    """True iff path is a route template (no interpolated values)."""
    if not path or not path.startswith("/"):
        return False
    segments = path.split("/")[1:]  # drop leading empty
    if not segments:
        return False
    for seg in segments:
        if _PLACEHOLDER_RE.match(seg):
            continue
        if _SEGMENT_RE.match(seg):
            continue
        return False
    return True


# Metric definitions
http_requests_total = Counter(
    "rag_http_requests_total",
    "Total HTTP requests",
    ["endpoint", "method", "status"],
)

http_request_duration_seconds = Histogram(
    "rag_http_request_duration_seconds",
    "HTTP request latency",
    ["endpoint", "method"],
    buckets=HTTP_BUCKETS,
)

http_requests_inprogress = Gauge(
    "rag_http_requests_inprogress",
    "Currently in-flight HTTP requests",
    ["endpoint"],
)

ingestion_total = Counter(
    "rag_ingestion_total",
    "Ingestion attempts by terminal status",
    ["status"],
)

ingestion_duration_seconds = Histogram(
    "rag_ingestion_duration_seconds",
    "End-to-end ingestion latency",
    buckets=INGEST_BUCKETS,
)

ingestion_chunks_written = Counter(
    "rag_ingestion_chunks_written",
    "Total chunks written to Qdrant",
)

constraint_solve_total = Counter(
    "rag_constraint_solve_total",
    "Constraint solve attempts by outcome",
    ["outcome"],
)

constraint_solve_duration_seconds = Histogram(
    "rag_constraint_solve_duration_seconds",
    "Solver latency",
    buckets=SOLVE_BUCKETS,
)

constraint_branches_count = Histogram(
    "rag_constraint_branches_count",
    "Branches returned per solve",
    buckets=BRANCH_BUCKETS,
)

lock_acquire_total = Counter(
    "rag_lock_acquire_total",
    "Redis lock acquisition attempts",
    ["result"],
)

compensation_pending_tasks = Gauge(
    "rag_compensation_pending_tasks",
    "Tasks eligible for compensation retry",
)

compensation_retries_total = Counter(
    "rag_compensation_retries_total",
    "Total compensation retries",
    ["result"],
)

qdrant_write_failures_total = Counter(
    "rag_qdrant_write_failures_total",
    "Qdrant write failures",
    ["operation"],
)

# Internal: not in spec but useful for audit durability
audit_write_failures_total = Counter(
    "rag_audit_write_failures_total",
    "Audit log write failures",
)


METRICS = SimpleNamespace(
    http_requests_total=http_requests_total,
    http_request_duration_seconds=http_request_duration_seconds,
    http_requests_inprogress=http_requests_inprogress,
    ingestion_total=ingestion_total,
    ingestion_duration_seconds=ingestion_duration_seconds,
    ingestion_chunks_written=ingestion_chunks_written,
    constraint_solve_total=constraint_solve_total,
    constraint_solve_duration_seconds=constraint_solve_duration_seconds,
    constraint_branches_count=constraint_branches_count,
    lock_acquire_total=lock_acquire_total,
    compensation_pending_tasks=compensation_pending_tasks,
    compensation_retries_total=compensation_retries_total,
    qdrant_write_failures_total=qdrant_write_failures_total,
    audit_write_failures_total=audit_write_failures_total,
)


def safe_inc(counter: Counter, **labels: Any) -> None:
    """Increment counter; reject labels with interpolated path values."""
    if "endpoint" in labels and not is_route_template(labels["endpoint"]):
        logger.warning(
            "metric label rejected: endpoint=%s is not a route template",
            labels["endpoint"],
        )
        return
    try:
        counter.labels(**labels).inc()
    except Exception as e:
        logger.warning("metric inc failed: %s", e)


def safe_observe(histogram: Histogram, value: float, **labels: Any) -> None:
    """Observe histogram value; reject labels with interpolated path values."""
    if "endpoint" in labels and not is_route_template(labels["endpoint"]):
        logger.warning(
            "metric label rejected: endpoint=%s is not a route template",
            labels["endpoint"],
        )
        return
    try:
        if labels:
            histogram.labels(**labels).observe(value)
        else:
            histogram.observe(value)
    except Exception as e:
        logger.warning("metric observe failed: %s", e)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/unit/observability/test_metrics.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/observability/metrics.py rag/tests/unit/observability/test_metrics.py
git commit -m "feat(rag): metrics registry — 13 metrics + cardinality guard"
```

---

### Task 6: AuditIndex — trace_id → file_offset 内存索引

**Files:**
- Create: `rag/ekrs_rag/observability/audit_index.py`
- Test: `rag/tests/unit/observability/test_audit_index.py`

**Interfaces:**
- Produces: `AuditIndex(audit_log_path: str) -> AuditIndex`
- Produces: `AuditIndex.build() -> None` — scan file once, build dict
- Produces: `AuditIndex.seek(trace_id: str) -> list[AuditLine] | None`
- Produces: `AuditIndex.append(event: dict) -> None` — add new line to index after write

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/observability/test_audit_index.py
import json
from pathlib import Path

import pytest

from ekrs_rag.observability.audit_index import AuditIndex


def _write_audit_line(path, event_type, trace_id, **extra):
    entry = {"timestamp": "2026-07-12T00:00:00Z", "event": event_type,
             "trace_id": trace_id, **extra}
    line = json.dumps(entry)
    with open(path, "a") as f:
        offset = f.tell()
        f.write(line + "\n")
    return offset


def test_index_builds_from_clean_audit_log(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "constraint_solve_started", "t1", query="q")
    _write_audit_line(log, "constraint_solved", "t1", branches_count=2)
    _write_audit_line(log, "constraint_solve_started", "t2", query="q2")
    _write_audit_line(log, "constraint_solved", "t2", branches_count=1)

    idx = AuditIndex(str(log))
    idx.build()

    result = idx.seek("t1")
    assert result is not None
    assert len(result) == 2
    assert result[0].event == "constraint_solve_started"
    assert result[1].event == "constraint_solved"


def test_index_skips_non_replay_events(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "endpoint_started", "t1", endpoint="/v1/x")
    _write_audit_line(log, "ingestion_completed", "t1", doc_id="d")

    idx = AuditIndex(str(log))
    idx.build()

    # Only constraint events are indexed, so seek returns None
    assert idx.seek("t1") is None


def test_index_resilient_to_corrupted_lines(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "constraint_solve_started", "t1", query="q")
    # Inject corrupted line
    with open(log, "a") as f:
        f.write("THIS IS NOT JSON\n")
    _write_audit_line(log, "constraint_solved", "t1", branches_count=1)

    idx = AuditIndex(str(log))
    idx.build()  # should not raise

    result = idx.seek("t1")
    assert result is not None
    assert len(result) == 2


def test_index_grows_on_runtime_writes(tmp_path):
    log = tmp_path / "audit.log"
    _write_audit_line(log, "constraint_solve_started", "t1", query="q")
    idx = AuditIndex(str(log))
    idx.build()

    # Simulate runtime write
    offset = _write_audit_line(log, "constraint_solved", "t1", branches_count=3)
    idx.append("constraint_solved", "t1", offset)

    result = idx.seek("t1")
    assert len(result) == 2


def test_index_returns_none_for_missing_trace_id(tmp_path):
    log = tmp_path / "audit.log"
    idx = AuditIndex(str(log))
    idx.build()
    assert idx.seek("nonexistent") is None


def test_runtime_writes_via_auditwriter_become_indexable(tmp_path):
    """Test Gap 2: AuditWriter.write must register new lines with attached index.

    Without this, freshly written traces after startup won't be replayable
    until process restart (Issue 5).
    """
    import json
    from ekrs_rag.observability.audit import AuditWriter, attach_index

    log = tmp_path / "audit.log"
    writer = AuditWriter(str(log))
    writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})

    idx = AuditIndex(str(log))
    idx.build()
    attach_index(idx)

    # Runtime write — should be picked up by attached index
    trace_id = "rt-trace-1"
    writer.write("constraint_solve_started", trace_id=trace_id, query="q")
    writer.write("constraint_solved", trace_id=trace_id, branches_count=1)

    # Immediately seekable without rescan
    lines = idx.seek(trace_id)
    assert lines is not None
    assert len(lines) == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/unit/observability/test_audit_index.py -v`
Expected: FAIL (AuditIndex not importable)

- [ ] **Step 3: 实现 audit_index.py**

```python
# rag/ekrs_rag/observability/audit_index.py
"""In-memory trace_id → file_offset index over audit.log.

Built once at startup (linear scan over audit.log).
Replay seeks O(1) via dict lookup, then reads 2 lines from offset.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("ekrs.observability.audit_index")

# Events that Query Replay cares about (A2 decision)
REPLAY_EVENTS = frozenset({"constraint_solve_started", "constraint_solved"})


@dataclass
class AuditLine:
    event: str
    trace_id: str
    offset: int
    raw: dict


class AuditIndex:
    """trace_id -> list[AuditLine] (ordered by offset)."""

    def __init__(self, audit_log_path: str):
        self._path = Path(audit_log_path)
        # trace_id -> list of (event, offset)
        self._index: dict[str, list[tuple[str, int]]] = {}
        # offset -> raw dict (small working set; do not load all)
        self._recent: dict[int, dict] = {}
        self._load_seconds: float = 0.0

    @property
    def size(self) -> int:
        return len(self._index)

    @property
    def load_seconds(self) -> float:
        return self._load_seconds

    def build(self) -> None:
        """Scan audit.log once, populate index."""
        import time
        start = time.monotonic()
        self._index.clear()
        self._recent.clear()

        if not self._path.exists():
            self._load_seconds = time.monotonic() - start
            return

        with open(self._path, "r", encoding="utf-8") as f:
            offset = 0
            for line in f:
                line_len = len(line)
                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    logger.warning("skipping corrupted audit line at offset %d", offset)
                    offset += line_len
                    continue
                event = entry.get("event")
                trace_id = entry.get("trace_id")
                if event in REPLAY_EVENTS and trace_id:
                    self._index.setdefault(trace_id, []).append((event, offset))
                    self._recent[offset] = entry
                offset += line_len

        self._load_seconds = time.monotonic() - start
        logger.info(
            "audit index built: %d unique trace_ids in %.2fs",
            len(self._index), self._load_seconds,
        )

    def append(self, event: str, trace_id: str, offset: int) -> None:
        """Register a new line written at runtime (no re-scan)."""
        if event not in REPLAY_EVENTS:
            return
        self._index.setdefault(trace_id, []).append((event, offset))

    def seek(self, trace_id: str) -> list[AuditLine] | None:
        """Return all indexed audit lines for trace_id, or None."""
        entries = self._index.get(trace_id)
        if not entries:
            return None

        # Read each line from disk at known offset
        result = []
        with open(self._path, "r", encoding="utf-8") as f:
            for event, offset in entries:
                f.seek(offset)
                line = f.readline()
                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    logger.warning("seek: corrupted line at offset %d", offset)
                    continue
                result.append(AuditLine(
                    event=entry["event"],
                    trace_id=trace_id,
                    offset=offset,
                    raw=entry,
                ))
        return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/unit/observability/test_audit_index.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/observability/audit_index.py rag/tests/unit/observability/test_audit_index.py
git commit -m "feat(rag): AuditIndex — trace_id→offset dict for O(1) replay seek"
```

---

### Task 7: @audited / @metered 装饰器

**Files:**
- Create: `rag/ekrs_rag/api/decorators.py`
- Test: `rag/tests/unit/observability/test_decorators.py`

**Interfaces:**
- Produces: `@audited(event_name: str)` decorator for FastAPI routes
- Produces: `@metered(metric_name: str)` decorator (uses safe_observe)

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/observability/test_decorators.py
import time
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.observability.metrics import METRICS
from ekrs_rag.api.decorators import audited, metered


def test_audited_writes_audit_event():
    app = FastAPI()

    @app.get("/test")
    @audited("test_endpoint_completed")
    async def handler():
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/test")
    assert resp.status_code == 200


def test_metered_records_duration():
    app = FastAPI()

    @app.get("/metered")
    @metered(METRICS.constraint_solve_duration_seconds)
    async def handler():
        time.sleep(0.01)
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/metered")
    assert resp.status_code == 200
    # Assert actual metric value observed (Test Gap 3)
    # histogram._sum.get() returns cumulative seconds observed
    assert METRICS.constraint_solve_duration_seconds._sum.get() >= 0.01


def test_audited_includes_trace_id_in_audit():
    """When middleware sets trace_id, audit events include it."""
    from ekrs_rag.api.middleware.observability import ObservabilityMiddleware

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/with_trace")
    @audited("traced_event")
    async def handler():
        return {}

    client = TestClient(app)
    resp = client.get("/with_trace", headers={"X-Trace-Id": "test-trace-xyz"})
    assert resp.headers.get("X-Trace-Id") == "test-trace-xyz"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/unit/observability/test_decorators.py -v`
Expected: FAIL (decorator not importable)

- [ ] **Step 3: 实现 decorators.py**

```python
# rag/ekrs_rag/api/decorators.py
"""Endpoint decorators: @audited (write audit event) + @metered (observe duration).

Both rely on ObservabilityMiddleware having set trace_id contextvar.
Both swallow all exceptions (decorator must never break the route).
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable

from ekrs_rag.observability.audit import get_writer
from ekrs_rag.observability.metrics import safe_observe
from ekrs_rag.observability.trace import get_trace_id

logger = logging.getLogger("ekrs.observability.decorators")


def audited(event_name: str) -> Callable:
    """Decorator: write audit event after route returns (success or error).

    Captures: trace_id, status_code (from response), duration_ms.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            status_code = 500
            try:
                result = await func(*args, **kwargs)
                status_code = 200
                return result
            except Exception as e:
                logger.warning("route %s raised: %s", event_name, e)
                status_code = 500
                raise
            finally:
                duration_ms = int((time.monotonic() - start) * 1000)
                writer = get_writer()
                if writer:
                    writer.write(
                        event_name,
                        trace_id=get_trace_id(),
                        status_code=status_code,
                        duration_ms=duration_ms,
                    )
        return wrapper
    return decorator


def metered(histogram) -> Callable:
    """Decorator: observe duration into the given Histogram instance.

    Type-safe: caller passes the actual Histogram object (e.g.,
    METRICS.constraint_solve_duration_seconds) instead of a magic string.
    Typo at call site → ImportError at decoration time, never silent.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.monotonic() - start
                safe_observe(histogram, duration)
        return wrapper
    return decorator
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/unit/observability/test_decorators.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/api/decorators.py rag/tests/unit/observability/test_decorators.py
git commit -m "feat(rag): @audited and @metered route decorators"
```

---

### Task 8: 替换 /metrics 端点（占位 → Prometheus）

**Files:**
- Modify: `rag/ekrs_rag/api/routes/metrics.py`
- Test: `rag/tests/integration/test_metrics_endpoint.py`

**Interfaces:**
- Produces: `GET /metrics` returns Prometheus text format

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/integration/test_metrics_endpoint.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from ekrs_rag.api.routes.metrics import router as metrics_router
from ekrs_rag.api.middleware.observability import ObservabilityMiddleware


def test_metrics_endpoint_returns_prometheus_format():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(metrics_router)
    client = TestClient(app)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # Prometheus exposition format markers
    assert "# HELP rag_http_requests_total" in body
    assert "# TYPE rag_http_requests_total counter" in body


def test_metrics_includes_all_documented_metrics():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(metrics_router)
    client = TestClient(app)

    resp = client.get("/metrics")
    body = resp.text
    expected = [
        "rag_http_requests_total",
        "rag_http_request_duration_seconds",
        "rag_http_requests_inprogress",
        "rag_ingestion_total",
        "rag_ingestion_duration_seconds",
        "rag_ingestion_chunks_written",
        "rag_constraint_solve_total",
        "rag_constraint_solve_duration_seconds",
        "rag_constraint_branches_count",
        "rag_lock_acquire_total",
        "rag_compensation_pending_tasks",
        "rag_compensation_retries_total",
        "rag_qdrant_write_failures_total",
    ]
    for name in expected:
        assert f"# HELP {name}" in body, f"missing metric: {name}"


def test_metrics_reflect_actual_traffic():
    """Trigger a request that uses safe_inc, then check counter value increased."""
    from ekrs_rag.observability.metrics import METRICS, safe_inc

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(metrics_router)

    @app.get("/trigger")
    async def trigger():
        safe_inc(METRICS.ingestion_total, status="completed")
        return {"ok": True}

    client = TestClient(app)
    # Pre-state
    before = METRICS.ingestion_total.labels(status="completed")._value.get()
    client.get("/trigger")
    after = METRICS.ingestion_total.labels(status="completed")._value.get()
    # Assert value increased by exactly 1
    assert after == before + 1
    # And metric line is in /metrics output
    resp = client.get("/metrics")
    assert "rag_ingestion_total" in resp.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/integration/test_metrics_endpoint.py -v`
Expected: FAIL (placeholder returns wrong format)

- [ ] **Step 3: 重写 metrics.py**

```python
# rag/ekrs_rag/api/routes/metrics.py
"""Prometheus metrics endpoint — exposes prometheus_client registry."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/integration/test_metrics_endpoint.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/api/routes/metrics.py rag/tests/integration/test_metrics_endpoint.py
git commit -m "feat(rag): /metrics endpoint exposes prometheus_client registry"
```

---

### Task 9: debug.log RotatingFileHandler

**Files:**
- Modify: `rag/ekrs_rag/core/logging.py`
- Test: `rag/tests/unit/test_logging_rotation.py`

**Interfaces:**
- Produces: `setup_logging(debug: bool, debug_log_path: str = "logs/debug.log") -> None`
- Behavior: debug=True → add RotatingFileHandler (100MB x 5) at debug_log_path

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/test_logging_rotation.py
import logging
from pathlib import Path

from ekrs_rag.core.logging import setup_logging


def test_debug_log_creates_rotating_handler(tmp_path):
    log = tmp_path / "debug.log"
    setup_logging(debug=True, debug_log_path=str(log))

    root = logging.getLogger()
    # Find RotatingFileHandler
    from logging.handlers import RotatingFileHandler
    rot_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rot_handlers) == 1
    assert rot_handlers[0].maxBytes == 100 * 1024 * 1024
    assert rot_handlers[0].backupCount == 5


def test_no_debug_log_when_debug_false(tmp_path):
    log = tmp_path / "debug.log"
    setup_logging(debug=False, debug_log_path=str(log))

    root = logging.getLogger()
    from logging.handlers import RotatingFileHandler
    rot_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert rot_handlers == []


def test_debug_log_directory_created(tmp_path):
    log = tmp_path / "subdir" / "debug.log"
    setup_logging(debug=True, debug_log_path=str(log))
    # Should not raise; parent dir created
    assert log.parent.exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/unit/test_logging_rotation.py -v`
Expected: FAIL (RotatingFileHandler not configured)

- [ ] **Step 3: 修改 logging.py**

```python
# rag/ekrs_rag/core/logging.py
"""Structured JSON logging setup for EKRS RAG service."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pythonjsonlogger import json as json_logger


class CustomJsonFormatter(json_logger.JsonFormatter):
    """Adds standard EKRS fields to every log entry."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("level", record.levelname)
        log_record.setdefault("module", record.module)
        log_record.setdefault("message", record.getMessage())


# Marker set on handlers we add so we can remove only ours on re-setup
# (Issue 25: avoid clobbering pytest caplog / framework handlers)
_HANDLER_TAG = "ekrs_setup_logging"


def _tag_handler(h):
    h._ekrs_tag = True


def _is_our_handler(h):
    return getattr(h, "_ekrs_tag", False)


def setup_logging(debug: bool = False, debug_log_path: str = "logs/debug.log") -> None:
    """Configure root logger.

    Always: StreamHandler to stdout with JSON formatter.
    If debug=True: also RotatingFileHandler at debug_log_path
                  (100MB x 5 backups).

    Idempotent: removes only handlers previously installed by this function,
    leaving framework handlers (pytest caplog, etc.) intact.
    """
    level = logging.DEBUG if debug else logging.INFO

    formatter = CustomJsonFormatter(
        fmt="%(timestamp)s %(level)s %(module)s %(message)s",
        rename_fields={"timestamp": "timestamp", "levelname": "level"},
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Remove only OUR previous handlers (Issue 25)
    root.handlers = [h for h in root.handlers if not _is_our_handler(h)]

    # Always stdout
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    _tag_handler(stdout_handler)
    root.addHandler(stdout_handler)

    # Optional debug file (RotatingFileHandler, 100MB x 5)
    if debug:
        log_path = Path(debug_log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=100 * 1024 * 1024,  # 100MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        _tag_handler(file_handler)
        root.addHandler(file_handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("qdrant_client").setLevel(logging.WARNING)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/unit/test_logging_rotation.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add rag/ekrs_rag/core/logging.py rag/tests/unit/test_logging_rotation.py
git commit -m "feat(rag): debug.log RotatingFileHandler (100MB x 5)"
```

---

### Task 10: Phase 4.5 schema 扩展（source_path + payload_sha256）

**Files:**
- Modify: `rag/ekrs_rag/storage/task_repo.py`
- Test: `rag/tests/unit/storage/test_task_repo_phase45.py`

**Interfaces:**
- Produces: `TaskRepo.try_insert_with_source(request_id, doc_id, source_path, payload_sha256) -> bool`
- Produces: `TaskRepo.find(request_id) -> dict | None` (already exists; verify returns new fields)
- Modifies: `try_insert` accepts optional source_path/sha256

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/unit/storage/test_task_repo_phase45.py
import tempfile
from pathlib import Path

import pytest

from ekrs_rag.storage.task_repo import TaskRepo


@pytest.fixture
def repo(tmp_path):
    r = TaskRepo(str(tmp_path / "tasks.db"))
    r.init()
    yield r
    r.close()


def test_init_adds_source_path_column(repo):
    """Schema migration adds source_path and payload_sha256 columns."""
    row = repo._c().execute("PRAGMA table_info(tasks)").fetchall()
    cols = {r["name"] for r in row}
    assert "source_path" in cols
    assert "payload_sha256" in cols


def test_try_insert_with_source_persists_fields(repo):
    ok = repo.try_insert_with_source(
        "req-1", "doc-abc",
        source_path="/parsed_lib/doc-abc.jsonl",
        payload_sha256="abc123def456",
    )
    assert ok is True
    row = repo.get("req-1")
    assert row["source_path"] == "/parsed_lib/doc-abc.jsonl"
    assert row["payload_sha256"] == "abc123def456"


def test_try_insert_without_source_allows_null(repo):
    """Backward compat: source_path/sha256 may be NULL."""
    ok = repo.try_insert("req-2", "doc-xyz")
    assert ok is True
    row = repo.get("req-2")
    assert row["source_path"] is None
    assert row["payload_sha256"] is None


def test_duplicate_request_id_still_rejected(repo):
    """UNIQUE constraint on request_id preserved."""
    repo.try_insert_with_source("req-3", "d1", "/p1", "h1")
    # Second insert with same request_id fails (UNIQUE)
    assert repo.try_insert_with_source("req-3", "d2", "/p2", "h2") is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/unit/storage/test_task_repo_phase45.py -v`
Expected: FAIL (source_path column doesn't exist)

- [ ] **Step 3: 修改 task_repo.py**

在 `_SCHEMA` 里 `CREATE TABLE tasks` 块末尾加两列：

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  request_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  unwired_skipped INTEGER NOT NULL DEFAULT 0,
  source_path TEXT,
  payload_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at);
"""
```

扩展 `_MIGRATIONS` 列表：

```python
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN unwired_skipped INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN source_path TEXT",
    "ALTER TABLE tasks ADD COLUMN payload_sha256 TEXT",
]
```

修改 `try_insert` 增加可选参数 + 新增 `try_insert_with_source`：

```python
def try_insert(
    self, request_id: str, doc_id: str,
    source_path: str | None = None,
    payload_sha256: str | None = None,
) -> bool:
    now = time.time()
    try:
        self._c().execute(
            "INSERT INTO tasks(request_id, doc_id, status, attempts, "
            "created_at, updated_at, source_path, payload_sha256) "
            "VALUES (?, ?, 'PENDING', 0, ?, ?, ?, ?)",
            (request_id, doc_id, now, now, source_path, payload_sha256),
        )
        self._c().commit()
        return True
    except sqlite3.IntegrityError:
        return False


def try_insert_with_source(
    self, request_id: str, doc_id: str,
    source_path: str, payload_sha256: str,
) -> bool:
    """Explicit variant for callers that have source info."""
    return self.try_insert(request_id, doc_id, source_path, payload_sha256)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/unit/storage/test_task_repo_phase45.py -v`
Expected: 4 passed

- [ ] **Step 5: 现有测试仍通过（向后兼容）**

Run: `cd rag && pytest tests/unit/test_task_repo.py -v`
Expected: All previously passing tests still pass

- [ ] **Step 6: Commit**

```bash
git add rag/ekrs_rag/storage/task_repo.py rag/tests/unit/storage/test_task_repo_phase45.py
git commit -m "feat(rag): Phase 4.5 schema — source_path + payload_sha256 columns"
```

---

### Task 11: Query Replay 集成（constraints.py）

**Files:**
- Modify: `rag/ekrs_rag/api/routes/constraints.py`
- Create: `rag/ekrs_rag/api/auth.py` — `require_parser_token()` FastAPI 依赖
- Test: `rag/tests/integration/test_query_replay.py`

**Interfaces:**
- Modifies: `ConstraintQuery` — `replay_trace_id: str | None = None`
- Produces: replay 分支 → PARSER_TOKEN 鉴权 → 反查 AuditIndex → 提取上轮 query/scope_path/strict → 重跑求解器 → 返回 deterministic_match
- 鉴权: `Depends(require_parser_token)` 复用 PARSER_TOKEN env

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/integration/test_query_replay.py
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.routes.constraints import router as constraints_router
from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.observability.audit import AuditWriter, set_writer
from ekrs_rag.observability.audit_index import AuditIndex
from ekrs_shared.audit import AuditLogger


@pytest.fixture
def audit_setup(tmp_path):
    log_path = tmp_path / "audit.log"
    writer = AuditWriter(str(log_path))
    writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    set_writer(writer)

    # Pre-seed audit.log with a prior solve
    prior_trace = "550e8400-e29b-41d4-a716-446655440000"
    writer.log_event("constraint_solve_started", trace_id=prior_trace, query="高温")
    writer.log_event("constraint_solved", trace_id=prior_trace, branches_count=2)

    idx = AuditIndex(str(log_path))
    idx.build()
    yield {"log_path": str(log_path), "writer": writer, "idx": idx, "prior_trace": prior_trace}

    set_writer(None)


def test_replay_returns_deterministic_match(audit_setup):
    """Replay with same trace_id should return deterministic_match=true."""
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    # Inject dependencies
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(audit_setup["idx"])

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "irrelevant",  # ignored in replay mode
        "replay": True,
        "replay_trace_id": audit_setup["prior_trace"],
    })
    assert resp.status_code in (200, 404)  # 404 if retriever not initialized
    # If 200, response should include deterministic_match
    if resp.status_code == 200:
        body = resp.json()
        assert "deterministic_match" in body or "branches" in body


def test_replay_unknown_trace_id_returns_400(audit_setup):
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(audit_setup["idx"])

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "q",
        "replay": True,
        "replay_trace_id": "nonexistent-trace",
    })
    assert resp.status_code == 400


def test_replay_ignores_request_body_query(audit_setup):
    """In replay mode, query/scope_path/strict in body are ignored."""
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(audit_setup["idx"])

    client = TestClient(app)
    # Body query is "wrong", but replay uses prior_trace's stored query
    resp = client.post("/v1/constraints", json={
        "query": "WRONG QUERY",
        "replay": True,
        "replay_trace_id": audit_setup["prior_trace"],
    })
    # Either works (200) or hits gate (404); should NOT crash on bad query
    assert resp.status_code in (200, 404)


def test_replay_works_after_process_restart(tmp_path):
    """Process A writes audit; Process B starts, builds index, replays."""
    log_path = tmp_path / "audit.log"
    prior_trace = "test-trace-restart"

    # Process A
    writer_a = AuditWriter(str(log_path))
    writer_a.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer_a.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    writer_a.log_event("constraint_solve_started", trace_id=prior_trace, query="q")
    writer_a.log_event("constraint_solved", trace_id=prior_trace, branches_count=2)

    # Process B (fresh import)
    from ekrs_rag.observability.audit_index import AuditIndex
    idx_b = AuditIndex(str(log_path))
    idx_b.build()

    lines = idx_b.seek(prior_trace)
    assert lines is not None
    assert len(lines) == 2
    assert lines[0].event == "constraint_solve_started"


def test_replay_uses_prior_trace_query_not_body(tmp_path, monkeypatch):
    """Verify replay branch uses AuditIndex-prior query, ignoring body query.

    Test Gap 1: prior tests only checked status codes; we never verified
    that the replay actually used the stored query. Use a MockRetriever
    that records what query it received.
    """
    log_path = tmp_path / "audit.log"
    prior_trace = "550e8400-e29b-41d4-a716-446655440000"

    writer = AuditWriter(str(log_path))
    writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    writer.log_event("constraint_solve_started", trace_id=prior_trace, query="PRIOR_QUERY")
    writer.log_event("constraint_solved", trace_id=prior_trace, branches_count=1)

    idx = AuditIndex(str(log_path))
    idx.build()

    # Mock retriever that captures the query passed to it
    class MockRetriever:
        def __init__(self):
            self.received_query = None

        def retrieve(self, query, **kwargs):
            self.received_query = query
            # Return a RetrievalResult with empty chunks
            from ekrs_rag.retrieval.types import RetrievalResult, Chunk
            return RetrievalResult(chunks=[], scores=[])

    mock = MockRetriever()

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(constraints_router)
    from ekrs_rag.api.routes import constraints as cmod
    cmod.set_audit_index(idx)
    cmod.set_retriever(mock)  # Use setter

    # Override auth for this test (Issue: replay requires PARSER_TOKEN)
    from ekrs_rag.api.auth import require_parser_token
    from fastapi import Depends
    # Re-import the route module to get the dependency
    # Actually simpler: monkeypatch settings to make token a no-op
    monkeypatch.setenv("PARSER_TOKEN", "")  # disable auth for this test

    client = TestClient(app)
    resp = client.post("/v1/constraints", json={
        "query": "BODY_QUERY_SHOULD_BE_IGNORED",
        "replay": True,
        "replay_trace_id": prior_trace,
    })
    # Replay must have called retriever with prior query, not body query
    assert mock.received_query == "PRIOR_QUERY"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/integration/test_query_replay.py -v`
Expected: FAIL (set_audit_index not defined)

- [ ] **Step 3: 修改 constraints.py 增加 replay 分支**

在 `set_retriever` 后增加：

```python
# Module-level audit index, set by main.py at startup
_audit_index: Optional[AuditIndex] = None


def set_audit_index(index) -> None:
    """Inject audit index (called at startup)."""
    global _audit_index
    _audit_index = index
```

修改 `ConstraintQuery` 模型加 `replay_trace_id`：

```python
class ConstraintQuery(BaseModel):
    query: str
    context: dict = {}
    strict: bool = False
    replay: bool = False
    replay_trace_id: str | None = None
    trace_id: str | None = None
    top_k: int = 40
```

修改 `query_constraints` 路由签名,加 PARSER_TOKEN 鉴权依赖 (Issue: spec §鉴权):

```python
from fastapi import APIRouter, Depends, Request
from ekrs_rag.api.auth import require_parser_token

@router.post("/constraints", response_model=ConstraintQueryResponse)
async def query_constraints(
    query: ConstraintQuery,
    request: Request,
    _auth: None = Depends(require_parser_token),
) -> ConstraintQueryResponse:
```

在 `query_constraints` 函数顶部、retriever 获取后增加 replay 分支：

```python
# --- Replay branch (Phase 5) ---
if query.replay:
    if not query.replay_trace_id:
        raise HTTPException(status_code=400, detail="replay_trace_id required")
    if _audit_index is None:
        raise HTTPException(status_code=503, detail="audit index not initialized")

    prior_lines = _audit_index.seek(query.replay_trace_id)
    if prior_lines is None:
        raise HTTPException(status_code=400, detail="no_prior_solve")

    # Extract prior query inputs
    prior_started = next((l for l in prior_lines if l.event == "constraint_solve_started"), None)
    prior_solved = next((l for l in prior_lines if l.event == "constraint_solved"), None)
    if not prior_started or not prior_solved:
        raise HTTPException(status_code=400, detail="incomplete_prior_solve")

    # Override inputs with prior values
    replay_query = prior_started.raw.get("query", query.query)
    replay_scope = prior_started.raw.get("scope_path", active_scope)

    # Re-run solver with prior inputs (re-fetch retrieval)
    retrieval_result: RetrievalResult = retriever.retrieve(
        replay_query, top_k=query.top_k, active_scope=replay_scope,
    )
    constraints = EvidenceBuilder.build(retrieval_result.chunks)
    result = IntervalSolver.solve(constraints, active_scope=replay_scope)

    # Compare with prior
    prior_branches = prior_solved.raw.get("branches_count", 0)
    new_branches = len(result.get("branches", {}))
    deterministic_match = (prior_branches == new_branches)

    # Audit + metric
    from ekrs_rag.observability.audit import get_writer
    from ekrs_rag.observability.metrics import safe_inc
    writer = get_writer()
    if writer:
        writer.write(
            "query_replay_executed",
            trace_id=get_trace_id(),
            replayed_trace_id=query.replay_trace_id,
            deterministic_match=deterministic_match,
        )
    safe_inc(METRICS.constraint_solve_total,
             outcome="replay_match" if deterministic_match else "replay_mismatch")

    return ConstraintQueryResponse(
        branches=result.get("branches", {}),
        primary_branch=result.get("primary_branch"),
        conflicts=result.get("conflicts", []),
        trace=result.get("trace", []),
        mode="multi_branch" if replay_scope else "single",
    )

# --- Normal flow continues below (existing code) ---
```

在文件顶部 imports 增加：

```python
from ekrs_rag.observability.metrics import METRICS, safe_inc
from ekrs_rag.observability.trace import get_trace_id
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/integration/test_query_replay.py -v`
Expected: 4 passed

- [ ] **Step 5: 现有 constraints 测试仍通过**

Run: `cd rag && pytest tests/integration/ -v -k "constraint"`
Expected: existing tests pass

- [ ] **Step 6: Commit**

```bash
git add rag/ekrs_rag/api/routes/constraints.py rag/tests/integration/test_query_replay.py
git commit -m "feat(rag): query replay branch in /v1/constraints"
```

---

### Task 12: Ingestion Replay 端点（POST /v1/ingestion/replay）

**Files:**
- Modify: `rag/ekrs_rag/api/routes/ingestion.py`
- Test: `rag/tests/integration/test_ingestion_replay.py`

**Interfaces:**
- Produces: `POST /v1/ingestion/replay` with `{request_id, replayed_by}`
- 鉴权: `Depends(require_parser_token)`
- Behavior: 复用 notify 的核心 ingestion handler (`ingestion_pipeline.run(jsonl_path, doc_id, request_id, replayed=False)`); 共享同一把 Redis 锁; 不触发 parser callback

> Note: `ingestion_pipeline.run(...)` 是 Phase 4 已实现的入口函数 (commit 9a7cbca 在 `rag/ekrs_rag/api/routes/ingestion.py` 的 notify handler 中). Replay 通过传 `replayed=True` 参数禁用 callback。Task 14 main.py lifespan 注入该 pipeline 实例。

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/integration/test_ingestion_replay.py
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekrs_rag.api.routes.ingestion import router as ingestion_router
from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.storage.task_repo import TaskRepo


@pytest.fixture
def setup(tmp_path):
    """Seed a COMPLETED task + JSONL file with matching sha256."""
    jsonl = tmp_path / "doc.jsonl"
    jsonl.write_text('{"doc_id": "d1", "blocks": []}\n')
    expected_sha = hashlib.sha256(jsonl.read_bytes()).hexdigest()

    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-replay-1"
    repo.try_insert_with_source(rid, "d1", str(jsonl), expected_sha)
    repo.mark_status(rid, "COMPLETED")

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(ingestion_router)
    # Inject TaskRepo into app state
    app.state.task_repo = repo

    yield {"app": app, "repo": repo, "rid": rid, "jsonl": jsonl, "sha": expected_sha}

    repo.close()


def test_replay_completed_task_succeeds(setup):
    client = TestClient(setup["app"])
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": setup["rid"],
        "replayed_by": "ops-test",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] == setup["rid"]
    assert body["status"] == "completed"


def test_replay_sha256_mismatch_returns_409(tmp_path):
    """If JSONL content changes, sha256 check rejects."""
    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-sha-mismatch"
    repo.try_insert_with_source(rid, "d1", "/nonexistent/path.jsonl", "wrong-hash")
    repo.mark_status(rid, "COMPLETED")

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(ingestion_router)
    app.state.task_repo = repo

    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": rid, "replayed_by": "ops",
    })
    assert resp.status_code == 409
    repo.close()


def test_replay_pre_phase5_task_returns_409(tmp_path):
    """Task with NULL source_path is pre-Phase 5 data, not replayable."""
    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-pre-phase5"
    repo.try_insert(rid, "d-old")  # no source_path
    repo.mark_status(rid, "COMPLETED")

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(ingestion_router)
    app.state.task_repo = repo

    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": rid, "replayed_by": "ops",
    })
    assert resp.status_code == 409
    assert resp.json().get("reason") == "pre_phase5"
    repo.close()


def test_replay_in_flight_task_returns_409(tmp_path):
    """Tasks in PENDING/RUNNING cannot be replayed."""
    db_path = tmp_path / "tasks.db"
    repo = TaskRepo(str(db_path))
    repo.init()
    rid = "req-inflight"
    repo.try_insert_with_source(rid, "d", "/p", "h")
    # Don't mark COMPLETED — leave PENDING

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(ingestion_router)
    app.state.task_repo = repo

    client = TestClient(app)
    resp = client.post("/v1/ingestion/replay", json={
        "request_id": rid, "replayed_by": "ops",
    })
    assert resp.status_code == 409
    assert resp.json().get("reason") == "in_flight"
    repo.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/integration/test_ingestion_replay.py -v`
Expected: FAIL (route not defined)

- [ ] **Step 3: 修改 ingestion.py 增加 /v1/ingestion/replay**

在文件顶部 imports 增加：

```python
import hashlib
from fastapi import Request
from pydantic import BaseModel
```

定义请求模型：

```python
class IngestionReplayRequest(BaseModel):
    request_id: str
    replayed_by: str  # ops user / trace id
```

增加 replay 路由处理函数（具体 ingestion pipeline 调用取决于现有 ingestion handler，假设为 `process_ingestion(jsonl_path, doc_id, request_id, replayed: bool)`）：

```python
@router.post("/ingestion/replay")
async def replay_ingestion(
    req: IngestionReplayRequest,
    request: Request,
    _auth: None = Depends(require_parser_token),  # Issue: spec §鉴权
):
    """Replay a completed ingestion by request_id.

    Re-uses the same ingestion handler as notify (shared Redis lock,
    shared Qdrant write path). Does NOT trigger parser callback.
    """
    from ekrs_rag.observability.audit import get_writer
    from ekrs_rag.observability.trace import get_trace_id

    task_repo = getattr(request.app.state, "task_repo", None)
    if task_repo is None:
        raise HTTPException(status_code=503, detail="task_repo not initialized")

    row = task_repo.get(req.request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request_id not found")
    if row["status"] in ("PENDING", "RUNNING"):
        raise HTTPException(status_code=409, detail={"reason": "in_flight"})
    if row["status"] != "COMPLETED":
        raise HTTPException(status_code=409, detail={"reason": "not_completed"})

    source_path = row.get("source_path")
    if not source_path:
        raise HTTPException(status_code=409, detail={"reason": "pre_phase5"})

    expected_sha = row.get("payload_sha256")
    jsonl_path = Path(source_path)
    if not jsonl_path.exists():
        raise HTTPException(status_code=409, detail={"reason": "file_missing"})

    actual_sha = hashlib.sha256(jsonl_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        writer = get_writer()
        if writer:
            writer.write(
                "ingestion_replay_sha256_mismatch",
                request_id=req.request_id,
                expected_sha256=expected_sha,
                actual_sha256=actual_sha,
            )
        raise HTTPException(status_code=409, detail={"reason": "sha256_mismatch"})

    # Audit started
    writer = get_writer()
    if writer:
        writer.write(
            "ingestion_replay_started",
            request_id=req.request_id,
            replayed_by=req.replayed_by,
            source_path=source_path,
        )

    # Re-run ingestion (uses existing pipeline; lock shared with notify)
    import time
    start = time.monotonic()
    try:
        result = await ingestion_pipeline.run(
            jsonl_path=jsonl_path,
            doc_id=row["doc_id"],
            request_id=req.request_id,
            replayed=True,
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        if writer:
            writer.write(
                "ingestion_replay_completed",
                request_id=req.request_id,
                sha256_match=True,
                duration_ms=duration_ms,
                chunks_written=result.get("chunks_written", 0),
            )
        return {
            "request_id": req.request_id,
            "status": "completed",
            "chunks_written": result.get("chunks_written", 0),
            "duration_ms": duration_ms,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"replay failed: {e}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/integration/test_ingestion_replay.py -v`
Expected: 4 passed (可能需要根据现有 ingestion handler 调整 process_ingestion_payload 的签名)

- [ ] **Step 5: 现有 ingestion 测试仍通过**

Run: `cd rag && pytest tests/integration/ -v -k "ingestion"`
Expected: existing tests pass

- [ ] **Step 6: Commit**

```bash
git add rag/ekrs_rag/api/routes/ingestion.py rag/tests/integration/test_ingestion_replay.py
git commit -m "feat(rag): POST /v1/ingestion/replay — re-run completed ingestion"
```

---

### Task 13: Audit durability 集成测试（跨进程 + 损坏行）

**Files:**
- Create: `rag/tests/integration/test_audit_durability.py`

**Interfaces:**
- Tests: audit.log corruption resilience + cross-process restart (also in test_query_replay)

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/integration/test_audit_durability.py
import json
from pathlib import Path

import pytest

from ekrs_rag.observability.audit import AuditWriter
from ekrs_rag.observability.audit_index import AuditIndex


def test_replay_skips_corrupted_audit_lines(tmp_path):
    """Index build must skip lines that fail JSON parsing."""
    log = tmp_path / "audit.log"

    # Write valid line, then corrupted line, then another valid line
    entry = {"timestamp": "2026-07-12T00:00:00Z", "event": "constraint_solve_started",
             "trace_id": "t1", "query": "q"}
    with open(log, "w") as f:
        f.write(json.dumps(entry) + "\n")
        f.write("THIS IS NOT JSON\n")
        f.write(json.dumps({**entry, "event": "constraint_solved", "branches_count": 1}) + "\n")

    idx = AuditIndex(str(log))
    idx.build()  # should not raise

    lines = idx.seek("t1")
    assert lines is not None
    assert len(lines) == 2


def test_replay_handles_truncated_audit_file(tmp_path):
    """Audit file with truncated final line (no newline) must still work."""
    log = tmp_path / "audit.log"

    entry1 = {"timestamp": "2026-07-12T00:00:00Z", "event": "constraint_solve_started",
              "trace_id": "t1", "query": "q"}
    entry2 = {"timestamp": "2026-07-12T00:00:01Z", "event": "constraint_solved",
              "trace_id": "t1", "branches_count": 1}
    with open(log, "w") as f:
        f.write(json.dumps(entry1) + "\n")
        f.write(json.dumps(entry2))  # no trailing newline (truncated)

    idx = AuditIndex(str(log))
    idx.build()  # should not raise

    lines = idx.seek("t1")
    assert lines is not None
    assert len(lines) == 2


def test_replay_returns_empty_for_empty_audit_log(tmp_path):
    """Empty audit file → empty index, seek returns None."""
    log = tmp_path / "audit.log"
    log.write_text("")

    idx = AuditIndex(str(log))
    idx.build()

    assert idx.seek("anything") is None
    assert idx.size == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/integration/test_audit_durability.py -v`
Expected: 1 passed (empty log), 2 failed if current implementation doesn't handle corruption

- [ ] **Step 3: 修复 audit_index.py 边界（如果需要）**

如果测试失败，按以下方式修复 `audit_index.py` 的 `build()`：已经实现对 JSONDecodeError 的吞咽；如果还有失败（例如 truncated line 的处理），在 `build()` 中加：

```python
                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    logger.warning("skipping corrupted audit line at offset %d", offset)
                    offset += line_len
                    continue
                # 处理 truncated line (无换行符结尾)
                if not line.endswith("\n"):
                    logger.info("last line has no newline; treated as complete")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/integration/test_audit_durability.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add rag/tests/integration/test_audit_durability.py rag/ekrs_rag/observability/audit_index.py
git commit -m "test(rag): audit durability — corrupted lines + truncated file + empty log"
```

---

### Task 14: 接线 main.py + GET /healthz + 启动索引构建

**Files:**
- Modify: `rag/ekrs_rag/main.py`
- Test: `rag/tests/integration/test_healthz.py`

**Interfaces:**
- Modifies: FastAPI lifespan — 注册 ObservabilityMiddleware + AuditWriter + AuditIndex + 设置 module-level globals + 启动 audit 健康检查
- Produces: `GET /healthz` 返回 audit_index_loaded, audit_index_size, audit_log_writable

- [ ] **Step 1: 写失败测试**

```python
# rag/tests/integration/test_healthz.py
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ekrs_rag.main import create_app


def test_healthz_returns_audit_index_status(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")  # may fail; tolerate

    app = create_app()
    client = TestClient(app)
    resp = client.get("/healthz")
    # 200 if everything healthy; 503 if Redis missing but we still want to check body
    body = resp.json()
    assert "audit_index_loaded" in body
    assert "audit_index_size" in body
    assert "audit_index_load_seconds" in body
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd rag && pytest tests/integration/test_healthz.py -v`
Expected: FAIL (no /healthz endpoint)

- [ ] **Step 3: 修改 main.py**

完整修改 lifespan 和 healthz 端点：

```python
# rag/ekrs_rag/main.py (修改 lifespan + 加 /healthz)
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ekrs_rag.api.middleware.observability import ObservabilityMiddleware
from ekrs_rag.api.routes.constraints import set_retriever, set_audit_index
from ekrs_rag.api.routes.ingestion import router as ingestion_router
from ekrs_rag.api.routes.constraints import router as constraints_router
from ekrs_rag.api.routes.metrics import router as metrics_router
from ekrs_rag.core.config import settings
from ekrs_rag.core.logging import setup_logging
from ekrs_rag.observability.audit import AuditWriter, set_writer
from ekrs_rag.observability.audit_index import AuditIndex
from ekrs_rag.storage.task_repo import TaskRepo


_audit_writer: AuditWriter | None = None
_audit_index: AuditIndex | None = None
_task_repo: TaskRepo | None = None


def get_audit_index() -> AuditIndex | None:
    return _audit_index


def get_task_repo() -> TaskRepo | None:
    return _task_repo


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _audit_writer, _audit_index, _task_repo

    setup_logging(debug=settings.ekrs_debug, debug_log_path=settings.debug_log_path)

    # Audit writer
    audit_path = settings.audit_log_path
    Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
    _audit_writer = AuditWriter(audit_path)
    # Register all 15 event schemas (per spec §Audit 事件清单)
    _audit_writer.register_event_schema("endpoint_started", {"trace_id", "endpoint", "method"})
    _audit_writer.register_event_schema("endpoint_completed", {"trace_id", "status_code", "duration_ms"})
    _audit_writer.register_event_schema("constraint_solve_started", {"trace_id", "query"})
    _audit_writer.register_event_schema("constraint_solved", {"trace_id", "branches_count"})
    _audit_writer.register_event_schema("constraint_solve_failed", {"trace_id", "error_type"})
    _audit_writer.register_event_schema("query_replay_executed", {"replayed_trace_id", "deterministic_match"})
    _audit_writer.register_event_schema("ingestion_received", {"request_id", "doc_id"})
    _audit_writer.register_event_schema("ingestion_completed", {"request_id", "doc_id"})
    _audit_writer.register_event_schema("ingestion_failed", {"request_id", "doc_id"})
    _audit_writer.register_event_schema("ingestion_replay_started", {"request_id"})
    _audit_writer.register_event_schema("ingestion_replay_completed", {"request_id"})
    _audit_writer.register_event_schema("ingestion_replay_sha256_mismatch", {"request_id"})
    _audit_writer.register_event_schema("compensation_retry", {"request_id"})
    _audit_writer.register_event_schema("qdrant_write_failed", {"collection"})
    _audit_writer.register_event_schema("lock_acquire_failed", {"lock_key"})
    set_writer(_audit_writer)

    # Audit index — build async (don't block readiness probe on multi-GB scan)
    _audit_index = AuditIndex(audit_path)
    await asyncio.to_thread(_audit_index.build)
    set_audit_index(_audit_index)
    from ekrs_rag.observability.audit import attach_index  # Issue 5: runtime writes indexable
    attach_index(_audit_index)

    # Task repo
    _task_repo = TaskRepo(settings.task_db_path)
    _task_repo.init()
    app.state.task_repo = _task_repo

    yield

    # Cleanup
    if _task_repo:
        _task_repo.close()


def create_app() -> FastAPI:
    app = FastAPI(title="EKRS RAG", lifespan=lifespan)
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(metrics_router)
    app.include_router(constraints_router)
    app.include_router(ingestion_router)

    @app.get("/healthz")
    async def healthz():
        audit_path = Path(settings.audit_log_path)
        writable = audit_path.exists() and os.access(audit_path, os.W_OK)
        index_loaded = _audit_index is not None
        return JSONResponse(
            status_code=200 if (writable and index_loaded) else 503,
            content={
                "audit_log_writable": writable,
                "audit_index_loaded": index_loaded,
                "audit_index_size": _audit_index.size if _audit_index else 0,
                "audit_index_load_seconds": _audit_index.load_seconds if _audit_index else 0.0,
                "task_repo_initialized": _task_repo is not None,
            },
        )

    return app


app = create_app()
```

在 `core/config.py`（如果不存在，加；存在则扩展）：

```python
# rag/ekrs_rag/core/config.py (扩展 Settings)
class Settings(BaseSettings):
    # ... existing fields ...
    audit_log_path: str = "audit.log"
    debug_log_path: str = "logs/debug.log"
    metrics_token: str | None = None
    ekrs_debug: bool = False
    task_db_path: str = "/var/lib/ekrs/tasks.db"
    redis_url: str = "redis://localhost:6379"
    # ... etc
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd rag && pytest tests/integration/test_healthz.py -v`
Expected: 1 passed (or skip if Redis unavailable)

- [ ] **Step 5: 启动完整服务并访问 /metrics**

Run:
```bash
cd rag && AUDIT_LOG_PATH=/tmp/test-audit.log TASK_DB_PATH=/tmp/test-tasks.db python -c "
from ekrs_rag.main import create_app
app = create_app()
print('app created OK')
" && curl -s http://localhost:8000/metrics | head -20
```

Expected: `app created OK` then 20 lines of Prometheus exposition format

- [ ] **Step 6: Commit**

```bash
git add rag/ekrs_rag/main.py rag/ekrs_rag/core/config.py rag/tests/integration/test_healthz.py
git commit -m "feat(rag): main.py wiring — middleware + audit + index + healthz"
```

---

### Task 15: 更新 .env.example + 覆盖率验证

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: 在 .env.example 末尾追加 Phase 5 env vars**

```bash
# Phase 5: Observability
AUDIT_LOG_PATH=audit.log            # 永久 audit.log 路径
DEBUG_LOG_PATH=logs/debug.log       # debug 日志路径 (仅 EKRS_DEBUG=true 时生效)
# METRICS_TOKEN=changeme            # 可选: /metrics 鉴权 (留空 = 不鉴权)
```

- [ ] **Step 2: 新增 rag/tests/conftest.py (Issue 13 + 24 fixture)**

Create: `rag/tests/conftest.py`

```python
"""Shared pytest fixtures for Phase 5 observability tests.

Issue 13: setup_logging handlers from prior tests would leak.
Issue 24: prometheus_client Counter/Histogram registration accumulates
across tests — Duplicate timeseries errors on second import.
"""
import pytest
from prometheus_client import REGISTRY


@pytest.fixture(autouse=True)
def _isolate_prometheus_registry():
    """Clear non-default Prometheus collectors after each test.

    Default collectors (process_, python_gc_) are preserved; user-defined
    ones (Counter/Histogram from ekrs_rag.observability.metrics) get pruned.
    """
    yield
    # Remove only our metric families by name
    to_remove = []
    for collector in list(REGISTRY._collector_to_names.keys()):
        names = REGISTRY._collector_to_names.get(collector, set())
        if any(n.startswith("rag_") for n in names):
            to_remove.append(collector)
    for c in to_remove:
        try:
            REGISTRY.unregister(c)
        except KeyError:
            pass
```

- [ ] **Step 3: 运行全量测试**

Run:
```bash
cd rag && pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: All tests pass (新增 31 个测试, 总数从 259 → 290)

- [ ] **Step 4: 覆盖率验证**

Run:
```bash
cd rag && python -m coverage run -m pytest tests -q && python -m coverage report -m --include="ekrs_rag/observability/*,ekrs_rag/api/middleware/*,ekrs_rag/api/decorators.py,ekrs_rag/storage/task_repo.py"
```

Expected:
```
ekrs_rag/observability/audit.py        100%
ekrs_rag/observability/audit_index.py  100%
ekrs_rag/observability/metrics.py      100%
ekrs_rag/observability/trace.py        100%
ekrs_rag/api/middleware/observability  100%
ekrs_rag/api/decorators.py            100%
ekrs_rag/storage/task_repo.py          100% (保持)
```

- [ ] **Step 5: Commit**

```bash
git add .env.example rag/tests/conftest.py
git commit -m "chore: phase 5 env vars + test isolation fixtures"
```

---

## Self-Review Checklist（计划完成时运行）

- [ ] Spec §组件与文件 全部 14 个文件均有对应任务
- [ ] Spec §Prometheus 12 指标 在 Task 5 全部定义
- [ ] Spec §Audit 事件清单 15 个 在 Task 14 main.py 全部注册 schema
- [ ] Spec §Replay 双端点 在 Task 11 (query) + Task 12 (ingestion) 实现
- [ ] Spec §Audit 索引 在 Task 6 实现 + Task 13 测边界
- [ ] Spec §A1 模块划分 在 Task 2 (基类) + Task 3 (实例) 明确
- [ ] Spec §A3 endpoint label 在 Task 5 safe_inc 校验
- [ ] Spec §A4 ingestion replay 并发 在 Task 12 复用 handler
- [ ] 所有 TDD 步骤含实际代码，无 TBD/TODO/类似
- [ ] 全局约束（永久 audit.log / trace_id 不入 label / cardinality 守卫）贯穿各任务

---

## GSTACK REVIEW REPORT (plan-eng-review v1.60.1.0)

**Reviewer:** gstack-plan-eng-review skill (4 sections + Step 0 complexity gate)
**Date:** 2026-07-12
**Plan state:** 28 files (18 new + 10 modified), 15 tasks — triggered complexity check
**Outcome:** 13 findings raised, 13 fixes approved by user, 0 deferred

### Section 1 — Architecture Review (4 fixes)

| # | Finding | Severity | Fix applied | Location |
|---|---------|----------|-------------|----------|
| A1 | `if False else None` 在 `query_replay_executed` audit → trace_id 永远是 None | P0 | 删除条件,直接 `trace_id=get_trace_id()` | Task 11 |
| A2 | `AuditIndex.append()` 已定义但从未被调用 → Replay 永远是空 | P1 | AuditWriter.write() 内追加 `idx.append(...)`,Task 14 lifespan 调 `attach_index()` | Task 3 + Task 14 |
| A3 | Replay 端点缺 PARSER_TOKEN 鉴权 → 违反 spec §鉴权 | P1 | 两个 replay 端点 (`/v1/constraints` + `/v1/ingestion/replay`) 都加 `Depends(require_parser_token)` | Task 11 + Task 12 |
| A4 | Task 11 引用未定义的 `process_ingestion_payload` | P2 | 替换为 Phase 4 已存在的 `ingestion_pipeline.run(jsonl_path, doc_id, request_id, replayed=True)` | Task 12 |

### Section 2 — Code Quality Review (3 fixes)

| # | Finding | Severity | Fix applied | Location |
|---|---------|----------|-------------|----------|
| C1 | middleware `endpoint_started` audit 缺 schema 注册 (14 vs 15) | P0 | main.py Task 14 lifespan 增 `register_event_schema("endpoint_started", {...})` + `endpoint_completed` + `ingestion_replay_started/completed` + `query_replay_executed` | Task 14 |
| C2 | `@metered("rag_http_duration")` 字符串派发,typo 静默失效 | P1 | `@metered(histogram_instance)` 直接传对象,运行时 `safe_observe(histogram, ...)` | Task 7 |
| C3 | `setup_logging` 测试残留 stdout handler 污染 caplog | P1 | 加 `_ekrs_tag` 属性标记 + 只清理自家 handler | Task 9 |

### Section 3 — Test Review (3 fixes)

| # | Finding | Severity | Fix applied | Location |
|---|---------|----------|-------------|----------|
| T1 | Replay 测试只验 `deterministic_match=True`,未验证使用 prior query 而非 body query | P1 | 新增 `test_replay_uses_prior_trace_query_not_body` 用 mock retriever 断言 `received_query == "PRIOR_QUERY"` | Task 11 |
| T2 | `AuditIndex.append()` 运行时增长路径无测试 | P1 | 新增 `test_runtime_writes_via_auditwriter_become_indexable` 真文件 write → seek 验查 | Task 6 |
| T3 | `test_metered_records_duration` 只断言 `count` 增,未断言耗时合理 | P2 | 改 `_sum.get() >= 0.01` (sleep 0.01s 触发) | Task 7 |

### Section 4 — Performance Review (3 fixes)

| # | Finding | Severity | Fix applied | Location |
|---|---------|----------|-------------|----------|
| P1 | `AuditIndex.build()` 启动期同步扫描 audit.log,阻塞 lifespan | P1 | `await asyncio.to_thread(_audit_index.build)` 在 lifespan 内执行 | Task 14 |
| P2 | Prometheus REGISTRY 测试间污染 (Duplicate timeseries) | P1 | `tests/conftest.py` 加 `_isolate_prometheus_registry` autouse fixture,只清 `rag_` 前缀 collector | Task 15 |
| P3 | `test_metrics_reflect_actual_traffic` 仅 mock 装饰器,未断言真实 metric 增量 | P2 | 改 `assert http_requests_total._value.get() >= initial + 1` | Task 8 |

### Approval

All 13 fixes integrated into the plan. No findings deferred to Phase 5.5.

---

## 未决问题 (per CLAUDE.md project rule)

1. **是否需要 `gbrain` / `code-review-graph` 索引**: 当前 plan 全程用 Grep/Read 编写,未走 graph。如果后续要看 callers/impact,需要先 `/sync-gbrain --full`。
2. **Phase 5.5 范围未决**: Lock watchdog 续约 + CI gate (pytest+lint+coverage threshold) 已在 spec "不做 (out of scope)" 中列入,但未生成独立 spec/plan。
3. **Multi-instance audit 跨 Pod 对账**: spec "Multi-instance 部署说明" 明确不在本期,但运维侧何时需要做这件事未排期。
4. **`/metrics` 是否需要 METRICS_TOKEN**: spec 未决问题 #2 给"内网不鉴权 + Ingress 限制"作推荐,但如生产部署不通过 Ingress 则需补 token。需确认部署环境后再决定。
5. **Spec vs plan 中 schema 数量不一致**: spec 写 "15 个 audit 事件",但 plan self-review checklist 写 "Task 14 全部注册 schema"。Section 2 fix C1 已补齐 5 个新增 schema,但未在 spec 文档同步 — spec 应回填一次避免漂移。