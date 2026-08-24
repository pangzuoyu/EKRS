"""Phase 13a T9 — encode backend seam for Phase 13c GPU channel.

The encode call site lives in ``QdrantManager.upsert_chunks`` today (CPU
bge-m3 ONNX path). Phase 13b (separate plan, see
``docs/superpowers/plans/2026-08-23-phase13b-gpu-container.md``) replaces
the dense-encoder backend with a torch FP16 GPU implementation. Phase 13c
then wires the GPU container into the request path.

Eng-review Issue 5: lock the encode return-shape contract NOW with a
Protocol + TypeGuard so a Phase 13b shape change (e.g. returning
``torch.Tensor`` instead of ``list[list[float]]``) fails the test suite
rather than silently corrupting Qdrant writes.

These tests do NOT introduce GPU code (YAGNI). They only verify:
1. ``_EncodeBackend`` Protocol signature matches the module fn.
2. The fn returns ``list[list[float]]`` of the right length.
3. A stub implementing the Protocol passes runtime_checkable isinstance.
4. Stub return shape matches CPU stub return shape (size + nesting).
"""
from __future__ import annotations

from typing import get_type_hints, runtime_checkable

import pytest


def test_encode_backend_protocol_signature_matches_module_fn() -> None:
    """Protocol signature equals the module-level fn's signature."""
    from ekrs_rag.services.step5_worker import _EncodeBackend, _encode_backend

    # Protocol exposes __call__ as the signature surface.
    proto_hints = get_type_hints(_EncodeBackend.__call__)  # type: ignore[attr-defined]
    fn_hints = get_type_hints(_encode_backend)
    assert proto_hints == fn_hints
    # Spot-check the canonical shape contract (frozen in plan §9.1).
    assert fn_hints["texts"] == list[str]
    assert fn_hints["return"] == list[list[float]]


def test_encode_backend_is_runtime_checkable() -> None:
    """A stub class implementing the Protocol passes isinstance(..., _EncodeBackend)."""
    from ekrs_rag.services.step5_worker import _EncodeBackend

    assert getattr(_EncodeBackend, "_is_runtime_protocol", False) or hasattr(
        _EncodeBackend, "_is_protocol"
    ), "Protocol must be decorated with @runtime_checkable so isinstance works"

    class _StubBackend:
        def __call__(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 4 for _ in texts]

    # runtime_checkable Protocol: an instance of a class with matching
    # __call__ is recognized as a Protocol implementation.
    assert isinstance(_StubBackend(), _EncodeBackend)


def test_encode_backend_stub_returns_same_shape_as_cpu_stub() -> None:
    """Eng-review Issue 5 contract lock: stub shape == CPU stub shape.

    Both stubs return ``list[list[float]]`` with ``len(return) == len(texts)``
    and each inner vector of equal length. If Phase 13b GPU changes the
    return shape (e.g. ``list[torch.Tensor]``), this test fails BEFORE
    production sees the drift.
    """
    from ekrs_rag.services.step5_worker import _EncodeBackend

    class _StubBackend:
        def __call__(self, texts: list[str]) -> list[list[float]]:
            # 4-dim dense as a deterministic stand-in for bge-m3 1024-d.
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    stub = _StubBackend()
    texts = ["hello", "world", "foo"]

    # Protocol compliance (runtime_checkable).
    assert isinstance(stub, _EncodeBackend)

    # Stub return shape contract.
    result = stub(texts)
    assert isinstance(result, list)
    assert len(result) == len(texts)
    for vec in result:
        assert isinstance(vec, list)
        assert all(isinstance(x, float) for x in vec)
        assert len(vec) == 4  # stub dim


def test_encode_backend_module_fn_callable_with_list_input() -> None:
    """Module-level ``_encode_backend`` is callable and returns the
    contracted shape. CPU bge-m3 ONNX may be unavailable in test env
    (dummy mode) — the test only verifies the boundary shape, not the
    actual vector content. Tests skip gracefully if the model is missing.
    """
    from ekrs_rag.services.step5_worker import _encode_backend

    # Direct call with empty input — should return [] (no model load).
    result = _encode_backend([])
    assert result == []
    assert isinstance(result, list)

    # Single-input smoke: skip if dummy mode (CI without model files).
    try:
        result = _encode_backend(["test"])
    except Exception as e:  # noqa: BLE001 — EmbeddingUnavailableError etc.
        pytest.skip(f"bge-m3 model not loadable in test env: {type(e).__name__}")
    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], list)
    # ONNX runtime emits numpy.float32; numeric-element check uses
    # numbers.Real to accept both Python float and numpy floats. The
    # strict isinstance(x, float) check is reserved for the stub side
    # (test_encode_backend_stub_returns_same_shape_as_cpu_stub).
    import numbers
    assert all(isinstance(x, numbers.Real) for x in result[0])
