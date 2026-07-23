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
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Special token IDs from tokenizer_config.json — these are never content
# tokens and must be excluded from the sparse representation.
_SPECIAL_TOKEN_IDS = frozenset({0, 1, 2, 3, 250001})

# bge-m3 standard practical cap. The tokenizer advertises 8192 but most
# retrieval scenarios clip to 512 (matches BAAI's bge-m3 README).
_MAX_SEQ_LEN = 512


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
        sess_opts.intra_op_num_threads = 1  # match BGEM3FlagModel default
        self._session = ort.InferenceSession(
            str(onnx_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
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
    ) -> dict:
        """Encode texts to dense (1024d) + pseudo-sparse (token-importance) vectors.

        Args:
            texts: list of input strings. Empty list returns an empty result.
            return_dense: include ``dense_vecs`` (L2-normalized, 1024d).
            return_sparse: include ``lexical_weights`` (per-token dict).

        Returns:
            Dict with optional ``dense_vecs`` (np.ndarray) and ``lexical_weights``
            (list[dict[int, float]]) keys, mirroring BGEM3FlagModel.encode.
        """
        if not texts:
            return {"dense_vecs": np.zeros((0, 1024), dtype=np.float32), "lexical_weights": []}

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

        result: dict = {}
        if return_dense:
            # L2-normalize so Qdrant's COSINE distance is equivalent to
            # inner-product on dense vectors (matches BGEM3FlagModel output).
            norms = np.linalg.norm(sentence_embedding, axis=-1, keepdims=True)
            result["dense_vecs"] = sentence_embedding / np.clip(norms, 1e-9, None)

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

            lexical: list[dict[int, float]] = []
            for row_ids, row_scores, row_keep in zip(input_ids, importance, keep):
                weights: dict[int, float] = {}
                for tok_id, score in zip(row_ids[row_keep], row_scores[row_keep]):
                    weights[int(tok_id)] = float(score)
                lexical.append(weights)
            result["lexical_weights"] = lexical

        return result

    @property
    def sparse_mode(self) -> str:
        """Sparse computation mode: ``learned`` (BAAI W_lex loaded) or ``pseudo``."""
        return self._sparse_mode