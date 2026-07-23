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

``FlagEmbedding.BGEM3FlagModel`` adds two extra heads on top — a
``ColBert``-style multi-vector projection and a learned *lexical*
projection that produces per-token sparse weights for inverted-index
retrieval. Neither head is in this vanilla export, so we cannot recover
BAAI's official sparse weights verbatim.

Pseudo-sparse workaround
------------------------
We compute a *self-similarity* sparse weight as ``<token_emb, sent_emb>``
for each non-special token — a well-known dense-retrieval trick
(CoIL, SPLADE-style approximation without a learned head). The
resulting ``{token_id: weight}`` dict is not as discriminative as
BAAI's learned lexical projection, but it preserves the lexical
matching property well enough for hybrid retrieval to work, and
matches the ``{int: float}`` shape the existing ``EncodedVector``
contract and Qdrant sparse path already expect.

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
# tokens and must be excluded from the pseudo-sparse representation.
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
        logger.info("OnnxBgeM3 loaded from %s", self._model_dir)

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
            # Self-similarity importance: <token_emb, sent_emb> per token,
            # masked to non-padded, non-special positions. Negative scores
            # are clipped to 0 (Qdrant sparse weights must be positive).
            importance = (token_embeddings * sentence_embedding[:, None, :]).sum(axis=-1)
            importance = np.clip(importance, 0.0, None)
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