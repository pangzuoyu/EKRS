"""Unit tests for EmbeddingService facade.

Mock OnnxBgeM3 to avoid loading real ONNX in unit tests.
Heavy integration tests in tests/integration/test_embedding_heavy.py.
"""
from __future__ import annotations

import hashlib
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


# ---------------------------------------------------------------------------
# Learned-sparse (sparse_linear.pt) coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sparse_pt_present", [True, False])
def test_sparse_mode_reports_loaded_vs_pseudo(
    mock_flag_model: MagicMock, tmp_path: Path, sparse_pt_present: bool
) -> None:
    """OnnxBgeM3.sparse_mode is ``learned`` if sparse_linear.pt exists, else ``pseudo``.

    We mock OnnxBgeM3 to inspect its constructor behavior without loading the
    real ONNX model. The fixture's patch only intercepts the EmbeddingService
    loader, so we exercise the OnnxBgeM3 init directly here.
    """
    from ekrs_rag.retrieval.onnx_bge_m3 import OnnxBgeM3

    # Need model.onnx to exist so the existence check in __init__ passes.
    (tmp_path / "model.onnx").write_bytes(b"x")

    # Create a synthetic sparse_linear.pt (torch Linear(1024, 1) state_dict).
    if sparse_pt_present:
        import torch
        torch.save(
            {"weight": torch.zeros(1, 1024, dtype=torch.float16),
             "bias": torch.zeros(1, dtype=torch.float16)},
            tmp_path / "sparse_linear.pt",
        )

    # ort is imported lazily inside __init__, so patch the actual
    # onnxruntime module attribute that gets bound.
    with patch(
        "onnxruntime.InferenceSession"
    ), patch(
        "transformers.AutoTokenizer.from_pretrained"
    ):
        model = OnnxBgeM3(tmp_path)

    assert model.sparse_mode == ("learned" if sparse_pt_present else "pseudo")
    if sparse_pt_present:
        assert model._sparse_weight is not None and model._sparse_weight.shape == (1, 1024)
        assert model._sparse_bias is not None and model._sparse_bias.shape == (1,)
    else:
        assert model._sparse_weight is None
        assert model._sparse_bias is None


def test_sparse_mode_falls_back_on_wrong_shape(tmp_path: Path) -> None:
    """sparse_linear.pt with wrong weight shape falls back to pseudo-sparse."""
    from ekrs_rag.retrieval.onnx_bge_m3 import OnnxBgeM3

    (tmp_path / "model.onnx").write_bytes(b"x")
    import torch
    # Wrong shape — e.g. (2, 1024) — should be rejected.
    torch.save(
        {"weight": torch.zeros(2, 1024, dtype=torch.float16),
         "bias": torch.zeros(2, dtype=torch.float16)},
        tmp_path / "sparse_linear.pt",
    )

    with patch(
        "onnxruntime.InferenceSession"
    ), patch(
        "transformers.AutoTokenizer.from_pretrained"
    ):
        model = OnnxBgeM3(tmp_path)

    assert model.sparse_mode == "pseudo"
    assert model._sparse_weight is None


def test_sparse_mode_falls_back_on_corrupt_pt(tmp_path: Path) -> None:
    """sparse_linear.pt that fails to load falls back to pseudo-sparse."""
    from ekrs_rag.retrieval.onnx_bge_m3 import OnnxBgeM3

    (tmp_path / "model.onnx").write_bytes(b"x")
    # Write garbage that torch.load can't parse.
    (tmp_path / "sparse_linear.pt").write_bytes(b"not a valid torch checkpoint")

    with patch(
        "onnxruntime.InferenceSession"
    ), patch(
        "transformers.AutoTokenizer.from_pretrained"
    ):
        model = OnnxBgeM3(tmp_path)

    assert model.sparse_mode == "pseudo"


def test_sha256_verifies_sparse_linear_pt(tmp_path: Path) -> None:
    """EmbeddingService SHA256 verification covers sparse_linear.pt too (D1)."""
    onnx_bytes = b"fake onnx model bytes"
    sparse_bytes = b"fake sparse head bytes"
    (tmp_path / "model.onnx").write_bytes(onnx_bytes)
    (tmp_path / "sparse_linear.pt").write_bytes(sparse_bytes)

    onnx_sha = hashlib.sha256(onnx_bytes).hexdigest()
    sparse_sha = hashlib.sha256(sparse_bytes).hexdigest()
    # Write the CORRECT onnx sha + a TAMPERED sparse sha; expect RuntimeError.
    (tmp_path / "bge-m3.sha256").write_text(
        f"{onnx_sha}  model.onnx\n"
        "0000000000000000000000000000000000000000000000000000000000000000  sparse_linear.pt\n"
    )

    with pytest.raises(RuntimeError, match="SHA256 mismatch.*sparse_linear.pt"):
        EmbeddingService(model_dir=tmp_path)


def test_sha256_passes_when_sparse_linear_pt_correct(tmp_path: Path) -> None:
    """When SHA256 matches for both files, EmbeddingService proceeds to load."""
    onnx_bytes = b"fake onnx model bytes"
    sparse_bytes = b"fake sparse head bytes"
    (tmp_path / "model.onnx").write_bytes(onnx_bytes)
    (tmp_path / "sparse_linear.pt").write_bytes(sparse_bytes)

    onnx_sha = hashlib.sha256(onnx_bytes).hexdigest()
    sparse_sha = hashlib.sha256(sparse_bytes).hexdigest()
    (tmp_path / "bge-m3.sha256").write_text(
        f"{onnx_sha}  model.onnx\n{sparse_sha}  sparse_linear.pt\n"
    )

    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=tmp_path)

    assert svc.is_dummy is False


# ---------------------------------------------------------------------------
# EMBEDDING_MODEL_DIR env var override (Phase 8 T8-3a)
# ---------------------------------------------------------------------------


def test_resolve_model_dir_explicit_arg_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Explicit constructor arg takes precedence over env var.

    The Docker entrypoint never passes `model_dir=`, so EMBEDDING_MODEL_DIR
    is the operative signal in production. Local dev / tests that pass
    `model_dir=tmp_path` need that override to keep working.
    """
    from ekrs_rag.retrieval.embedding_service import _resolve_model_dir

    monkeypatch.setenv("EMBEDDING_MODEL_DIR", "/from/env")
    explicit = tmp_path / "explicit"
    assert _resolve_model_dir(explicit) == explicit


def test_resolve_model_dir_env_var_when_arg_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When no explicit arg, EMBEDDING_MODEL_DIR env var drives the path."""
    from ekrs_rag.retrieval.embedding_service import _resolve_model_dir

    env_path = tmp_path / "from-env"
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(env_path))
    assert _resolve_model_dir(None) == env_path


def test_resolve_model_dir_default_when_neither_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When arg is None and env is unset, fall back to DEFAULT_MODEL_DIR."""
    from ekrs_rag.retrieval.embedding_service import (
        DEFAULT_MODEL_DIR,
        _resolve_model_dir,
    )

    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    assert _resolve_model_dir(None) == DEFAULT_MODEL_DIR


def test_resolve_model_dir_treats_empty_env_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty EMBEDDING_MODEL_DIR is treated as unset (defensive against
    docker-compose env passthrough of an empty string)."""
    from ekrs_rag.retrieval.embedding_service import (
        DEFAULT_MODEL_DIR,
        _resolve_model_dir,
    )

    monkeypatch.setenv("EMBEDDING_MODEL_DIR", "   ")
    assert _resolve_model_dir(None) == DEFAULT_MODEL_DIR


def test_embedding_service_uses_env_var_model_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mock_flag_model: MagicMock,
) -> None:
    """Integration: when EMBEDDING_MODEL_DIR points at a dir with valid
    ONNX, EmbeddingService loads from there (not DEFAULT_MODEL_DIR). The
    observation is the model's _model_dir attribute must equal the env-var
    path. We populate tmp_path with model.onnx so the existence check
    passes, then construct without arg.
    """
    env_dir = tmp_path / "from-docker"
    env_dir.mkdir()
    (env_dir / "model.onnx").write_bytes(b"x")
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(env_dir))

    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService()  # no model_dir arg — env var drives

    assert svc._model_dir == env_dir


def test_embedding_service_constructor_arg_overrides_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mock_flag_model: MagicMock,
) -> None:
    """The explicit model_dir= arg still beats the env var. This is the
    safety net for tests + ad-hoc tool scripts that need to point at a
    fixture path."""
    env_dir = tmp_path / "from-docker"
    env_dir.mkdir()
    (env_dir / "model.onnx").write_bytes(b"x")
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(env_dir))

    explicit = tmp_path / "from-test-fixture"
    explicit.mkdir()
    (explicit / "model.onnx").write_bytes(b"x")

    with patch(
        "ekrs_rag.retrieval.embedding_service._load_onnx_model",
        return_value=mock_flag_model,
    ):
        svc = EmbeddingService(model_dir=explicit)

    assert svc._model_dir == explicit
