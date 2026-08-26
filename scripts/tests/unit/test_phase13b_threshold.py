"""Unit tests for Phase 13c T4 bench threshold dynamic logic.

Pre-13c: ``phase13b_poc_bench.py:279`` ``env_int("T5_PHASE_B_MIN_CHUNKS", 7787)``
→ 28-corpus total 3618 chunks永远 < 7787, 误报 fail。

Phase 13c T4 决议:
1. ``T5_PHASE_B_MIN_CHUNKS=0`` → 关闭阈值 (不 fail)
2. explicit env (正整数) → 严格阈值 (维持原行为)
3. 不设 env → ``int(corpus_total_blocks * 0.9)`` → **warning 但 exit 0** (验证 GPU 路径,
   不验证 chunker)
4. 新 env ``T5_PHASE_B_MIN_CHUNKS_STRICT=true`` → 低于阈值 hard fail (生产预发布 gate)

Verify 4 paths: env override / corpus-derived / disabled / STRICT.
"""

import importlib
import sys
from pathlib import Path

import pytest


# Test target: scripts/phase13b_poc_bench.py
# Add scripts/ to sys.path so the import works without conftest path magic.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def bench_module(monkeypatch: pytest.MonkeyPatch):
    """Fresh import of phase13b_poc_bench.py with optional env overrides.

    Env overrides are passed as kwargs: env_overrides={"T5_PHASE_B_MIN_CHUNKS": "0"}.
    """
    from phase13b_poc_bench import _check_thresholds, _resolve_chunk_threshold

    def _factory(env_overrides: dict[str, str] | None = None):
        if env_overrides:
            for k, v in env_overrides.items():
                monkeypatch.setenv(k, v)
        else:
            monkeypatch.delenv("T5_PHASE_B_MIN_CHUNKS", raising=False)
            monkeypatch.delenv("T5_PHASE_B_MIN_CHUNKS_STRICT", raising=False)
        return _check_thresholds, _resolve_chunk_threshold

    return _factory


class _FakePhaseReport:
    """Minimal PhaseReport stub — only fields _check_thresholds / threshold resolver need."""

    def __init__(self, total_chunks: int, n_failed: int = 0,
                 largest_doc_ms: float = 0.0, gpu_memory_peak_bytes: int = 0,
                 doc_outcomes: list | None = None) -> None:
        self.total_chunks = total_chunks
        self.n_failed = n_failed
        self.largest_doc_ms = largest_doc_ms
        self.gpu_memory_peak_bytes = gpu_memory_peak_bytes
        self.doc_outcomes = doc_outcomes or []


class TestResolveChunkThreshold:
    """Threshold resolution priority: 0=off, explicit=strict, default=corpus*0.9."""

    def test_explicit_zero_disables_threshold(self, bench_module):
        """``T5_PHASE_B_MIN_CHUNKS=0`` → threshold=0 (any chunks pass)."""
        _check, _resolve = bench_module({"T5_PHASE_B_MIN_CHUNKS": "0"})
        threshold, status = _resolve(corpus_total_blocks=3618, strict=False)
        assert threshold == 0
        assert status == "disabled"

    def test_explicit_positive_int_strict_threshold(self, bench_module):
        """``T5_PHASE_B_MIN_CHUNKS=5000`` → threshold=5000."""
        _check, _resolve = bench_module({"T5_PHASE_B_MIN_CHUNKS": "5000"})
        threshold, status = _resolve(corpus_total_blocks=3618, strict=False)
        assert threshold == 5000
        assert status == "explicit"

    def test_default_corpus_derived(self, bench_module):
        """No env → ``int(corpus_total_blocks * 0.9)`` → warn but pass."""
        _check, _resolve = bench_module(env_overrides=None)
        threshold, status = _resolve(corpus_total_blocks=3618, strict=False)
        assert threshold == int(3618 * 0.9)  # 3256
        assert status == "corpus_derived"

    def test_strict_flag_returns_hard_fail_status(self, bench_module):
        """``T5_PHASE_B_MIN_CHUNKS_STRICT=true`` → status='strict' even if corpus_derived."""
        _check, _resolve = bench_module(
            {"T5_PHASE_B_MIN_CHUNKS_STRICT": "true"},
        )
        threshold, status = _resolve(corpus_total_blocks=3618, strict=True)
        assert threshold == int(3618 * 0.9)
        assert status == "strict"


class TestCheckThresholdsWithResolvedThreshold:
    """``_check_thresholds`` must apply the resolved threshold (not the stale 7787 default)."""

    def test_corpus_derived_threshold_passes_28_corpus(self, bench_module, capsys):
        """28-corpus 3618 chunks > 3256 default threshold → no error, status 'pass'."""
        _check, _resolve = bench_module(env_overrides=None)
        report = _FakePhaseReport(total_chunks=3618, n_failed=0)
        errs, status = _check(report, corpus_total_blocks=3618, strict=False)
        assert errs == []
        assert status == "pass"

    def test_corpus_derived_threshold_warns_but_exits_zero_when_below(
        self, bench_module, capsys,
    ):
        """If Phase B returns fewer chunks than threshold → warning, NOT hard fail.

        Phase 13c T4: bench 默认 warning + exit 0 (验证 GPU 路径, 不验证 chunker)。
        """
        _check, _resolve = bench_module(env_overrides=None)
        report = _FakePhaseReport(total_chunks=1000, n_failed=0)
        errs, status = _check(report, corpus_total_blocks=3618, strict=False)
        # errs IS populated (so caller knows about short corpus), but status='warn'
        # — caller (main) decides exit code based on strict flag.
        assert status == "warn"
        assert len(errs) == 1
        assert "1000 < 3256" in errs[0]

    def test_strict_flag_hard_fails_below_threshold(self, bench_module):
        """``T5_PHASE_B_MIN_CHUNKS_STRICT=true`` + below threshold → status='fail'."""
        _check, _resolve = bench_module(
            {"T5_PHASE_B_MIN_CHUNKS_STRICT": "true"},
        )
        report = _FakePhaseReport(total_chunks=1000, n_failed=0)
        errs, status = _check(report, corpus_total_blocks=3618, strict=True)
        assert status == "fail"
        assert len(errs) == 1

    def test_disabled_threshold_zero_always_passes(self, bench_module):
        """``T5_PHASE_B_MIN_CHUNKS=0`` → threshold=0, any total_chunks passes."""
        _check, _resolve = bench_module({"T5_PHASE_B_MIN_CHUNKS": "0"})
        report = _FakePhaseReport(total_chunks=1, n_failed=0)
        errs, status = _check(report, corpus_total_blocks=3618, strict=False)
        assert errs == []
        assert status == "pass"

    def test_n_failed_still_hard_fails_regardless_of_threshold(self, bench_module):
        """``n_failed > 0`` → hard fail (GPU path broken, regardless of corpus size)."""
        _check, _resolve = bench_module(env_overrides=None)
        report = _FakePhaseReport(total_chunks=3618, n_failed=1)
        errs, status = _check(report, corpus_total_blocks=3618, strict=False)
        assert "failures" in errs[0]
        # n_failed is not affected by warn/strict → hard fail
        assert status == "fail"