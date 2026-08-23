"""Phase 13a T3 — picklable Step5 worker fn tests.

The worker is called by pebble from a subprocess (T4). Tests use
monkeypatch on the factory fns (`_build_qdrant`, `_build_fts`,
`_build_redis_lock`) so the worker logic runs end-to-end without
real Qdrant / Redis / bge-m3 ONNX.

Contract (plan T3.1 enumeration + boundaries):
- Happy path: 2-block JSONL → success outcome with chunks_indexed
- Idempotent skip: Qdrant already has same version → outcome success,
  NO encode/upsert called
- FTS paired write: FTS replace_doc called with same chunks + count match
- chunk_gate over-limit: 3001 chunks → failed chunks_over_limit,
  NO encode/upsert called
- concurrent_skip: RedisLock acquire fails → outcome success +
  error_code=concurrent_skip (idempotent semantics — another worker
  has it; not a failure)
- Unhandled exception: exception escaped → outcome failed +
  error_code=worker_unhandled (pebble wraps with ProcessExpired; this
  is the in-process safety net)
- Picklability: Step5Payload + run_step5 are picklable (pebble requirement)
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any, Optional

import pytest

from ekrs_rag.services.step5_worker import (
    Step5Payload,
    run_step5,
)


# ---------------------------------------------------------------------------
# Stub clients
# ---------------------------------------------------------------------------


class _StubQdrant:
    """QdrantManager stub for worker tests."""

    def __init__(
        self,
        *,
        ingestion_status: Optional[Any] = None,
        upsert_count: int = 1,
        upsert_exc: Optional[Exception] = None,
    ) -> None:
        self._ingestion_status = ingestion_status
        self._upsert_count = upsert_count
        self._upsert_exc = upsert_exc
        self.upsert_calls: list[list] = []
        self.delete_calls: list[tuple[str, int]] = []

    def get_ingestion_status(self, doc_hash: str) -> Any:
        return self._ingestion_status

    def upsert_chunks(self, chunks: list) -> int:
        self.upsert_calls.append(list(chunks))
        if self._upsert_exc is not None:
            raise self._upsert_exc
        return self._upsert_count

    def delete_old_versions(self, doc_hash: str, *, keep_version: int) -> int:
        self.delete_calls.append((doc_hash, keep_version))
        return 0


class _StubFTS:
    """FTSManager stub for worker tests."""

    def __init__(self, *, replace_exc: Optional[Exception] = None) -> None:
        self._replace_exc = replace_exc
        self.replace_calls: list[tuple[str, list, int]] = []

    def replace_doc(self, doc_hash: str, chunks: list, *, version: int) -> int:
        self.replace_calls.append((doc_hash, list(chunks), version))
        if self._replace_exc is not None:
            raise self._replace_exc
        return len(chunks)


class _StubRedisLock:
    """RedisLock stub: control acquire/release + concurrent_skip path."""

    def __init__(self, *, acquire_succeeds: bool = True) -> None:
        self._acquire_succeeds = acquire_succeeds
        self.acquire_calls: list[tuple[str, int]] = []
        self.release_calls: list[tuple[str, Optional[str]]] = []

    async def acquire(self, key: str, ttl_sec: int) -> Optional[str]:
        self.acquire_calls.append((key, ttl_sec))
        return "test-token" if self._acquire_succeeds else None

    async def release(self, key: str, token: Optional[str]) -> bool:
        self.release_calls.append((key, token))
        return True


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, n_blocks: int = 2) -> None:
    """Write n_blocks of valid DocumentBlockIR-shaped JSONL.

    Each block's raw content is 3000 chars (~750 tokens) — under
    MAX_CHUNK_TOKENS=768, so the chunker emits exactly one chunk per
    block. >3000 chars triggers "exceeds max_tokens" split into 2+
    chunks; <~600 chars gets merged with neighbors.
    """
    path.mkdir(parents=True, exist_ok=True)
    big_raw = "x" * 3000
    with (path / "data.jsonl").open("w", encoding="utf-8") as f:
        for i in range(n_blocks):
            obj = {
                "doc_id": "d1",
                "block_id": f"b{i}",
                "type": "text",
                "content": {"raw": big_raw, "md_preview": big_raw},
                "metadata": {"page_number": 1, "heading_path": []},
            }
            f.write(json.dumps(obj, ensure_ascii=False))
            f.write("\n")


def _wire_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    qdrant: _StubQdrant,
    fts: _StubFTS,
    lock: _StubRedisLock,
    tmp_path: Path,
) -> None:
    """Inject stubs into step5_worker module-level factories.

    Also patches settings.SHARED_STORAGE_PATH → tmp_path so the
    defense-in-depth check in _prepare_step5 (services/step5_helpers.py)
    accepts the test's tmp_path output_path as in-scope.
    """
    import ekrs_rag.services.step5_worker as worker
    from ekrs_rag.core.config import settings

    monkeypatch.setattr(settings, "SHARED_STORAGE_PATH", tmp_path)
    monkeypatch.setattr(worker, "_build_qdrant", lambda: qdrant)
    monkeypatch.setattr(worker, "_build_fts", lambda: fts)
    monkeypatch.setattr(worker, "_build_redis_lock", lambda: lock)


def _make_payload(output_path: Path, *, doc_hash: str = "d1", version: int = 1) -> Step5Payload:
    return Step5Payload(
        trace_id="trace-1",
        doc_hash=doc_hash,
        version=version,
        output_path=str(output_path),
    )


# ---------------------------------------------------------------------------
# Test 1: happy path (plan T3.1 verbatim)
# ---------------------------------------------------------------------------


def test_run_step5_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2-block JSONL → outcome rag_status='success', chunks_indexed=N."""
    _write_jsonl(tmp_path, n_blocks=2)
    qdrant = _StubQdrant(upsert_count=2)
    fts = _StubFTS()
    lock = _StubRedisLock(acquire_succeeds=True)
    _wire_stubs(monkeypatch, qdrant=qdrant, fts=fts, lock=lock, tmp_path=tmp_path)

    payload = _make_payload(tmp_path)
    result = run_step5(payload)

    assert result["rag_status"] == "success"
    assert result["chunks_indexed"] == 2
    assert result.get("error") is None
    assert len(qdrant.upsert_calls) == 1
    assert len(qdrant.upsert_calls[0]) == 2
    assert len(fts.replace_calls) == 1
    assert fts.replace_calls[0][2] == 1  # version
    assert lock.acquire_calls, "RedisLock must wrap the work"


# ---------------------------------------------------------------------------
# Test 2: idempotent skip (plan T3.1 verbatim)
# ---------------------------------------------------------------------------


def test_run_step5_idempotent_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Qdrant already has same doc+version → skip; no encode/upsert.

    Idempotent returns rag_status='success' (preserves pipeline semantics
    in pipeline.py:147 — already indexed at same version is success).
    """
    existing = type("S", (), {"status": "success", "version": 1, "chunks_indexed": 42})()
    qdrant = _StubQdrant(ingestion_status=existing)
    fts = _StubFTS()
    lock = _StubRedisLock()
    _wire_stubs(monkeypatch, qdrant=qdrant, fts=fts, lock=lock, tmp_path=tmp_path)

    payload = _make_payload(tmp_path)
    result = run_step5(payload)

    assert result["rag_status"] == "success"
    assert result["chunks_indexed"] == 42
    # NO upsert, NO fts write, NO lock acquired (idempotent short-circuits)
    assert qdrant.upsert_calls == []
    assert fts.replace_calls == []
    assert lock.acquire_calls == []


# ---------------------------------------------------------------------------
# Test 3: FTS paired write (plan T3.1 verbatim)
# ---------------------------------------------------------------------------


def test_run_step5_fts_paired_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FTS replace_doc called with same chunks + version, count matches qdrant.

    E8 invariant: paired writes must agree on count.
    """
    _write_jsonl(tmp_path, n_blocks=3)
    qdrant = _StubQdrant(upsert_count=3)
    fts = _StubFTS()
    lock = _StubRedisLock()
    _wire_stubs(monkeypatch, qdrant=qdrant, fts=fts, lock=lock, tmp_path=tmp_path)

    payload = _make_payload(tmp_path, version=2)
    result = run_step5(payload)

    assert result["rag_status"] == "success"
    assert result["chunks_indexed"] == 3
    assert len(fts.replace_calls) == 1
    doc_hash, chunks, version = fts.replace_calls[0]
    assert doc_hash == "d1"
    assert version == 2
    assert len(chunks) == 3


# ---------------------------------------------------------------------------
# Test 4: chunk_gate over-limit (plan T3.1 verbatim)
# ---------------------------------------------------------------------------


def test_run_step5_rejects_over_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3001 chunks → outcome failed chunks_over_limit, NO encode/upsert.

    We force chunk_count to 3001 by writing 3001 single-line JSONL blocks.
    chunk_gate(3001) → reject (per T2 strict-greater semantics).
    """
    _write_jsonl(tmp_path, n_blocks=3001)
    qdrant = _StubQdrant(upsert_count=999)  # would be wrong if gate didn't fire
    fts = _StubFTS()
    lock = _StubRedisLock()
    _wire_stubs(monkeypatch, qdrant=qdrant, fts=fts, lock=lock, tmp_path=tmp_path)

    payload = _make_payload(tmp_path)
    result = run_step5(payload)

    assert result["rag_status"] == "failed"
    assert result["error_code"] == "chunks_over_limit"
    # Defense in depth: gate fires BEFORE encode/upsert
    assert qdrant.upsert_calls == []
    assert fts.replace_calls == []
    # Gate fires BEFORE RedisLock acquire (cheap-rejection first)
    assert lock.acquire_calls == []


# ---------------------------------------------------------------------------
# Test 5: concurrent_skip via RedisLock
# ---------------------------------------------------------------------------


def test_run_step5_concurrent_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RedisLock.acquire returns None → concurrent_skip outcome.

    Idempotent semantics: another worker holds the lock; NOT a failure.
    rag_status='success' (matches existing pipeline semantics for
    concurrent handling — see pipeline.py idempotent-skip branch).
    """
    _write_jsonl(tmp_path, n_blocks=2)
    qdrant = _StubQdrant(upsert_count=2)
    fts = _StubFTS()
    lock = _StubRedisLock(acquire_succeeds=False)
    _wire_stubs(monkeypatch, qdrant=qdrant, fts=fts, lock=lock, tmp_path=tmp_path)

    payload = _make_payload(tmp_path)
    result = run_step5(payload)

    # Concurrent skip is success (idempotent semantics) but tagged
    # so T7 metrics + T6 audit can distinguish it.
    assert result["rag_status"] == "success"
    assert result["error_code"] == "concurrent_skip"
    # NO work done (encode/upsert skipped; lock not held)
    assert qdrant.upsert_calls == []
    assert fts.replace_calls == []
    assert lock.release_calls == [], "release called without acquire"


# ---------------------------------------------------------------------------
# Test 6: unhandled exception → failed + worker_unhandled
# ---------------------------------------------------------------------------


def test_run_step5_unhandled_exception_returns_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception escaping _step5_async → outcome failed, error_code=worker_unhandled.

    Defensive net for pebble: if ProcessExpired occurs the OS reports
    it, but if an exception escapes inside asyncio.run the worker fn
    must still return a structured outcome (pebble picks up the
    ProcessExpired separately; this path is the in-process safety net).
    """
    import ekrs_rag.services.step5_worker as worker

    def boom_qdrant() -> _StubQdrant:
        raise RuntimeError("simulated subprocess crash")

    monkeypatch.setattr(worker, "_build_qdrant", boom_qdrant)

    payload = _make_payload(tmp_path)
    result = run_step5(payload)

    assert result["rag_status"] == "failed"
    assert result["error_code"] == "worker_unhandled"
    assert "simulated subprocess crash" in result["error"]


# ---------------------------------------------------------------------------
# Test 7: picklability (pebble requirement)
# ---------------------------------------------------------------------------


def test_step5_payload_is_picklable() -> None:
    """Step5Payload + run_step5 module-level fn must be picklable.

    pebble.ProcessPool.schedule serializes args + target fn via pickle
    for subprocess dispatch. If this test fails, T4 pebble wiring
    cannot submit tasks.
    """
    payload = Step5Payload(
        trace_id="trace-x",
        doc_hash="d-x",
        version=3,
        output_path="/tmp/x",
    )
    blob = pickle.dumps(payload)
    restored = pickle.loads(blob)
    assert restored.doc_hash == "d-x"
    assert restored.version == 3

    # run_step5 is a top-level module fn (pebble requires picklable target)
    blob_fn = pickle.dumps(run_step5)
    assert pickle.loads(blob_fn) is run_step5