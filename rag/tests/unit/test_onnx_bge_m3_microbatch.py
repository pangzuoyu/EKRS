"""Unit tests for OnnxBgeM3.encode micro-batching.

Root cause (2026-08-21 wedge, doc 8b776a6e8eaae267): ``encode()`` fed the
whole document's chunks into ONE ``session.run`` call. A 1343-chunk OCR
table doc produced a [1343, ~512] batch whose attention intermediate
([1343, 16, 512, 512] float32 ≈ 22.5 GB) OOM-killed an unlimited-memory
repro container in 25 s and wedged the 20 GB-capped service (thread pool
frozen, 0% CPU, healthcheck timeouts).

Tests pin the fix: texts are encoded in sub-batches of at most
``batch_size`` (default 64, operator-tunable via ``BGE_M3_BATCH_SIZE``,
mirroring the ``BGE_M3_INTRA_OP_THREADS`` pattern in
test_onnx_bge_m3_intra_op_threads.py). The merged result must keep the
legacy shape contract: ``dense_vecs`` [N, 1024] + ``lexical_weights``
list[N] — callers (embedding_service) are unaware of sub-batching.

Session + tokenizer are mocked (same pattern as
test_onnx_bge_m3_intra_op_threads.py) so the 2.2 GB model is never loaded.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

_SEQ = 8  # tokenized seq len for every fake batch; value is irrelevant
_HID = 1024  # bge-m3 hidden size


def _build_model(tmp_path: Path) -> tuple[object, MagicMock]:
    """Instantiate OnnxBgeM3 with mocked session + tokenizer.

    Returns (model, session_mock) — tests observe ``session.run`` calls
    to verify sub-batch splitting without touching onnxruntime.
    """
    from ekrs_rag.retrieval.onnx_bge_m3 import OnnxBgeM3

    # model.onnx existence check in __init__ must pass; sparse_linear.pt
    # absent → pseudo-sparse mode (no torch needed).
    (tmp_path / "model.onnx").write_bytes(b"x")

    session = MagicMock(name="ort_session")
    rng = np.random.default_rng(42)

    def _fake_run(_outputs: object, feed: dict) -> tuple[np.ndarray, np.ndarray]:
        b = feed["input_ids"].shape[0]
        return (
            rng.random((b, _SEQ, _HID), dtype=np.float32),
            rng.random((b, _HID), dtype=np.float32),
        )

    session.run.side_effect = _fake_run

    def _fake_tokenizer(texts: list[str], **_kwargs: object) -> dict:
        b = len(texts)
        # ids in 10..109 — outside _SPECIAL_TOKEN_IDS {0,1,2,3,250001}
        ids = rng.integers(10, 110, size=(b, _SEQ)).astype(np.int64)
        return {
            "input_ids": ids,
            "attention_mask": np.ones((b, _SEQ), dtype=np.int64),
        }

    with patch(
        "onnxruntime.InferenceSession", return_value=session,
    ), patch(
        "transformers.AutoTokenizer.from_pretrained",
        return_value=MagicMock(side_effect=_fake_tokenizer),
    ):
        model = OnnxBgeM3(tmp_path)

    return model, session


def _run_batch_sizes(session: MagicMock) -> list[int]:
    """Batch sizes (input_ids.shape[0]) of each session.run call."""
    sizes = []
    for call in session.run.call_args_list:
        feed = call.args[1] if len(call.args) > 1 else call.kwargs.get("input_ids")
        if feed is not None and "input_ids" in feed:
            sizes.append(int(feed["input_ids"].shape[0]))
    return sizes


def test_encode_splits_large_batch(tmp_path: Path) -> None:
    """200 texts → ceil(200/64)=4 session.run calls (64,64,64,8);
    merged result keeps the [N, 1024] + list[N] contract."""
    model, session = _build_model(tmp_path)
    out = model.encode(["text"] * 200)

    assert session.run.call_count == 4
    assert _run_batch_sizes(session) == [64, 64, 64, 8]
    assert out["dense_vecs"].shape == (200, _HID)
    assert len(out["lexical_weights"]) == 200


def test_encode_does_not_split_small_batch(tmp_path: Path) -> None:
    """30 texts (≤ default 64) → single session.run, unchanged behavior."""
    model, session = _build_model(tmp_path)
    out = model.encode(["text"] * 30)

    assert session.run.call_count == 1
    assert _run_batch_sizes(session) == [30]
    assert out["dense_vecs"].shape == (30, _HID)


def test_encode_respects_custom_batch_size(tmp_path: Path) -> None:
    """encode(..., batch_size=10) → 10 calls for 100 texts (operator
    escape hatch / test knob without env changes)."""
    model, session = _build_model(tmp_path)
    out = model.encode(["text"] * 100, batch_size=10)

    assert session.run.call_count == 10
    assert out["dense_vecs"].shape == (100, _HID)


def test_encode_batch_size_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """BGE_M3_BATCH_SIZE=5 → 200 texts split into 40 calls. Mirrors the
    BGE_M3_INTRA_OP_THREADS operator-tunable pattern."""
    monkeypatch.setenv("BGE_M3_BATCH_SIZE", "5")
    model, session = _build_model(tmp_path)
    model.encode(["text"] * 200)

    assert session.run.call_count == 40


def test_encode_batch_size_env_garbage_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Non-numeric BGE_M3_BATCH_SIZE → default 64 with a warning (typo
    degrades gracefully instead of crashing ingest)."""
    monkeypatch.setenv("BGE_M3_BATCH_SIZE", "not-a-number")
    model, session = _build_model(tmp_path)
    model.encode(["text"] * 200)

    assert session.run.call_count == 4
