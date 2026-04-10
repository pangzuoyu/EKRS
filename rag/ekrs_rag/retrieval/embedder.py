"""BGESmall ONNX embedder for EKRS RAG.

Loads bge-small-en-v1.5 ONNX model from rag/models/ directory.
Falls back to dummy zero vectors when model files are absent (CI/CI without model download).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Model configuration
MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "bge-small-en-v1.5"
ONNX_MODEL_PATH = MODEL_DIR / "onnx" / "model.onnx"
VECTOR_SIZE = 384  # bge-small-en-v1.5 output dimension

# Try importing onnxruntime and transformers
try:
    import onnxruntime as ort

    ONNXRUNTIME_AVAILABLE = True
except ImportError:
    ort = None  # type: ignore[assignment]
    ONNXRUNTIME_AVAILABLE = False
    logger.warning("onnxruntime not available, using dummy embedder")

try:
    from transformers import AutoTokenizer

    TRANSFORMERS_AVAILABLE = True
except ImportError:
    AutoTokenizer = None  # type: ignore[assignment]
    TRANSFORMERS_AVAILABLE = False
    logger.warning("transformers not available, using dummy embedder")


class BGESmallEmbedder:
    """ONNX embedder for bge-small-en-v1.5.

    Encodes texts into 384-dimensional dense vectors using L2-normalized embeddings.
    Falls back to dummy zero vectors when model files are absent.
    """

    def __init__(self) -> None:
        self._session: Optional["ort.InferenceSession"] = None
        self._tokenizer: Optional["AutoTokenizer"] = None
        self._dummy_mode: bool = False

        self._model_load()

    def _model_load(self) -> None:
        """Load ONNX model and tokenizer, or fall back to dummy mode."""
        if not ONNXRUNTIME_AVAILABLE or not TRANSFORMERS_AVAILABLE:
            logger.warning("Dependencies unavailable, using dummy embedder")
            self._dummy_mode = True
            self._warmup()
            return

        if not ONNX_MODEL_PATH.exists():
            logger.warning(
                "ONNX model not found at %s, using dummy embedder",
                ONNX_MODEL_PATH,
            )
            self._dummy_mode = True
            self._warmup()
            return

        try:
            # Load tokenizer
            tokenizer_path = str(MODEL_DIR / "onnx")
            self._tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)

            # Load ONNX model
            sess_options = ort.SessionOptions()  # type: ignore[union-attr]
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL  # type: ignore[union-attr]
            self._session = ort.InferenceSession(  # type: ignore[union-attr]
                str(ONNX_MODEL_PATH),
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )

            # Warmup
            self._warmup()
            logger.info("Loaded bge-small-en-v1.5 ONNX model from %s", ONNX_MODEL_PATH)

        except Exception as e:
            logger.warning(
                "Failed to load ONNX model: %s, using dummy embedder",
                e,
            )
            self._dummy_mode = True
            self._warmup()

    def _warmup(self) -> None:
        """Run one dummy forward pass to warm up the model."""
        start = time.perf_counter()
        _ = self.encode(["warmup"])
        duration = time.perf_counter() - start
        if self._dummy_mode:
            logger.info("Dummy embedder warmup completed in %.3f s", duration)
        else:
            logger.info("Model warmup completed in %.3f s", duration)

    def _l2_normalize(self, vectors: list[list[float]]) -> list[list[float]]:
        """L2 normalize each vector."""
        normalized = []
        for vec in vectors:
            norm = sum(v * v for v in vec) ** 0.5
            if norm > 0:
                normalized.append([v / norm for v in vec])
            else:
                normalized.append(vec)
        return normalized

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode texts into 384-dimensional dense vectors.

        Args:
            texts: List of texts to encode.

        Returns:
            List of 384-dimensional L2-normalized dense vectors.
        """
        if self._dummy_mode:
            return [[0.0] * VECTOR_SIZE for _ in texts]

        # Tokenize
        inputs = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="np",
        )

        # Run inference
        input_names = [inp.name for inp in self._session.get_inputs()]
        onnx_inputs = {
            name: inputs[name]
            for name in input_names
            if name in inputs
        }

        outputs = self._session.run(None, onnx_inputs)
        embeddings = outputs[0].tolist()

        # L2 normalize
        return self._l2_normalize(embeddings)

    @property
    def vector_size(self) -> int:
        """Return the embedding vector size (384 for bge-small-en-v1.5)."""
        return VECTOR_SIZE
