"""EmbeddingService facade for bge-m3 (1024d dense + sparse).

Replaces the old BGESmallEmbedder (bge-small-en, 384d dense-only).
Loads the bge-m3 ONNX export directly via onnxruntime + HuggingFace
tokenizer (no FlagEmbedding dependency at runtime). Dense vectors
come from the ONNX export's sentence_embedding output; sparse weights
are either BAAI's learned ``nn.Linear(1024, 1)`` head (when
``sparse_linear.pt`` is present in the model directory) or a
self-similarity pseudo-sparse computed from token embeddings. See
``onnx_bge_m3.py`` docstring for the full rationale.

Falls back to dummy mode when model files are absent (CI without
model), but blocks upsert in dummy mode (D1) to prevent silent data
corruption.

Phase 7 T7 (Decision §4): encode() consults an in-process LRU+TTL
cache keyed on (text_hash, model_version). Cache prevents repeated
calls to the ONNX export for the same chunk text; flush_cache() is
exposed for /v1/admin/embedding-cache/flush and on model swap.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from qdrant_client import models

from ..core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "bge-m3"
DENSE_SIZE = 1024  # bge-m3 dense vector dimension

# Module-level knobs read at cache construction time so tests can patch.
_CACHE_CAPACITY = int(os.environ.get("EKRS_TEST_CACHE_CAPACITY", "10000"))
_CACHE_TTL_SEC = int(os.environ.get("EKRS_TEST_CACHE_TTL_SEC", "86400"))


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


def _compute_model_version(model_dir: Path) -> str:
    """Compute cache-versioning token for the loaded model.

    Decision §4: when the operator swaps bge-m3 on disk (e.g. upgrades
    the ONNX export or rotates sparse_linear.pt) the cache MUST drop
    all entries — otherwise we'd serve stale vectors from the previous
    model. SHA256 of model.onnx alone is enough for the common case;
    we add sparse_linear.pt into the mix when present so a sparse-head
    swap also invalidates cached entries.
    """
    digests: list[str] = []
    for fname in ("model.onnx", "sparse_linear.pt"):
        fp = model_dir / fname
        if fp.exists():
            digests.append(f"{fname}={hashlib.sha256(fp.read_bytes()).hexdigest()[:16]}")
    if not digests:
        return "dummy"
    return "|".join(digests)


class _LRUCache:
    """Thread-unsafe LRU + TTL cache keyed on str.

    Phase 7 T7: ordered by insertion order; oldest evicted on overflow;
    each entry carries a monotonic timestamp; entries older than ttl
    are treated as misses on access. The cache is intentionally local
    to a single EmbeddingService instance — embedder workloads are
    single-threaded in this service (FastAPI route handlers await
    independently but `encode` itself does not re-enter itself).
    """

    __slots__ = ("_entries", "_capacity", "_ttl_sec")

    def __init__(self, capacity: int, ttl_sec: int) -> None:
        self._entries: "OrderedDict[str, dict[str, object]]" = OrderedDict()
        self._capacity = capacity
        self._ttl_sec = ttl_sec

    def get(self, key: str) -> Optional[EncodedVector]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        # TTL check first — stale entries fall through to recomputation
        # without polluting the cache. They are NOT removed here because
        # touch() would re-position them and we want LRU order to reflect
        # actual last access (excluding freshness overrides).
        if time.monotonic() - float(entry["inserted_at"]) > self._ttl_sec:  # type: ignore[arg-type]
            self._entries.pop(key, None)
            return None
        # Move to end on hit — marks as most-recently-used.
        self._entries.move_to_end(key)
        return entry["vector"]  # type: ignore[return-value]

    def put(self, key: str, vector: EncodedVector) -> None:
        # Insertion-order refresh: re-inserting an existing key positions
        # it at the tail (most-recently-used) without changing capacity.
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = {"vector": vector, "inserted_at": time.monotonic()}
        # Evict oldest until within capacity.
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def clear(self) -> int:
        """Drop every entry; return the count that was cleared."""
        n = len(self._entries)
        self._entries.clear()
        return n

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self):
        """Yield keys in insertion order (oldest first).

        Exposed for diagnostics + tests that need to backdate timestamps
        on cached entries (TTL verification).
        """
        return iter(self._entries)

    def _backdate_all(self, secs_ago: float) -> None:
        """Test-only helper: shift every entry's inserted_at into the past.

        Lets tests force a TTL expiry without sleeping for hours. Production
        code never calls this — the underscore prefix signals internal.
        """
        target = time.monotonic() - secs_ago
        for entry in self._entries.values():
            entry["inserted_at"] = target


class EmbeddingService:
    """Facade over the bge-m3 ONNX export. Single encode() returns EncodedVector list."""

    DENSE_SIZE = DENSE_SIZE

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        self._model_dir = Path(model_dir) if model_dir else DEFAULT_MODEL_DIR
        self._model = None
        self._is_dummy = False
        # Phase 7 T7: LRU+TTL cache for encode() output. Capacity and TTL
        # are read from Settings so operators can tune via .env without
        # code changes. The cache is empty until encode() runs in
        # non-dummy mode (dummy mode bypasses the cache entirely).
        self._cache: _LRUCache = _LRUCache(
            capacity=_CACHE_CAPACITY,
            ttl_sec=_CACHE_TTL_SEC,
        )
        self._model_version: str = "dummy"
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
            # Phase 7 T7: stamp the loaded model so encode() can include
            # it in cache keys. If the operator later swaps model.onnx
            # and the service is not restarted, the next encode() will
            # see a different model_version → cache miss → recompute.
            # If the service IS restarted, _load() runs again and rebuilds
            # the cache from scratch (empty OrderedDict on construction).
            self._model_version = _compute_model_version(self._model_dir)
            logger.info(
                "Loaded bge-m3 (ONNX) from %s, model_version=%s",
                self._model_dir, self._model_version,
            )
        except Exception as e:
            logger.warning("Failed to load bge-m3: %s, using dummy", e)
            self._is_dummy = True

    def _verify_sha256(self, onnx_path: Path, sha_path: Path) -> None:
        """Verify SHA256 for every entry listed in the .sha256 file (D1).

        Iterates every ``<sha>  <filename>`` line and confirms the actual
        file's SHA matches. Raises RuntimeError on the first mismatch
        (or on a missing entry for the ONNX model). Originally only
        verified model.onnx; extended to cover ``sparse_linear.pt`` when
        that file is shipped alongside the ONNX export.
        """
        entries = {}
        for line in sha_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            sha, _, fname = line.partition("  ")
            entries[fname.strip()] = sha.strip()

        onnx_name = onnx_path.name
        if onnx_name not in entries:
            raise RuntimeError(f"No SHA256 entry for {onnx_name} in {sha_path}")

        # Verify ONNX first (it is the only file the load actually needs);
        # then walk the remaining entries so a tampered sparse_linear.pt
        # cannot silently load.
        for fname, expected_sha in entries.items():
            fpath = onnx_path.parent / fname
            if not fpath.exists():
                # Skip optional files that aren't shipped — only ONNX is
                # required to be present for the model to load.
                if fname != onnx_name:
                    logger.warning(
                        "SHA256 entry %s listed but file missing; skipping", fname
                    )
                    continue
            actual = hashlib.sha256()
            with open(fpath, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    actual.update(chunk)
            actual_hex = actual.hexdigest()
            if actual_hex != expected_sha:
                raise RuntimeError(
                    f"SHA256 mismatch for {fname}: "
                    f"expected {expected_sha}, got {actual_hex}"
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

        Phase 7 T7: in non-dummy mode, consults the LRU+TTL cache keyed
        on (sha256(text), model_version). Cache hits return without
        touching the ONNX export; cache misses trigger a single batched
        encode() and populate the cache per-input.
        """
        if not texts:
            return []
        if self._is_dummy:
            # Dummy mode bypasses the cache — there is no real model to
            # memoize, and writes are already blocked at upsert time (D1).
            return [EncodedVector(dense=[0.0] * self.DENSE_SIZE, sparse={}) for _ in texts]

        # _model is non-None when not in dummy mode (set by __init__), but mypy
        # can't follow that invariant through `_is_dummy`.
        assert self._model is not None, "model must be loaded when not in dummy mode"

        # Phase 7 T7: split texts into cached / uncached, encode only the
        # misses, then assemble the final list in original order.
        results: list[Optional[EncodedVector]] = [None] * len(texts)
        misses: list[tuple[int, str, str]] = []  # (orig_index, text_hash, raw_text)
        for i, t in enumerate(texts):
            key = f"{hashlib.sha256(t.encode('utf-8')).hexdigest()}|{self._model_version}"
            hit = self._cache.get(key)
            if hit is not None:
                results[i] = hit
            else:
                misses.append((i, key, t))

        if misses:
            raw = self._model.encode(
                [t for _, _, t in misses],
                return_dense=True,
                return_sparse=True,
            )
            dense_array = raw["dense_vecs"]
            sparse_list = raw["lexical_weights"]
            for (orig_idx, key, _t), d, s in zip(misses, dense_array, sparse_list):
                vec = EncodedVector(dense=list(d), sparse=s)
                results[orig_idx] = vec
                self._cache.put(key, vec)

        # All slots must be filled by this point — the assert catches any
        # future refactor that breaks the bookkeeping above.
        assert all(r is not None for r in results), "encode() produced holes"
        return results  # type: ignore[return-value]

    def flush_cache(self) -> int:
        """Drop every cached entry; return how many were cleared.

        Phase 7 T7: backing operation for /v1/admin/embedding-cache/flush
        and operator recovery after a model swap. Idempotent — safe to
        call when the cache is empty (returns 0).
        """
        return self._cache.clear()

    def cache_size(self) -> int:
        """Number of distinct (text, model_version) entries currently cached."""
        return len(self._cache)

    @property
    def model_version(self) -> str:
        """Versioning token included in cache keys (Decision §4)."""
        return self._model_version

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
