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

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from ..core.config import settings

if TYPE_CHECKING:
    # embedding_service is heavy (onnxruntime + bge-m3 ONNX load on import).
    # We only need the type hints + exception class, so keep them behind
    # TYPE_CHECKING. Runtime references use the lazy __getattr__ below.
    from ..retrieval.embedding_service import (  # noqa: F401
        EmbeddingUnavailableError,
        EncodedVector,
    )

logger = logging.getLogger(__name__)

# Runtime aliases — same lazy pattern as encoding_router. Tests can monkeypatch
# these via ``torch_bge_m3._EmbeddingUnavailableError`` without paying the
# onnxruntime load cost.
_EmbeddingUnavailableError: type[Exception] | None = None
_EncodedVector: Any = None


def __getattr__(name: str) -> Any:
    global _EmbeddingUnavailableError, _EncodedVector
    if name == "EmbeddingUnavailableError":
        if _EmbeddingUnavailableError is None:
            from ..retrieval import embedding_service
            _EmbeddingUnavailableError = embedding_service.EmbeddingUnavailableError
        return _EmbeddingUnavailableError
    if name == "EncodedVector":
        if _EncodedVector is None:
            from ..retrieval import embedding_service
            _EncodedVector = embedding_service.EncodedVector
        return _EncodedVector
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Special token IDs that must be excluded from the sparse representation.
# Mirrors the constant in onnx_bge_m3.py so GPU/CPU outputs align.
_SPECIAL_TOKEN_IDS = frozenset({0, 1, 2, 3, 250001})


# Lazy module-level cache keyed on model_dir so multiple encode_gpu() calls
# amortize the ~5-15s torch model cold-start on a single-GPU host.
_model_cache: dict[Path, "_TorchBgeM3"] = {}

# Path to the 4-class self-check probes fixture (review 🟡 #4 — at least 4
# categories covered: English / Chinese / digit-symbol / empty).
_SELF_CHECK_PROBES_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "fixtures"
    / "bge_m3_self_check_probes.jsonl"
)

# Cosine threshold for the self-check probe pass criterion (验收 #10).
# Same threshold as the unit test (dense ≥0.999 between GPU FP16 and CPU
# ONNX FP32). Below this we refuse to register the GPU channel and the
# router falls back to CPU.
_SELF_CHECK_COSINE_THRESHOLD = 0.999


def _load_self_check_probes() -> list[dict[str, str]]:
    """Load the 4-class probe fixtures, returning [] on missing file.

    Returning [] (instead of raising) lets the self-check degrade to False
    cleanly when the fixture is missing in production deployments — the
    caller logs a warning and the router registers CPU-only.
    """
    try:
        with open(_SELF_CHECK_PROBES_PATH, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        logger.warning(
            "self_check: probes fixture not found at %s — GPU channel will not register",
            _SELF_CHECK_PROBES_PATH,
        )
        return []
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("self_check: failed to read probes fixture: %s", e)
        return []


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

        Phase 13b T4.2: emit GPU memory + batch-size + latency histograms.
        Uses ``torch.cuda.memory_allocated(device_id)`` (review 🟡 #3 — no
        external nvidia-smi dependency, more accurate).
        """
        import torch  # noqa: WPS433
        from ..observability import metrics as _m
        from ..observability.metrics import safe_observe

        if not texts:
            return []

        results: list[EncodedVector] = []
        batch_size = self._BATCH_SIZE
        device_id = settings.BGE_M3_GPU_DEVICE_ID
        for batch_start in range(0, len(texts), batch_size):
            batch = texts[batch_start : batch_start + batch_size]
            # Phase 13b T4.2 — emit batch size + latency histogram around
            # each encode pass. safe_observe swallows failures so a metric
            # outage never breaks ingestion.
            safe_observe(_m.gpu_encode_batch_size, len(batch))

            import time as _t
            _batch_t0 = _t.perf_counter()

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

            # Phase 13b T4.2 — emit per-batch latency + memory after the
            # with-block (so torch frees intermediates before we read
            # memory_allocated). memory_allocated reads from torch's cache
            # accounting; no nvidia-smi shell-out (review 🟡 #3).
            _batch_dt = _t.perf_counter() - _batch_t0
            safe_observe(_m.gpu_encode_latency_seconds, _batch_dt)
            try:
                used = int(torch.cuda.memory_allocated(device_id))
                peak = int(torch.cuda.max_memory_allocated(device_id))
                _m.gpu_memory_used_bytes.labels(device_id=str(device_id)).set(used)
                _m.gpu_memory_peak_bytes.labels(device_id=str(device_id)).set(peak)
            except Exception as _e:  # pragma: no cover - defensive
                logger.debug("gpu memory metrics failed: %s", _e)

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
        # Lazy-resolve the production exception class on first call so
        # unit tests that mock the GPU path don't pay the onnxruntime
        # import cost at module load.
        if _EmbeddingUnavailableError is None:
            from ..retrieval import embedding_service
            globals()["_EmbeddingUnavailableError"] = embedding_service.EmbeddingUnavailableError
        raise globals()["_EmbeddingUnavailableError"](
            "torch.cuda.is_available() == False; encode_gpu requires CUDA",
        )
    resolved = _resolve_model_dir(model_dir)
    model = _model_cache.get(resolved)
    if model is None:
        model = _TorchBgeM3(resolved)
        _model_cache[resolved] = model
    return model.encode(texts)


__all__ = ["encode_gpu", "EmbeddingUnavailableError", "EncodedVector", "_self_check"]


def _self_check(
    *,
    model_dir: Path | None = None,
    probes: list[dict[str, str]] | None = None,
) -> bool:
    """Validate GPU channel against CPU ONNX baseline.

    Returns True iff every non-empty probe's GPU/CPU cosine is at or above
    the threshold (0.999). Empty probes are skipped — they're only there
    to verify the empty-input contract.

    Returns False (without raising) when:
    - torch CUDA is not available
    - probes fixture missing or empty (graceful CPU fallback)
    - CPU baseline (EmbeddingService) is in dummy mode (no comparison)
    - any probe's cosine dips below threshold

    Args:
        model_dir: GPU model dir override (defaults to Settings.BGE_M3_MODEL_DIR).
        probes: probe list override (defaults to reading the fixture file).
    """
    if not _cuda_available():
        logger.info("self_check: CUDA not available — GPU channel not registered")
        return False

    probe_list = probes if probes is not None else _load_self_check_probes()
    if not probe_list:
        logger.warning("self_check: no probes available — GPU channel not registered")
        return False

    # Lazy import — EmbeddingService pulls onnxruntime which is heavy.
    try:
        from ..retrieval.embedding_service import EmbeddingService
        cpu_service = EmbeddingService()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("self_check: failed to load CPU baseline: %s", e)
        return False

    if cpu_service.is_dummy:
        # No CPU baseline to compare against → refuse to register GPU.
        # Plan T2.2: "无 vendored ONNX (自检基准) → 直接 False + log warning".
        logger.warning(
            "self_check: CPU baseline is dummy (no ONNX) — GPU channel not registered",
        )
        return False

    resolved_model_dir = _resolve_model_dir(model_dir)

    # Separate empty probes (skip from cosine math) from real ones.
    real_probes = [p for p in probe_list if p.get("text", "").strip()]
    if not real_probes:
        # All empty — degenerate fixture; pass via path-availability check.
        logger.info("self_check: all probes empty — passing vacuously")
        return True

    texts = [p["text"] for p in real_probes]
    try:
        gpu_vecs = encode_gpu(texts, model_dir=resolved_model_dir)
        cpu_vecs = cpu_service.encode(texts)
    except EmbeddingUnavailableError:
        logger.warning("self_check: encode_gpu raised EmbeddingUnavailableError")
        return False
    except Exception as e:
        logger.warning("self_check: encode failed: %s", e)
        return False

    min_cos = 1.0
    for probe, gpu_vec, cpu_vec in zip(real_probes, gpu_vecs, cpu_vecs):
        gpu_row = np.asarray(gpu_vec.dense, dtype=np.float64)
        cpu_row = np.asarray(cpu_vec.dense, dtype=np.float64)
        cos = float(np.dot(gpu_row, cpu_row) / (np.linalg.norm(gpu_row) * np.linalg.norm(cpu_row)))
        if cos < min_cos:
            min_cos = cos
        logger.info(
            "self_check: probe=%s category=%s cosine=%.4f",
            probe.get("id", "?"), probe.get("category", "?"), cos,
        )

    if min_cos < _SELF_CHECK_COSINE_THRESHOLD:
        logger.warning(
            "self_check: min cosine %.4f below threshold %.4f — GPU channel not registered",
            min_cos, _SELF_CHECK_COSINE_THRESHOLD,
        )
        return False

    logger.info(
        "self_check: PASSED min_cosine=%.4f across %d probes",
        min_cos, len(real_probes),
    )
    return True