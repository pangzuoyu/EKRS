"""Phase 13b T1 — torch FP16 GPU encoder for bge-m3 (dual-head).

Plan: docs/superpowers/plans/2026-08-24-phase13b-gpu-encoder.md §T1
Spec: docs/specs/phase13-gpu-encoding-channel-spec.md v1.2

Implements ``encode_gpu(texts) -> list[EncodedVector]`` (dense 1024-d +
sparse {tok_id: weight}) for the bge-m3 torch model running in FP16 on
CUDA. Plan T1.1 review 🔴 #1 mandate: dense and sparse are computed in
the SAME batch via the learned ``sparse_linear.pt`` head — not deferred
to a follow-up call.

The T9 module-level seam in ``services/step5_worker.py:_encode_backend``
itself is unchanged by this module — T3 will rebind it to call
``encode_gpu`` when GPU is registered.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ..core.config import settings
from ..retrieval.embedding_service import (
    EmbeddingUnavailableError,
    EncodedVector,
)

logger = logging.getLogger(__name__)


# Special token IDs that must be excluded from the sparse representation.
# Mirrors the constant in onnx_bge_m3.py so GPU/CPU outputs align.
_SPECIAL_TOKEN_IDS = frozenset({0, 1, 2, 3, 250001})


# Lazy module-level cache keyed on model_dir so multiple encode_gpu() calls
# amortize the ~5-15s torch model cold-start on a single-GPU host.
_model_cache: dict[Path, "_TorchBgeM3"] = {}


class _TorchBgeM3:
    """torch FP16 GPU encoder for bge-m3 (dense CLS + sparse learned head).

    Mirrors the OnnxBgeM3 surface — ``encode(texts) -> list[EncodedVector]`` —
    so EncodingRouter (plan T3) can dispatch with one consistent contract
    across CPU/GPU backends.
    """

    DENSE_SIZE = 1024
    _MAX_SEQ_LEN = 512
    _BATCH_SIZE = 32  # plan T1.1; on OOM caller can fall back to CPU

    def __init__(self, model_dir: Path) -> None:
        self._model_dir = Path(model_dir)

        # Lazy imports — keep module importable in test environments that
        # don't need torch (matches OnnxBgeM3 pattern).
        import torch  # noqa: WPS433
        from transformers import AutoModel, AutoTokenizer  # noqa: WPS433

        device = torch.device(f"cuda:{settings.BGE_M3_GPU_DEVICE_ID}")
        self._model = (
            AutoModel.from_pretrained(
                str(self._model_dir),
                torch_dtype=torch.float16,
            )
            .to(device)
            .eval()
        )
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(self._model_dir), use_fast=True,
        )
        self._device = device

        # Learned sparse head — same sparse_linear.pt pattern as OnnxBgeM3.
        # FP32 weights for numerical stability at apply time.
        self._sparse_w: np.ndarray | None = None
        self._sparse_b: np.ndarray | None = None
        sparse_pt = self._model_dir / "sparse_linear.pt"
        if sparse_pt.exists():
            try:
                sd = torch.load(sparse_pt, map_location="cpu", weights_only=True)
                w = sd["weight"].to(torch.float32).cpu().numpy()  # [1, 1024]
                b = sd["bias"].to(torch.float32).cpu().numpy()    # [1]
                if w.shape == (1, 1024):
                    self._sparse_w = w
                    self._sparse_b = b
                    logger.info(
                        "torch_bge_m3 learned sparse head loaded from %s",
                        sparse_pt,
                    )
                else:
                    logger.warning(
                        "sparse_linear.pt has unexpected weight shape %s; "
                        "sparse will be empty", w.shape,
                    )
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to load sparse_linear.pt (%s); sparse will be empty",
                    e,
                )

    def encode(self, texts: list[str]) -> list[EncodedVector]:
        """Encode texts to (dense 1024-d, sparse {tok_id: weight}) EncodedVectors.

        Mirrors OnnxBgeM3.encode signature minus the return_dense/sparse
        flags (always returns both — plan T1.1 review 🔴 #1 mandate).
        """
        import torch  # noqa: WPS433

        if not texts:
            return []

        results: list[EncodedVector] = []
        batch_size = self._BATCH_SIZE
        for batch_start in range(0, len(texts), batch_size):
            batch = texts[batch_start : batch_start + batch_size]
            enc = self._tokenizer(
                list(batch),
                padding=True,
                truncation=True,
                max_length=self._MAX_SEQ_LEN,
                return_tensors="pt",
            ).to(self._device)

            with torch.no_grad():
                outputs = self._model(**enc)
                hidden = outputs.last_hidden_state  # [B, seq, 1024] FP16
                cls = hidden[:, 0]  # [B, 1024] CLS pooling (G1)

                # Sparse head — apply learned W_lex to all token positions.
                # einsum into FP32 weights keeps numerical parity with
                # OnnxBgeM3 (which also casts hidden→float32 before einsum).
                if self._sparse_w is not None:
                    importance = torch.einsum(
                        "bsh,kh->bs",
                        hidden.to(torch.float32),
                        torch.from_numpy(self._sparse_w).to(self._device),
                    ) + torch.from_numpy(self._sparse_b).to(self._device)
                else:
                    # Self-similarity fallback (matches OnnxBgeM3 pseudo-sparse).
                    importance = (
                        hidden.to(torch.float32)
                        * cls.unsqueeze(1).to(torch.float32)
                    ).sum(dim=-1)

                importance = torch.clamp(importance, min=0.0)  # relu

                # L2-normalize CLS for Qdrant COSINE distance.
                norms = cls.to(torch.float32).norm(p=2, dim=-1, keepdim=True).clamp(min=1e-9)
                dense = (cls.to(torch.float32) / norms).cpu().numpy()

                # Build per-row sparse dicts, filtering padded + special tokens.
                input_ids = enc["input_ids"].cpu().numpy()
                attention_mask = enc["attention_mask"].cpu().numpy()
                importance_np = importance.cpu().numpy()

            for row_idx in range(len(batch)):
                dense_list = dense[row_idx].tolist()
                sparse: dict[int, float] = {}
                for tok_id, score, mask in zip(
                    input_ids[row_idx],
                    importance_np[row_idx],
                    attention_mask[row_idx],
                ):
                    if (
                        mask
                        and int(tok_id) not in _SPECIAL_TOKEN_IDS
                        and score > 0
                    ):
                        sparse[int(tok_id)] = float(score)
                results.append(EncodedVector(dense=dense_list, sparse=sparse))

        return results


def _cuda_available() -> bool:
    """Probe torch.cuda.is_available() without forcing torch import at module load."""
    try:
        import torch  # noqa: WPS433
        return bool(torch.cuda.is_available())
    except ImportError:
        return False


def _resolve_model_dir(arg: Path | None) -> Path:
    """Resolve model_dir kwarg → Settings.BGE_M3_MODEL_DIR fallback."""
    if arg is not None:
        return Path(arg)
    return Path(settings.BGE_M3_MODEL_DIR)


def encode_gpu(
    texts: list[str],
    *,
    model_dir: Path | None = None,
) -> list[EncodedVector]:
    """Module-level encode entry point. Caches the model per model_dir.

    Raises EmbeddingUnavailableError when torch.cuda is not available
    (graceful degradation — callers can fall back to CPU EmbeddingService).

    Returns the FULL dual-head structure (dense 1024-d + sparse dict) per
    review 🔴 #1 — sparse is computed alongside dense in the same batch,
    not deferred to a follow-up call.
    """
    if not texts:
        return []
    if not _cuda_available():
        raise EmbeddingUnavailableError(
            "torch.cuda.is_available() == False; encode_gpu requires CUDA",
        )
    resolved = _resolve_model_dir(model_dir)
    model = _model_cache.get(resolved)
    if model is None:
        model = _TorchBgeM3(resolved)
        _model_cache[resolved] = model
    return model.encode(texts)


__all__ = ["encode_gpu", "EmbeddingUnavailableError", "EncodedVector"]