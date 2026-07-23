"""EmbeddingService facade for bge-m3 (1024d dense + sparse).

Replaces the old BGESmallEmbedder (bge-small-en, 384d dense-only).
Loads the bge-m3 ONNX export directly via onnxruntime + HuggingFace
tokenizer (no FlagEmbedding dependency). The sparse weights are a
self-similarity pseudo-sparse computed from token embeddings — see
``onnx_bge_m3.py`` docstring for the rationale.

Falls back to dummy mode when model files are absent (CI without
model), but blocks upsert in dummy mode (D1) to prevent silent data
corruption.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from qdrant_client import models

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "bge-m3"
DENSE_SIZE = 1024  # bge-m3 dense vector dimension


class EmbeddingUnavailableError(RuntimeError):
    """Raised when embedding service is in dummy mode and writes are attempted."""


@dataclass
class EncodedVector:
    """Single text encoded into dense + sparse vectors."""
    dense: list[float]            # 1024-dim L2-normalized
    sparse: dict[int, float] = field(default_factory=dict)  # {term_id: weight}


def _load_onnx_model(model_dir: Path):
    """Load the bge-m3 ONNX export via onnxruntime + HF tokenizer.

    Imported lazily to keep this module importable when onnxruntime or
    transformers are not installed (e.g., lightweight unit-test runners).
    """
    try:
        from .onnx_bge_m3 import OnnxBgeM3
    except ImportError as e:
        raise ImportError(
            "onnxruntime + transformers are required for EmbeddingService. "
            "Run: pip install onnxruntime>=1.15,<1.18 transformers>=4.37"
        ) from e
    return OnnxBgeM3(model_dir)


class EmbeddingService:
    """Facade over the bge-m3 ONNX export. Single encode() returns EncodedVector list."""

    DENSE_SIZE = DENSE_SIZE

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        self._model_dir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self._model = None
        self._is_dummy = False
        self._load()

    def _load(self) -> None:
        """Load model or fall back to dummy mode."""
        onnx_path = self._model_dir / "model.onnx"
        sha_path = self._model_dir / "bge-m3.sha256"
        if not onnx_path.exists():
            logger.warning("ONNX model not found at %s, using dummy embedder", onnx_path)
            self._is_dummy = True
            return

        # D1: SHA256 verification — fail loud, do NOT fall back to dummy
        if sha_path.exists():
            self._verify_sha256(onnx_path, sha_path)
        else:
            logger.warning("No bge-m3.sha256 at %s, skipping integrity check", sha_path)

        try:
            self._model = _load_onnx_model(self._model_dir)
            logger.info("Loaded bge-m3 (ONNX) from %s", self._model_dir)
        except Exception as e:
            logger.warning("Failed to load bge-m3: %s, using dummy", e)
            self._is_dummy = True

    def _verify_sha256(self, onnx_path: Path, sha_path: Path) -> None:
        """Verify ONNX model SHA256. Raise RuntimeError on mismatch (D1)."""
        expected = None
        for line in sha_path.read_text().splitlines():
            if line.endswith(onnx_path.name):
                expected = line.split()[0]
                break
        if not expected:
            raise RuntimeError(f"No SHA256 entry for {onnx_path.name} in {sha_path}")

        actual = hashlib.sha256()
        with open(onnx_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                actual.update(chunk)
        actual_hex = actual.hexdigest()

        if actual_hex != expected:
            raise RuntimeError(
                f"SHA256 mismatch for {onnx_path.name}: "
                f"expected {expected}, got {actual_hex}"
            )

    @property
    def is_dummy(self) -> bool:
        return self._is_dummy

    @property
    def dense_size(self) -> int:
        return self.DENSE_SIZE

    def encode(self, texts: list[str]) -> list[EncodedVector]:
        """Encode texts to (dense, sparse) vectors.

        In dummy mode, returns zero vectors + empty sparse (so reads work in dev).
        Callers must check is_dummy before allowing writes (D1).
        """
        if not texts:
            return []
        if self._is_dummy:
            return [EncodedVector(dense=[0.0] * self.DENSE_SIZE, sparse={}) for _ in texts]

        # _model is non-None when not in dummy mode (set by __init__), but mypy
        # can't follow that invariant through `_is_dummy`.
        assert self._model is not None, "model must be loaded when not in dummy mode"
        raw = self._model.encode(texts, return_dense=True, return_sparse=True)
        # OnnxBgeM3 returns dict with 'dense_vecs' (np.ndarray [N, 1024])
        # and 'lexical_weights' (list[dict[int, float]]), matching the
        # BGEM3FlagModel.encode shape so existing callers stay compatible.
        dense_array = raw["dense_vecs"]
        sparse_list = raw["lexical_weights"]
        return [
            EncodedVector(dense=list(d), sparse=s)
            for d, s in zip(dense_array, sparse_list)
        ]

    def to_qdrant_sparse(self, sparse: dict[int, float]) -> models.SparseVector:
        """Convert {term_id: weight} dict to Qdrant sparse format.

        Returns: SparseVector(indices=sorted(term_ids), values=[matching_weights])
        QdrantManager does not know about internal sparse format (D8).
        """
        if not sparse:
            return models.SparseVector(indices=[], values=[])
        indices = sorted(sparse.keys())
        values = [sparse[i] for i in indices]
        return models.SparseVector(indices=indices, values=values)
