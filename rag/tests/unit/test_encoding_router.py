"""Phase 13b T2.3 — EncodingRouter unit tests.

Plan: docs/superpowers/plans/2026-08-24-phase13b-gpu-encoder.md §T2.3

5 RED tests covering:
- test_try_register_gpu_with_no_cuda_returns_false: monkeypatched CUDA unavailable → False
- test_try_register_gpu_idempotent: second call short-circuits
- test_route_uses_cpu_when_not_registered: state="unknown" → CPU encode path
- test_route_falls_back_to_cpu_on_gpu_failure: GPU raise → CPU + transition
- test_state_machine_transition_only_emit: 3 consecutive GPU errors → 1 channel_switched

Plus the T3 tests (review 🟢 #6 state machine):
- test_route_gpu_when_available: mocked GPU encode → GPU path
- test_state_machine_no_emit_on_same_channel: 3 GPU errors in a row → 1 emit
- test_force_re_register_resets_state
- test_route_queue_overflow_routes_to_cpu
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ekrs_rag.services import encoding_router
from ekrs_rag.services.encoding_router import EncodingRouter, RouterState


# Local stand-in for the production EmbeddingUnavailableError (which lives
# in embedding_service.py and triggers a heavy onnxruntime load). Tests inject
# this into encoding_router._EmbeddingUnavailableError so the production
# exception class never has to be resolved.
class _FakeEmbeddingUnavailableError(Exception):
    pass


# Local fixture path mirrors the production probes fixture.
PROBES_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "bge_m3_self_check_probes.jsonl"
)


@pytest.fixture(autouse=True)
def _reset_router() -> None:
    """Reset module-level singleton between cases."""
    encoding_router.reset_router()
    # Patch the lazy-resolved exception class so encode_gpu's raise path
    # is caught without triggering onnxruntime import.
    encoding_router._EmbeddingUnavailableError = _FakeEmbeddingUnavailableError
    yield
    encoding_router.reset_router()


@pytest.fixture
def sample_probes() -> list[dict[str, str]]:
    return [
        {"id": "p1", "text": "Hello world.", "category": "english_short"},
        {"id": "p2", "text": "中文 probe 温度 ≤ 80℃", "category": "chinese_long"},
        {"id": "p3", "text": "a=1.6e-3; T=80±0.5", "category": "digit_symbol"},
        {"id": "p4", "text": "", "category": "empty"},
    ]


# ---------- T2.3 self-check / registration tests ----------


def test_try_register_gpu_with_no_cuda_returns_false(
    monkeypatch: pytest.MonkeyPatch,
    sample_probes: list[dict[str, str]],
) -> None:
    """No CUDA → self_check fails → router registers cpu channel.

    The router's is_gpu_available must be False; calling try_register_gpu
    a second time is idempotent (no flip).
    """
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    router = EncodingRouter()
    result = router.try_register_gpu(probes=sample_probes)
    assert result is False
    assert router.is_gpu_available is False
    assert router.current_channel == "cpu"


def test_try_register_gpu_idempotent(sample_probes: list[dict[str, str]]) -> None:
    """Calling try_register_gpu twice with the same outcome doesn't flip state.

    Verifies the registration_attempted short-circuit so the self-check
    probe isn't re-run on every cold-start / per-task call (which would
    cost real GPU seconds for no benefit).
    """
    import torch

    with patch.object(torch.cuda, "is_available", return_value=False):
        router = EncodingRouter()
        first = router.try_register_gpu(probes=sample_probes)
        # Second call — must short-circuit (we patch self_check to assert it's not called)
        with patch.object(
            encoding_router.torch_bge_m3, "_self_check", return_value=True
        ) as mock_check:
            second = router.try_register_gpu(probes=sample_probes)
        assert first is False
        assert second is False  # Still cpu even though _self_check returned True
        mock_check.assert_not_called()


def test_route_uses_cpu_when_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State='unknown' (un-registered) routes to CPU via EmbeddingService.

    This is the default path before try_register_gpu() has been called —
    ensures the router is safe to instantiate anywhere without surprise GPU
    side-effects.
    """
    router = EncodingRouter()
    assert router.current_channel == "unknown"

    captured: dict[str, object] = {}

    class _FakeCPU:
        def encode(self, texts: list[str]) -> list[object]:
            captured["called"] = True
            captured["texts"] = texts
            return [object() for _ in texts]

    # Patch the EmbeddingService class reference inside encoding_router's
    # _encode_cpu. It does ``from ..retrieval import embedding_service`` then
    # reads the attribute — patch the module-level name (the __getattr__
    # resolver hands out the class object on demand).
    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: _FakeCPU(),
    )

    result = router.route(["hello"])
    assert captured["called"] is True
    assert captured["texts"] == ["hello"]
    assert len(result) == 1


# ---------- T3 dispatch + state machine tests ----------


def test_route_falls_back_to_cpu_on_gpu_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GPU encode raises EmbeddingUnavailableError → fall back to CPU + transition.

    Channel goes from "gpu" → "cpu" (transition emit). We patch both
    try_register_gpu to land us in "gpu" state and encode_gpu to raise.
    """
    router = EncodingRouter()
    # Pre-register as GPU by patching self_check + cuda availability.
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    assert router.try_register_gpu() is True
    assert router.is_gpu_available is True

    # Now make GPU encode raise.
    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: (_ for _ in ()).throw(
            _FakeEmbeddingUnavailableError("test gpu down")
        ),
    )

    # CPU path stub
    cpu_calls: list[list[str]] = []

    class _FakeCPU:
        def encode(self, texts: list[str]) -> list[object]:
            cpu_calls.append(texts)
            return [f"cpu_{i}" for i in range(len(texts))]

    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: _FakeCPU(),
    )

    # Audit writer mock to confirm transition-only emit
    audit_calls: list[dict[str, object]] = []

    def _fake_emit(self: EncodingRouter, **kw: object) -> None:
        audit_calls.append(kw)

    monkeypatch.setattr(EncodingRouter, "_emit_channel_switched", _fake_emit)

    result = router.route(["hello"])
    assert result == ["cpu_0"]
    assert cpu_calls == [["hello"]]
    assert router.current_channel == "cpu"
    # Exactly one audit emit for the transition.
    assert len(audit_calls) == 1
    assert audit_calls[0]["from_channel"] == "gpu"
    assert audit_calls[0]["to_channel"] == "cpu"


def test_state_machine_no_emit_on_same_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three consecutive GPU errors after first transition → 0 additional emits.

    Review 🟢 #6 mandate: state stays "cpu" after the first transition;
    subsequent gpu→cpu attempts see state == "cpu" already and don't emit.
    """
    router = EncodingRouter()
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    router.try_register_gpu()

    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda texts, **kw: (_ for _ in ()).throw(
            _FakeEmbeddingUnavailableError("test")
        ),
    )
    monkeypatch.setattr(
        encoding_router, "EmbeddingService",
        lambda: type("_E", (), {"encode": lambda self, t: [f"cpu_{i}" for i in range(len(t))]})(),
    )

    audit_calls: list[dict[str, object]] = []

    def _fake_emit(self: EncodingRouter, **kw: object) -> None:
        audit_calls.append(kw)

    monkeypatch.setattr(EncodingRouter, "_emit_channel_switched", _fake_emit)

    # First call: gpu → cpu transition (1 emit).
    router.route(["x"])
    assert len(audit_calls) == 1

    # Subsequent calls stay on cpu — NO additional emit.
    for _ in range(3):
        router.route(["y"])
    assert len(audit_calls) == 1


def test_route_gpu_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """GPU state + queue depth below threshold → encode_gpu is the path."""
    router = EncodingRouter()
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    router.try_register_gpu()

    gpu_calls: list[list[str]] = []

    def _fake_encode_gpu(texts: list[str], **kw: object) -> list[object]:
        gpu_calls.append(texts)
        return [f"gpu_{i}" for i in range(len(texts))]

    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu", _fake_encode_gpu,
    )

    # CPU should NOT be called when GPU succeeds.
    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: type("_E", (), {"encode": lambda self, t: pytest.fail("CPU called when GPU ok")})(),
    )

    result = router.route(["a", "b"])
    assert result == ["gpu_0", "gpu_1"]
    assert gpu_calls == [["a", "b"]]
    assert router.current_channel == "gpu"


def test_route_queue_overflow_routes_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """GPU state but queue depth > threshold → route to CPU (overload protection).

    State stays "gpu" — we don't transition just because of an overload
    decision; the channel stays available for the next task. This matches
    plan T3.1 ("队列深度 ≤10 → GPU; 否则 CPU").
    """
    router = EncodingRouter(queue_depth_provider=lambda: 11, max_queue_depth_for_gpu=10)
    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", lambda **kw: True)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    router.try_register_gpu()
    assert router.current_channel == "gpu"

    # GPU encode must NOT be called when overload is active.
    monkeypatch.setattr(
        encoding_router.torch_bge_m3, "encode_gpu",
        lambda *a, **kw: pytest.fail("GPU should not be called on overload"),
    )

    cpu_calls: list[list[str]] = []
    monkeypatch.setattr(
        "ekrs_rag.retrieval.embedding_service.EmbeddingService",
        lambda *a, **kw: type("_E", (), {"encode": lambda self, t: cpu_calls.append(t) or [f"cpu_{i}" for i in range(len(t))]})(),
    )

    result = router.route(["x"])
    assert result == ["cpu_0"]
    assert cpu_calls == [["x"]]
    # Channel state stays gpu (no transition — overload is not a fault).
    assert router.current_channel == "gpu"


def test_force_re_register_resets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """force_re_register_gpu clears the registration_attempted flag."""
    import torch
    router = EncodingRouter()

    # First pass — make _self_check return True regardless of cuda state,
    # but emulate the cuda patch externally so the production code path is
    # exercised end-to-end.
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    def _fake_self_check(**kw: object) -> bool:
        # Mirror the production semantics: cuda must be available.
        return bool(torch.cuda.is_available())

    monkeypatch.setattr(encoding_router.torch_bge_m3, "_self_check", _fake_self_check)

    router.try_register_gpu()
    assert router.is_gpu_available is True

    # Now disable CUDA and re-register → must transition to cpu.
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    router.force_re_register_gpu()
    assert router.is_gpu_available is False
    assert router.current_channel == "cpu"


# ---------- probes fixture shape test ----------


def test_probes_fixture_has_4_categories() -> None:
    """Plan T2.1 review 🟡 #4 — fixture covers ≥4 categories.

    This is a structural guard — if someone removes a probe, this test
    catches it. Categories must include at least: english_short, chinese,
    digit_symbol, empty.
    """
    assert PROBES_PATH.exists(), f"missing probes fixture at {PROBES_PATH}"
    rows = [
        json.loads(line)
        for line in PROBES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cats = {r["category"] for r in rows}
    required = {"english_short", "chinese_long", "digit_symbol", "empty"}
    missing = required - cats
    assert not missing, f"probes fixture missing categories: {missing}"
    # Each probe must have id + text + category
    for r in rows:
        assert "id" in r and "text" in r and "category" in r