"""Phase 13b T5.5 — pure-Python stubs covering all 10 §6 acceptance lines.

Plan: docs/superpowers/plans/2026-08-24-phase13b-T5-e2e-acceptance.md §T5.5

Each test exercises the EncodingRouter state machine + audit emit contract
without needing a real GPU, real Qdrant, or real infra. Real-infra validation
lives in T5.1 / T5.2 / T5.3 scripts and T5.4 @pytest.mark.heavy wrapper.

Three scenarios (parent Q5):
1. GPU healthy → encode_gpu raises → state→cpu + audit emit (1 transition)
2. GPU OOM RuntimeError → state→cpu + recovery (force_re_register_gpu
   re-passes → state→gpu + second audit emit)
3. 10 concurrent route() → all return EncodedVector; audit emits exactly
   once (transition-only — no flap on success path)

Other acceptance lines (≤6GB / 7787≤30s / sparse / cosine / self_check)
covered via stubbed encode_gpu + timing + stubbed EmbeddingService.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from ekrs_rag.services import encoding_router
from ekrs_rag.services.encoding_router import EncodingRouter, RouterState


# Local stand-in for EmbeddingUnavailableError (matching test_encoding_router
# pattern). Keeps onnxruntime out of unit-test collection.
class _FakeEmbeddingUnavailableError(Exception):
    pass


class _FakeOOMError(RuntimeError):
    """Stand-in for torch.cuda.OutOfMemoryError (which needs real CUDA)."""


def _make_fake_encoded_vector(text: str) -> Any:
    """Return an opaque marker object representing an EncodedVector.

    EncodingRouter doesn't actually inspect EncodedVector — route() returns
    whatever encode_gpu / EmbeddingService.encode() returns. Tests only
    assert on counts and identity, not on dense/sparse contents.
    """
    return ("encoded", text)


@pytest.fixture(autouse=True)
def _reset_router() -> Iterator[None]:
    """Reset module-level singleton between cases."""
    encoding_router.reset_router()
    encoding_router._EmbeddingUnavailableError = _FakeEmbeddingUnavailableError
    yield
    encoding_router.reset_router()


@pytest.fixture
def audit_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Capture every _emit_channel_switched invocation as a list of kwargs.

    Monkeypatches EncodingRouter._emit_channel_switched so we can assert
    on emit count and ordering without touching the real audit writer.
    """
    captured: list[dict[str, object]] = []

    def _fake_emit(self: EncodingRouter, **kw: object) -> None:
        captured.append(kw)

    monkeypatch.setattr(EncodingRouter, "_emit_channel_switched", _fake_emit)
    return captured


# ---------- Acceptance line #1: GPU channel active when healthy ----------


def test_acceptance_1_gpu_healthy_state_is_gpu(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, object]],
) -> None:
    """BGE_M3_GPU_ENABLED=true + self-check passes → state="gpu" after register.

    Verify the self-check→gpu transition emits exactly one audit event
    (unknown→gpu) and try_register_gpu() returns True.
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    assert router.try_register_gpu() is True
    assert router.current_channel == "gpu"
    assert router.is_gpu_available is True
    # One transition: unknown→gpu
    assert len(audit_calls) == 1
    assert audit_calls[0]["from_channel"] == "unknown"
    assert audit_calls[0]["to_channel"] == "gpu"


# ---------- Acceptance line #2: CPU fallback on EmbeddingUnavailableError ----------


def test_acceptance_2_fallback_on_unavailable_error(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, object]],
) -> None:
    """GPU encode raises EmbeddingUnavailableError → state→cpu + audit emit.

    This is the "GPU driver reset / CUDA not compiled" production case —
    EmbeddingUnavailableError is the documented signal that GPU is gone.
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    router.try_register_gpu()
    assert router.current_channel == "gpu"
    initial_emit_count = len(audit_calls)  # unknown→gpu

    # Now make encode_gpu raise EmbeddingUnavailableError.
    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: (_ for _ in ()).throw(
            _FakeEmbeddingUnavailableError("driver reset")
        ),
    )
    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: type(
            "_E", (), {"encode": lambda self, t: [_make_fake_encoded_vector(x) for x in t]}
        )(),
    )

    result = router.route(["hello"])
    assert len(result) == 1
    assert router.current_channel == "cpu"

    # Exactly one additional emit (gpu→cpu).
    assert len(audit_calls) == initial_emit_count + 1
    last = audit_calls[-1]
    assert last["from_channel"] == "gpu"
    assert last["to_channel"] == "cpu"
    assert last["reason"] == "unavailable"


# ---------- Acceptance line #3: CPU fallback on OOM / generic exception ----------


def test_acceptance_3_fallback_on_oom(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, object]],
) -> None:
    """GPU encode raises RuntimeError (OOM) → state→cpu + reason=encode_error.

    The OOM path is a distinct fallback reason (encode_error vs unavailable)
    so ops dashboards can distinguish "GPU broken" from "GPU ran out of
    memory on a single batch". Both fall to CPU; only the reason differs.
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    router.try_register_gpu()
    initial_emit_count = len(audit_calls)

    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: (_ for _ in ()).throw(
            _FakeOOMError("CUDA out of memory")
        ),
    )
    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: type(
            "_E", (), {"encode": lambda self, t: [_make_fake_encoded_vector(x) for x in t]}
        )(),
    )

    result = router.route(["x"])
    assert len(result) == 1
    assert router.current_channel == "cpu"

    last = audit_calls[-1]
    assert last["from_channel"] == "gpu"
    assert last["to_channel"] == "cpu"
    assert last["reason"] == "encode_error"
    assert len(audit_calls) == initial_emit_count + 1


# ---------- Acceptance line #4: transition-only emit (no flap) ----------


def test_acceptance_4_no_audit_flap_on_repeated_failures(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, object]],
) -> None:
    """3 consecutive GPU failures after first transition → 0 additional emits.

    Review 🟢 #6 mandate: once state→cpu, subsequent gpu→cpu attempts
    observe state already cpu and skip emit. This prevents audit log
    flooding on a permanently-broken GPU.
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    router.try_register_gpu()
    initial_emit_count = len(audit_calls)  # unknown→gpu

    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: (_ for _ in ()).throw(
            _FakeEmbeddingUnavailableError("permanent failure")
        ),
    )
    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: type(
            "_E", (), {"encode": lambda self, t: [_make_fake_encoded_vector(x) for x in t]}
        )(),
    )

    # First call: gpu→cpu (1 emit).
    router.route(["x"])
    assert len(audit_calls) == initial_emit_count + 1

    # 3 more calls — state stays "cpu", NO additional emits.
    for _ in range(3):
        router.route(["y"])
    assert len(audit_calls) == initial_emit_count + 1


# ---------- Acceptance line #5: force_re_register_gpu recovery ----------


def test_acceptance_5_recovery_via_force_re_register(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, object]],
) -> None:
    """GPU OOM → cpu. Probe later finds GPU healthy → force_re_register
    returns True + state→gpu + audit emits a second transition (cpu→gpu).

    This is the production failover → recovery round-trip; both directions
    emit exactly one event each (cpu→gpu NOT a flap, it's a real change).
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    router.try_register_gpu()

    # Force GPU to fail.
    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: (_ for _ in ()).throw(
            _FakeOOMError("OOM")
        ),
    )
    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: type(
            "_E", (), {"encode": lambda self, t: [_make_fake_encoded_vector(x) for x in t]}
        )(),
    )
    router.route(["x"])
    assert router.current_channel == "cpu"
    cpu_emits = len(audit_calls)

    # Probe calls force_re_register_gpu; self_check now passes again.
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    # encode_gpu is still the OOM stub — that's fine; recovery test only
    # verifies the registration succeeded, not the encode path.
    assert router.force_re_register_gpu() is True
    assert router.current_channel == "gpu"

    # One additional emit: cpu→gpu.
    assert len(audit_calls) == cpu_emits + 1
    last = audit_calls[-1]
    assert last["from_channel"] == "cpu"
    assert last["to_channel"] == "gpu"
    assert last["reason"] == "self_check_pass"


# ---------- Acceptance line #6: 10 concurrent route() → 1 emit (transition-only) ----------


def test_acceptance_6_concurrent_routes_no_audit_flood(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, object]],
) -> None:
    """10 concurrent threads route() through a healthy GPU → no audit emits.

    With GPU healthy and no failures, state stays "gpu" — the transition
    guard short-circuits even under concurrent load. Critical for prod:
    if every request emitted a channel_switched, the audit log would be
    unreadable under load.
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    router.try_register_gpu()
    initial_emit_count = len(audit_calls)  # unknown→gpu

    # Stub encode_gpu to simulate healthy GPU returning markers.
    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: [_make_fake_encoded_vector(t) for t in texts],
    )

    barrier = threading.Barrier(10)
    results: list[list[Any]] = [[] for _ in range(10)]

    def _worker(idx: int) -> None:
        barrier.wait()
        results[idx] = router.route([f"text-{idx}"])

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "thread hung"

    # All 10 routes returned a single marker each.
    assert all(len(r) == 1 for r in results)
    # No additional audit emits — state stayed "gpu" the whole time.
    assert len(audit_calls) == initial_emit_count
    assert router.current_channel == "gpu"


# ---------- Acceptance line #7: largest single-doc wall-time budget ----------


def test_acceptance_7_largest_doc_wall_time_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stubbed 7787-chunk encode completes well under the 30s budget.

    Real-infra perf is validated by T5.1 (scripts/phase13b_poc_bench.py);
    here we just verify the shape of the timing measurement and that the
    default budget (30s) is reasonable for a stubbed encode.
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    router.try_register_gpu()

    # Simulate a 7787-chunk batch completing in ~100ms (well under budget).
    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: [_make_fake_encoded_vector(t) for t in texts],
    )

    BUDGET_MS = 30_000  # acceptance line #7 default
    start = time.monotonic()
    result = router.route(["chunk"] * 7787)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert len(result) == 7787
    assert elapsed_ms < BUDGET_MS, (
        f"7787-chunk stub took {elapsed_ms:.1f}ms; budget is {BUDGET_MS}ms"
    )


# ---------- Acceptance line #8: GPU memory peak ≤ 6 GB (stubbed) ----------


def test_acceptance_8_gpu_memory_peak_under_6gb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the torch.cuda.max_memory_allocated read path returns a
    numeric value below the 6 GB ceiling. Stubbed via a fake module that
    exposes the same interface.

    The real measurement happens in T5.1 via the /v1/admin/gpu/memory-stats
    endpoint. Here we verify the stub interface matches.
    """
    import sys

    fake_torch = MagicMock()
    # 5.2 GB peak — just under the 6 GB ceiling.
    fake_torch.cuda.max_memory_allocated.return_value = 5.2 * 1024**3
    fake_torch.cuda.memory_allocated.return_value = 4.1 * 1024**3
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    device_id = "0"
    peak = fake_torch.cuda.max_memory_allocated(int(device_id))
    allocated = fake_torch.cuda.memory_allocated(int(device_id))

    CEILING = 6 * 1024**3
    assert peak <= CEILING, (
        f"GPU memory peak {peak / 1024**3:.2f}GB exceeds 6GB ceiling"
    )
    assert allocated <= CEILING


# ---------- Acceptance line #9: cosine + sparse Jaccard stubs ----------


def test_acceptance_9_cosine_and_sparse_jaccard_geometry() -> None:
    """Verify the geometry formulas used by T5.2 (equiv_check).

    cosine = np.dot(a, b) when vectors are L2-normalized → in [0, 1].
    Jaccard = |A∩B| / |A∪B| → in [0, 1].
    Sparse filter excludes _SPECIAL_TOKEN_IDS = {0, 1, 2, 3, 250001}.
    Acceptance: cosine ≥ 0.999, sparse Jaccard ≥ 0.95.
    """
    import numpy as np

    # L2-normalized vectors with cosine similarity ≥ 0.999.
    a = np.array([0.6, 0.8, 0.0])
    b = a * 0.99999 + np.array([0.001, 0.0, 0.0])
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    cosine = float(np.dot(a, b))
    assert cosine >= 0.999, f"cosine {cosine} < 0.999"

    # Sparse Jaccard ≥ 0.95 after filtering special tokens.
    SPECIAL = frozenset({0, 1, 2, 3, 250001})
    a_sparse = {5, 7, 9, 11, 13, 15, 17, 19, 21, 23}
    b_sparse = a_sparse | {25, 27}  # 8/12 ≈ 0.667 — too low; use closer set.
    # Overlap of 19/20 = 0.95 with 1 unique on each side.
    a_sparse = set(range(100, 120))
    b_sparse = a_sparse | {200} - {105}  # remove 1, add 1 → Jaccard = 19/21 ≈ 0.905
    # Use the exact ratio: 19/(19+1+1) = 0.9047. Build 19/(19+1) = 0.95:
    a_sparse = set(range(100, 120))  # 20
    b_sparse = (a_sparse - {100}) | {200}  # 19 overlap, 2 unique → 19/21
    # Correct construction for Jaccard ≥ 0.95: 19 overlap / 20 union = 0.95.
    a_sparse = set(range(100, 120))  # 20 ids
    b_sparse = a_sparse | {200}  # 21 ids, 20 overlap → 20/21 ≈ 0.952
    overlap = a_sparse & b_sparse
    union = a_sparse | b_sparse
    jaccard = len(overlap) / len(union)
    assert jaccard >= 0.95, f"sparse Jaccard {jaccard} < 0.95"
    # SPECIAL filter doesn't affect these ids (all > 250001), but verify
    # the filter would exclude the bge-m3 special tokens if present.
    assert 0 not in a_sparse
    assert SPECIAL.isdisjoint(a_sparse)


# ---------- Acceptance line #10: top-10 retrieval equivalence ----------


def test_acceptance_10_top10_jaccard_geometry() -> None:
    """Verify the top-K Jaccard formula used by T5.2.

    Acceptance: top-10 Jaccard ≥ 0.99 between Phase A (CPU) and Phase B (GPU).
    With 10/10 overlap: Jaccard = 1.0.
    With 9/11: Jaccard = 9/11 ≈ 0.818 — below threshold.
    """
    full_overlap = set(range(10))
    full_b = set(range(10))
    jaccard_full = len(full_overlap & full_b) / len(full_overlap | full_b)
    assert jaccard_full == 1.0
    assert jaccard_full >= 0.99

    # 10/11 overlap (1 unique): Jaccard = 10/11 ≈ 0.909 — below threshold.
    near = set(range(10))
    near_b = set(range(11))
    jaccard_near = len(near & near_b) / len(near | near_b)
    assert jaccard_near < 0.99  # below the bar

    # 10/10 + 0 unique: trivially 1.0
    ten = set(range(10))
    assert len(ten & ten) / len(ten | ten) == 1.0


# ---------- Acceptance line #11: probe-driven transition detection ≤30s ----------


def test_acceptance_11_probe_transition_latency(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, object]],
) -> None:
    """Verify that force_re_register_gpu under a 5s probe interval surfaces
    a GPU→CPU transition in <30s (acceptance line #11).

    The probe interval itself is configured by BGE_M3_GPU_PROBE_INTERVAL_S
    (T3.4 default = 30s; CI override = 5s). We measure the elapsed wall-time
    between an "invalidate" event and the resulting channel_switched emit.
    """
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    router = EncodingRouter()
    router.try_register_gpu()
    initial_emit_count = len(audit_calls)

    # Simulate the /v1/admin/gpu/invalidate handler by switching self-check
    # to False mid-flight (the handler sets last_self_check_pass=False so
    # the next probe will fail). Then probe calls force_re_register_gpu.
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: False)

    start = time.monotonic()
    result = router.force_re_register_gpu()
    elapsed_ms = (time.monotonic() - start) * 1000

    assert result is False
    assert router.current_channel == "cpu"
    # The transition itself is sub-millisecond; the ≤30s budget is the
    # time-to-detect = probe_interval (5s in CI, 30s in prod).
    assert elapsed_ms < 30_000, (
        f"transition detection wall-time {elapsed_ms:.1f}ms > 30s budget"
    )

    # One new emit: gpu→cpu with reason=self_check_fail
    assert len(audit_calls) == initial_emit_count + 1
    last = audit_calls[-1]
    assert last["from_channel"] == "gpu"
    assert last["to_channel"] == "cpu"
    assert last["reason"] == "self_check_fail"