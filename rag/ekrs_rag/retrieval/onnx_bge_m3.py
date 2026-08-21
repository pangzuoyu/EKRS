"""OnnxBgeM3 — onnxruntime-based loader for the bge-m3 ONNX export.

Background
----------
The bge-m3 ONNX model in ``rag/models/bge-m3/`` is the vanilla
``XLMRobertaModel`` export from HuggingFace (BAAI/bge-m3). Its inputs
and outputs are::

    inputs:
        input_ids:        int64 [batch, seq_len]
        attention_mask:   int64 [batch, seq_len]
    outputs:
        token_embeddings:     float [batch, seq_len, 1024]
        sentence_embedding:   float [batch, 1024]

Sparse head (optional BAAI-learned weights)
-------------------------------------------
BAAI's ``FlagEmbedding.BGEM3FlagModel`` adds a small learned head
(``nn.Linear(1024, 1)``) on top of the encoder to produce per-token
sparse weights via ``relu(W_lex @ token_emb + b)``. BAAI publishes
those weights separately as ``sparse_linear.pt`` (3.5 KB on disk —
just the 1×1024 weight + 1-element bias). When that file is present
in the model directory, we load it and use the learned projection for
sparse weights, matching what ``BGEM3FlagModel.encode`` would produce
without needing the 2.1 GB ``pytorch_model.bin`` to be loaded.

Pseudo-sparse fallback
----------------------
If ``sparse_linear.pt`` is absent (e.g., a minimal install that only
ships the dense ONNX export), we fall back to a *self-similarity*
sparse weight ``<token_emb, sent_emb>`` per non-special token — a
well-known dense-retrieval trick (CoIL, SPLADE-style approximation).
The resulting ``{token_id: weight}`` dict is not as discriminative as
the BAAI learned projection, but it preserves lexical matching well
enough for hybrid retrieval and matches the ``{int: float}`` shape
the existing ``EncodedVector`` contract and Qdrant sparse path expect.

Interface
---------
``OnnxBgeM3.encode(texts, return_dense=True, return_sparse=True)``
returns a dict with keys ``dense_vecs`` (np.ndarray, shape [N, 1024],
L2-normalized) and ``lexical_weights`` (list of dict[int, float]).
The shape mirrors ``BGEM3FlagModel.encode`` so existing callers that
consume ``raw["dense_vecs"]`` / ``raw["lexical_weights"]`` keep
working.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Special token IDs from tokenizer_config.json — these are never content
# tokens and must be excluded from the sparse representation.
_SPECIAL_TOKEN_IDS = frozenset({0, 1, 2, 3, 250001})

# bge-m3 standard practical cap. The tokenizer advertises 8192 but most
# retrieval scenarios clip to 512 (matches BAAI's bge-m3 README).
_MAX_SEQ_LEN = 512

# Default ONNX intra-op thread count for bge-m3 inference. Phase 12 Task D+
# (2026-08-19 commit 1865168) verified that 4 threads yields ~2.5–4x
# speedup over the BGEM3FlagModel default of 1 without exhausting memory
# headroom on 20-core hosts (4.78 GB steady vs 20 GB container limit).
# Operators can override at runtime via the BGE_M3_INTRA_OP_THREADS env
# var (see `.env.example`). Non-numeric / empty values fall back to this
# default with a warning so a typo degrades gracefully rather than
# crashing ingest.
_DEFAULT_INTRA_OP_THREADS = 4

# Sub-batch size cap for encode(). Root cause fix for the 2026-08-21
# wedge (doc 8b776a6e8eaae267): a 1343-chunk OCR table doc went into ONE
# session.run as [1343, ~512]; the attention intermediate
# ([1343, 16, 512, 512] float32 ≈ 22.5 GB) OOM-killed an unlimited-memory
# repro in 25 s and wedged the 20 GB-capped service (thread pool frozen,
# 0% CPU). 64 keeps every sub-batch's transient ≈ 1 GB (verified shape
# math: 64 × 16 × 512 × 512 × 4 B ≈ 1.07 GB) while amortizing per-call
# overhead fine (normal docs are ≤ 64 chunks → single call, zero change).
_DEFAULT_BATCH_SIZE = 64


def _resolve_batch_size() -> int:
    """Read BGE_M3_BATCH_SIZE from env, with safe fallback.

    Mirrors ``_resolve_intra_op_threads``: unset/empty → default (64),
    positive int → that value, anything else → default with a warning
    (operator typo degrades gracefully instead of crashing ingest).
    Read at ``__init__`` time so a restart picks up changes.
    """
    raw = os.environ.get("BGE_M3_BATCH_SIZE", "").strip()
    if not raw:
        return _DEFAULT_BATCH_SIZE
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "BGE_M3_BATCH_SIZE=%r is not a valid integer; "
            "falling back to default %d",
            raw, _DEFAULT_BATCH_SIZE,
        )
        return _DEFAULT_BATCH_SIZE
    if n < 1:
        logger.warning(
            "BGE_M3_BATCH_SIZE=%d is < 1; clamping to 1", n,
        )
        return 1
    return n


def _resolve_intra_op_threads() -> int:
    """Read BGE_M3_INTRA_OP_THREADS from env, with safe fallback.

    Returns:
        int: thread count to feed into ``sess_opts.intra_op_num_threads``.

    Behavior:
        - Env unset / empty / whitespace → ``_DEFAULT_INTRA_OP_THREADS`` (4)
        - Env set to a positive integer → that value
        - Env set to a non-numeric value → ``_DEFAULT_INTRA_OP_THREADS``
          with a warning (operator typo shouldn't crash ingest)

    Read at ``__init__`` time so operators can change the value between
    container restarts without rebuilding the image. Not exposed as a
    constructor argument because the only consumer is this class.
    """
    raw = os.environ.get("BGE_M3_INTRA_OP_THREADS", "").strip()
    if not raw:
        return _DEFAULT_INTRA_OP_THREADS
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "BGE_M3_INTRA_OP_THREADS=%r is not a valid integer; "
            "falling back to default %d",
            raw, _DEFAULT_INTRA_OP_THREADS,
        )
        return _DEFAULT_INTRA_OP_THREADS
    if n < 1:
        logger.warning(
            "BGE_M3_INTRA_OP_THREADS=%d is < 1; clamping to 1",
            n,
        )
        return 1
    return n


class OnnxBgeM3:
    """onnxruntime wrapper around the bge-m3 ONNX export."""

    def __init__(self, model_dir: Path) -> None:
        try:
            import onnxruntime as ort  # noqa: WPS433 — lazy import (heavy)
        except ImportError as e:
            raise ImportError(
                "onnxruntime is required for OnnxBgeM3; install "
                "onnxruntime>=1.15,<1.18."
            ) from e

        # Tokenizer is optional at module-import time; only required when
        # this class is instantiated. transformers pulls in torch as a
        # dependency, which is heavy — keep it lazy.
        from transformers import AutoTokenizer  # noqa: WPS433

        onnx_path = Path(model_dir) / "model.onnx"
        if not onnx_path.exists():
            raise FileNotFoundError(f"ONNX model not found at {onnx_path}")

        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Phase 12 Task D+ optimization (2026-08-19): bump from 1 → 4 to use
        # multiple cores during bge-m3 inference. The 1-thread default
        # matches BGEM3FlagModel but wastes 19 of 20 cores on this host.
        # 4 is conservative (memory safety headroom); can scale to 8 later
        # if memory + wedge rate stay acceptable. Verified 2026-08-19 on
        # Task D 745-bundle run: 30-chunk bundle 19.8s → ~7s (target).
        # Phase 12 Task D+ follow-up (2026-08-20): the value is now sourced
        # from BGE_M3_INTRA_OP_THREADS so operators can tune without a
        # code change. Default 4 (no env) preserves commit-1865168 behavior.
        sess_opts.intra_op_num_threads = _resolve_intra_op_threads()
        self._session = ort.InferenceSession(
            str(onnx_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        # Micro-batch cap — see _DEFAULT_BATCH_SIZE docstring for the
        # wedge root cause. Env-tunable (BGE_M3_BATCH_SIZE).
        self._batch_size = _resolve_batch_size()
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(model_dir), use_fast=True
        )
        self._model_dir = Path(model_dir)
        # BAAI's learned per-token sparse projection (Linear(1024, 1)).
        # Optional — when missing, we fall back to pseudo-sparse.
        self._sparse_weight: np.ndarray | None = None
        self._sparse_bias: np.ndarray | None = None
        self._sparse_mode: str = "pseudo"
        sparse_pt = self._model_dir / "sparse_linear.pt"
        if sparse_pt.exists():
            try:
                import torch  # noqa: WPS433 — lazy import (heavy)
                sd = torch.load(sparse_pt, map_location="cpu", weights_only=True)
                w = sd["weight"].to(torch.float32).cpu().numpy()  # [1, 1024]
                b = sd["bias"].to(torch.float32).cpu().numpy()    # [1]
                if w.shape == (1, 1024):
                    self._sparse_weight = w
                    self._sparse_bias = b
                    self._sparse_mode = "learned"
                    logger.info(
                        "OnnxBgeM3 learned sparse head loaded from %s", sparse_pt
                    )
                else:
                    logger.warning(
                        "sparse_linear.pt has unexpected weight shape %s; "
                        "falling back to pseudo-sparse", w.shape
                    )
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to load sparse_linear.pt (%s); falling back to "
                    "pseudo-sparse", e
                )
        logger.info(
            "OnnxBgeM3 loaded from %s (sparse_mode=%s)",
            self._model_dir, self._sparse_mode,
        )

    def encode(
        self,
        texts: list[str],
        return_dense: bool = True,
        return_sparse: bool = True,
        batch_size: int | None = None,
    ) -> dict:
        """Encode texts to dense (1024d) + pseudo-sparse (token-importance) vectors.

        Texts are encoded in sub-batches of at most ``batch_size`` texts
        (default 64, env ``BGE_M3_BATCH_SIZE``) so a single pathological
        document can never blow up the session.run batch shape — see
        ``_DEFAULT_BATCH_SIZE`` for the wedge this prevents. Sub-batching
        is invisible to callers: the merged result keeps the exact legacy
        contract.

        Args:
            texts: list of input strings. Empty list returns an empty result.
            return_dense: include ``dense_vecs`` (L2-normalized, 1024d).
            return_sparse: include ``lexical_weights`` (per-token dict).
            batch_size: override the sub-batch cap for this call;
                ``None``/``< 1`` falls back to the ``__init__``-resolved
                default (operator escape hatch, also used by tests).

        Returns:
            Dict with optional ``dense_vecs`` (np.ndarray) and ``lexical_weights``
            (list[dict[int, float]]) keys, mirroring BGEM3FlagModel.encode.
        """
        if not texts:
            return {"dense_vecs": np.zeros((0, 1024), dtype=np.float32), "lexical_weights": []}

        eff_batch = batch_size if batch_size is not None and batch_size >= 1 else self._batch_size

        dense_parts: list[np.ndarray] = []
        lexical_all: list[dict[int, float]] = []
        for i in range(0, len(texts), eff_batch):
            d, lex = self._encode_batch(
                texts[i:i + eff_batch], return_dense=return_dense,
                return_sparse=return_sparse,
            )
            if d is not None:
                dense_parts.append(d)
            lexical_all.extend(lex)

        result: dict = {}
        if return_dense:
            if dense_parts:
                result["dense_vecs"] = np.concatenate(dense_parts, axis=0)
            else:
                result["dense_vecs"] = np.zeros((0, 1024), dtype=np.float32)
        if return_sparse:
            result["lexical_weights"] = lexical_all
        return result

    def _encode_batch(
        self, texts: list[str],
        return_dense: bool, return_sparse: bool,
    ) -> tuple[np.ndarray | None, list[dict[int, float]]]:
        """Tokenize + run one sub-batch; return (dense_rows, lexical_rows).

        ``dense_rows`` is ``None`` when ``return_dense`` is falsey. Output
        row order matches ``texts`` order — encode() concatenates slices.
        """
        enc = self._tokenizer(
            list(texts),
            return_tensors="np",
            padding=True,
            truncation=True,
            max_length=_MAX_SEQ_LEN,
        )
        input_ids = enc["input_ids"]
        attention_mask = enc["attention_mask"]

        token_embeddings, sentence_embedding = self._session.run(
            None,
            {"input_ids": input_ids, "attention_mask": attention_mask},
        )

        dense_rows: np.ndarray | None = None
        if return_dense:
            # L2-normalize so Qdrant's COSINE distance is equivalent to
            # inner-product on dense vectors (matches BGEM3FlagModel output).
            norms = np.linalg.norm(sentence_embedding, axis=-1, keepdims=True)
            dense_rows = sentence_embedding / np.clip(norms, 1e-9, None)

        lexical: list[dict[int, float]] = []
        if return_sparse:
            if self._sparse_mode == "learned":
                # _sparse_mode == "learned" implies weight & bias are loaded;
                # narrow for mypy.
                assert self._sparse_weight is not None
                assert self._sparse_bias is not None
                # BAAI learned projection: relu(W_lex @ token_emb + b).
                # token_embeddings [batch, seq, 1024]; sparse_weight [1, 1024].
                # einsum gives [batch, seq] (sum over the singleton h-axis).
                importance = np.einsum(
                    "bsh,kh->bs",
                    token_embeddings.astype(np.float32, copy=False),
                    self._sparse_weight,
                ) + self._sparse_bias
            else:
                # Self-similarity importance: <token_emb, sent_emb> per token,
                # masked to non-padded, non-special positions.
                importance = (token_embeddings * sentence_embedding[:, None, :]).sum(axis=-1)
            importance = np.clip(importance, 0.0, None)  # relu; sparse weights must be ≥0
            is_special = np.isin(input_ids, list(_SPECIAL_TOKEN_IDS))
            keep = (attention_mask.astype(bool)) & (~is_special)

            for row_ids, row_scores, row_keep in zip(input_ids, importance, keep):
                weights: dict[int, float] = {}
                for tok_id, score in zip(row_ids[row_keep], row_scores[row_keep]):
                    weights[int(tok_id)] = float(score)
                lexical.append(weights)

        return dense_rows, lexical

    @property
    def sparse_mode(self) -> str:
        """Sparse computation mode: ``learned`` (BAAI W_lex loaded) or ``pseudo``."""
        return self._sparse_mode