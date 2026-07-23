"""Unit tests for EmbeddingService facade.

Mock OnnxBgeM3 to avoid loading real ONNX in unit tests.
Heavy integration tests in tests/integration/test_embedding_heavy.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from qdrant_client import models

from ekrs_rag.retrieval.embedding_service import (
    EmbeddingService,
    EmbeddingUnavailableError,
    EncodedVector,
)


@pytest.fixture
def mock_flag_model() -> MagicMock:
    """Mock OnnxBgeM3 with deterministic encode output."""
    mock = MagicMock()
    mock.encode.return_value = {
        "dense_vecs": [[0.1] * 1024, [0.2] * 1024],
        "lexical_weights": [
            {1: 0.5, 5: 0.3, 100: 0.1},
            {2: 0.6, 50: 0.2},
        ],
    }
    return mock


def test_encode_returns_dense_and_sparse(mock_flag_model: MagicMock, tmp_path: Path) -> None:
    """encode() returns EncodedVector list with dense (1024d) + sparse dict."""
    # Create model.onnx so the existence check in _load() passes and
    # _load_onnx_model (patched) is actually invoked. /fake/path from the
    # brief is unwritable, so we use pytest's tmp_path fixture instead.
    (tmp_path / "model.onnx").write_bytes(b"x")
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=tmp_path)
        result = svc.encode(["hello", "world"])

    assert len(result) == 2
    assert isinstance(result[0], EncodedVector)
    assert len(result[0].dense) == 1024
    assert result[0].sparse == {1: 0.5, 5: 0.3, 100: 0.1}
    assert mock_flag_model.encode.called


def test_encode_handles_empty_list(mock_flag_model: MagicMock) -> None:
    """encode([]) returns [] and does not call model."""
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=Path("/fake/path"))
        result = svc.encode([])

    assert result == []
    assert not mock_flag_model.encode.called


def test_encode_normalizes_dense(mock_flag_model: MagicMock, tmp_path: Path) -> None:
    """Encoded dense vectors are L2-normalized (FlagEmbedding behavior)."""
    (tmp_path / "model.onnx").write_bytes(b"x")
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=tmp_path)
        result = svc.encode(["text"])

    norm = sum(v * v for v in result[0].dense) ** 0.5
    # 0.1 * sqrt(1024) ≈ 3.2
    assert abs(norm - (0.1 * (1024 ** 0.5))) < 1e-6


def test_is_dummy_when_model_missing(tmp_path: Path) -> None:
    """is_dummy=True when ONNX model not present at model_dir."""
    # tmp_path is empty
    svc = EmbeddingService(model_dir=tmp_path)
    assert svc.is_dummy is True


def test_dense_size_returns_1024(tmp_path: Path) -> None:
    """dense_size property returns 1024 (bge-m3 spec)."""
    svc = EmbeddingService(model_dir=tmp_path)
    assert svc.dense_size == 1024


def test_sha256_mismatch_raises_runtime_error(tmp_path: Path) -> None:
    """SHA256 mismatch raises RuntimeError, does NOT fall back to dummy (D1)."""
    (tmp_path / "model.onnx").write_bytes(b"fake model")
    (tmp_path / "bge-m3.sha256").write_text(
        "0000000000000000000000000000000000000000000000000000000000000000  model.onnx\n"
    )

    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        EmbeddingService(model_dir=tmp_path)


def test_to_qdrant_sparse_converts_dict_format(tmp_path: Path) -> None:
    """to_qdrant_sparse converts {term_id: weight} to Qdrant format (D8)."""
    svc = EmbeddingService(model_dir=tmp_path)
    sparse = {100: 0.5, 5: 0.3, 50: 0.1}
    result = svc.to_qdrant_sparse(sparse)

    assert result == models.SparseVector(
        indices=[5, 50, 100], values=[0.3, 0.1, 0.5]
    )


def test_to_qdrant_sparse_handles_empty_dict(tmp_path: Path) -> None:
    """Empty sparse dict returns empty indices/values (D8)."""
    svc = EmbeddingService(model_dir=tmp_path)
    result = svc.to_qdrant_sparse({})
    assert result == models.SparseVector(indices=[], values=[])


def test_is_dummy_when_onnx_load_fails(tmp_path: Path) -> None:
    """If FlagEmbedding load raises, is_dummy=True (graceful fallback)."""
    (tmp_path / "model.onnx").write_bytes(b"x")
    # No sha256 file = skip check; load will fail
    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        side_effect=RuntimeError("onnx broken"),
    ):
        svc = EmbeddingService(model_dir=tmp_path)
    assert svc.is_dummy is True
