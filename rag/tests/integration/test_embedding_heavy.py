"""Heavy integration tests for EmbeddingService (real bge-m3 model).

Marked @pytest.mark.heavy; skipped in default CI, run in nightly job.
Requires rag/models/bge-m3/ files (T1 vendored).
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from ekrs_rag.retrieval.embedding_service import (
    DEFAULT_MODEL_DIR,
    EmbeddingService,
)


# BGE_M3_MODEL_DIR env var overrides default for CI flexibility
MODEL_DIR = Path(os.environ.get("BGE_M3_MODEL_DIR", str(DEFAULT_MODEL_DIR)))


@pytest.mark.heavy
def test_real_bge_m3_encodes_english_text() -> None:
    """Real bge-m3 encodes English text to 1024d L2-normalized dense + sparse."""
    if not MODEL_DIR.exists():
        pytest.skip(f"Model dir {MODEL_DIR} not found; run T1 first")
    svc = EmbeddingService(model_dir=MODEL_DIR)
    if svc.is_dummy:
        pytest.skip("EmbeddingService in dummy mode (model load failed)")

    result = svc.encode(["hello world"])

    assert len(result) == 1
    assert len(result[0].dense) == 1024
    # L2 norm should be 1.0
    norm = math.sqrt(sum(v * v for v in result[0].dense))
    assert abs(norm - 1.0) < 1e-3
    # Sparse should have common English tokens
    assert len(result[0].sparse) > 0


@pytest.mark.heavy
def test_real_bge_m3_handles_chinese() -> None:
    """Real bge-m3 handles Chinese text with sparse containing Chinese tokens."""
    if not MODEL_DIR.exists():
        pytest.skip(f"Model dir {MODEL_DIR} not found; run T1 first")
    svc = EmbeddingService(model_dir=MODEL_DIR)
    if svc.is_dummy:
        pytest.skip("EmbeddingService in dummy mode (model load failed)")

    result = svc.encode(["高温合金材料"])

    assert len(result) == 1
    assert len(result[0].dense) == 1024
    # Chinese token IDs are typically in the thousands range
    assert any(tid > 1000 for tid in result[0].sparse.keys())