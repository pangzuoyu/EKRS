"""Unit tests for BGE_M3_INTRA_OP_THREADS env var wiring.

Phase 12 Task D+ follow-up (2026-08-20): wraps the hardcoded
``sess_opts.intra_op_num_threads = 4`` (commit 1865168) in a
configurable env var so operators can tune thread count without
code changes. Defaults to 4 (verified-stable during 745-bundle run).

Tests pin the behavior end-to-end:
- env unset → 4 (default matches current hardcoded value, zero-migration)
- env=8 → 8 (override up)
- env=1 → 1 (override down to BGEM3FlagModel parity for A/B)

We capture the ``sess_options`` kwarg passed to the mocked
``onnxruntime.InferenceSession`` — that's the load-bearing observation,
because ``OnnxBgeM3.__init__`` builds the SessionOptions object
locally and hands it to the session.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _build_onnx_bge_m3(tmp_path: Path) -> MagicMock:
    """Instantiate OnnxBgeM3 with heavy deps mocked; return the captured
    ``SessionOptions`` object.

    Mirrors the pattern in ``test_embedding_service.py::test_sparse_mode_*``:
    patch ``onnxruntime.InferenceSession`` (so the real bge-m3 model is
    never loaded) and ``transformers.AutoTokenizer.from_pretrained`` (so
    no tokenizer download). After the constructor returns, we inspect
    what was passed to ``InferenceSession`` to learn the thread setting.
    """
    from ekrs_rag.retrieval.onnx_bge_m3 import OnnxBgeM3

    # Need model.onnx so the existence check in __init__ passes.
    (tmp_path / "model.onnx").write_bytes(b"x")

    captured: dict = {}

    def _capture_inference_session(*_args: object, **kwargs: object) -> MagicMock:
        captured["sess_options"] = kwargs.get("sess_options")
        return MagicMock()

    with patch(
        "onnxruntime.InferenceSession",
        side_effect=_capture_inference_session,
    ), patch(
        "transformers.AutoTokenizer.from_pretrained",
    ):
        OnnxBgeM3(tmp_path)

    assert "sess_options" in captured, (
        "InferenceSession was not called — OnnxBgeM3.__init__ short-circuited"
    )
    return captured["sess_options"]


def test_intra_op_threads_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No env var → fall back to 4 (matches current hardcoded value at
    commit 1865168; zero-migration contract for existing deployments)."""
    monkeypatch.delenv("BGE_M3_INTRA_OP_THREADS", raising=False)
    sess_opts = _build_onnx_bge_m3(tmp_path)
    assert sess_opts.intra_op_num_threads == 4


def test_intra_op_threads_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """BGE_M3_INTRA_OP_THREADS=8 → intra_op_num_threads=8 (operator can
    scale up from the conservative default once memory headroom is
    verified on their host)."""
    monkeypatch.setenv("BGE_M3_INTRA_OP_THREADS", "8")
    sess_opts = _build_onnx_bge_m3(tmp_path)
    assert sess_opts.intra_op_num_threads == 8


def test_intra_op_threads_env_override_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """BGE_M3_INTRA_OP_THREADS=1 → intra_op_num_threads=1 (revert to
    BGEM3FlagModel default for A/B comparison or memory-constrained
    environments)."""
    monkeypatch.setenv("BGE_M3_INTRA_OP_THREADS", "1")
    sess_opts = _build_onnx_bge_m3(tmp_path)
    assert sess_opts.intra_op_num_threads == 1


def test_intra_op_threads_env_with_garbage_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Non-numeric env value → fall back to default (4) with a warning.

    Defensive against operator typos like ``BGE_M3_INTRA_OP_THREADS=auto``
    that would otherwise raise ValueError at module import time and
    prevent the service from booting. Validating at runtime (instead
    of failing fast) is the right tradeoff here because a typo in
    this knob should degrade gracefully, not crash ingest.
    """
    monkeypatch.setenv("BGE_M3_INTRA_OP_THREADS", "not-a-number")
    sess_opts = _build_onnx_bge_m3(tmp_path)
    assert sess_opts.intra_op_num_threads == 4
