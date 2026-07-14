"""Behavior tests for the BGE-small ONNX embedder."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ekrs_rag.retrieval import embedder as embedder_module
from ekrs_rag.retrieval.embedder import BGESmallEmbedder, VECTOR_SIZE


class _FakeTokenizer:
    def __call__(self, texts, **kwargs):
        assert texts
        assert kwargs["return_tensors"] == "np"
        return {"input_ids": [[1]], "attention_mask": [[1]], "unused": [[0]]}


class _FakeOutput:
    def tolist(self):
        return [[3.0, 4.0]]


class _FakeSession:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def get_inputs(self):
        return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

    def run(self, output_names, inputs):
        assert output_names is None
        assert set(inputs) == {"input_ids", "attention_mask"}
        return [_FakeOutput()]


def test_missing_dependencies_use_dummy_vectors(monkeypatch):
    monkeypatch.setattr(embedder_module, "ONNXRUNTIME_AVAILABLE", False)

    embedder = BGESmallEmbedder()

    assert embedder.encode(["one", "two"]) == [
        [0.0] * VECTOR_SIZE,
        [0.0] * VECTOR_SIZE,
    ]
    assert embedder.vector_size == VECTOR_SIZE


def test_missing_model_uses_dummy_vectors(monkeypatch, tmp_path):
    monkeypatch.setattr(embedder_module, "ONNXRUNTIME_AVAILABLE", True)
    monkeypatch.setattr(embedder_module, "TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(embedder_module, "ONNX_MODEL_PATH", tmp_path / "missing.onnx")

    embedder = BGESmallEmbedder()

    assert embedder._dummy_mode is True
    assert embedder.encode(["query"])[0] == [0.0] * VECTOR_SIZE


def test_model_load_failure_falls_back_to_dummy(monkeypatch, tmp_path):
    model = tmp_path / "model.onnx"
    model.touch()

    class _BrokenAutoTokenizer:
        @staticmethod
        def from_pretrained(path):
            raise RuntimeError("bad tokenizer")

    monkeypatch.setattr(embedder_module, "ONNXRUNTIME_AVAILABLE", True)
    monkeypatch.setattr(embedder_module, "TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(embedder_module, "ONNX_MODEL_PATH", model)
    monkeypatch.setattr(embedder_module, "AutoTokenizer", _BrokenAutoTokenizer)

    embedder = BGESmallEmbedder()

    assert embedder._dummy_mode is True


def test_onnx_inference_filters_inputs_and_normalizes(monkeypatch, tmp_path):
    model = tmp_path / "model.onnx"
    model.touch()

    class _AutoTokenizer:
        @staticmethod
        def from_pretrained(path):
            return _FakeTokenizer()

    session_options = SimpleNamespace(graph_optimization_level=None)
    fake_ort = SimpleNamespace(
        SessionOptions=lambda: session_options,
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL="all"),
        InferenceSession=_FakeSession,
    )

    monkeypatch.setattr(embedder_module, "ONNXRUNTIME_AVAILABLE", True)
    monkeypatch.setattr(embedder_module, "TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(embedder_module, "ONNX_MODEL_PATH", model)
    monkeypatch.setattr(embedder_module, "AutoTokenizer", _AutoTokenizer)
    monkeypatch.setattr(embedder_module, "ort", fake_ort)

    embedder = BGESmallEmbedder()
    result = embedder.encode(["query"])

    assert session_options.graph_optimization_level == "all"
    assert result[0] == pytest.approx([0.6, 0.8])


def test_l2_normalize_preserves_zero_vector(monkeypatch):
    monkeypatch.setattr(embedder_module, "ONNXRUNTIME_AVAILABLE", False)
    embedder = BGESmallEmbedder()

    assert embedder._l2_normalize([[0.0, 0.0]]) == [[0.0, 0.0]]
