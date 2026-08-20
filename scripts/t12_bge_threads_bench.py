#!/usr/bin/env python3
"""Phase 12 Task D+ micro-bench — bge-m3 4-vs-8 thread scaling.

Measures bge-m3 ONNX encode() wall-time at different
``BGE_M3_INTRA_OP_THREADS`` values to decide whether the default should
move from 4 → 8. The plan-doc expectation (memory file) is that 8 will
beat 4 by < 20% due to diminishing returns past 4 threads on matmul /
attention ops.

Acceptance:
- If threads=8 / threads=4 < 1.20 → keep default at 4 (insufficient gain).
- If threads=8 / threads=4 >= 1.20 → recommend bumping default to 8.

Corpus: 48 chunks for doc_hash ``000150f86cdbc3c1`` (verified 48
v=2 points in Qdrant, single bundle from Phase 12 Task D 745-bundle
re-ingest). Pulled live from Qdrant so the bench uses real production
text rather than synthetic stubs.

Metrics per thread setting:
- mean / p50 / p99 wall-time across N iterations
- peak RSS via ``resource.getrusage`` (KB → MB)
- CPU% proxy = 1 / (wall_time / theoretical_single_thread_time)

Usage:
    # Set env var BEFORE running — the value is read at OnnxBgeM3 init.
    BGE_M3_INTRA_OP_THREADS=4 python scripts/t12_bge_threads_bench.py

Or run inside the live rag container where Qdrant is reachable on
the ``qdrant`` hostname:
    docker exec -e BGE_M3_INTRA_OP_THREADS=4 deployment-rag-1 \\
        python /tmp/t12_bge_threads_bench.py
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import List

# Repo path so ekrs_rag package is importable in container.
sys.path.insert(0, "/home/pangzy/code_project/EKRS/rag")

from qdrant_client import QdrantClient  # noqa: E402

from ekrs_rag.retrieval.onnx_bge_m3 import OnnxBgeM3  # noqa: E402


# Verified during Phase 12 Task D verification: 48 unique v=2 points for
# this doc. Picked because it's a single 1-block doc split into 48
# chunks by the Chinese-text chunker — typical real-world workload.
DOC_HASH = "000150f86cdbc3c1"
COLLECTION = "rag_documents"


def _percentile(data: List[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[idx]


def _fetch_chunks(qdrant_host: str, qdrant_port: int) -> List[str]:
    """Pull chunk texts for DOC_HASH from Qdrant (live data)."""
    client = QdrantClient(host=qdrant_host, port=qdrant_port)
    texts: List[str] = []
    offset = None
    while True:
        results, next_offset = client.scroll(
            collection_name=COLLECTION,
            scroll_filter={
                "must": [
                    {"key": "doc_hash", "match": {"value": DOC_HASH}},
                    {"key": "version", "match": {"value": 2}},
                ]
            },
            limit=100,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for pt in results:
            if pt.payload and "text" in pt.payload:
                texts.append(pt.payload["text"])
        if next_offset is None:
            break
        offset = next_offset
    return texts


def _peak_rss_mb() -> float:
    """Linux peak RSS in MB (KB from getrusage → MB)."""
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss_kb / 1024.0


def _bench_encode(model: OnnxBgeM3, texts: List[str], iterations: int) -> dict:
    """Run encode() ``iterations`` times, return timing + memory stats."""
    latencies: List[float] = []
    # Warmup — first encode loads any lazy state (tokenizer warm, etc.).
    _ = model.encode(texts)

    for _ in range(iterations):
        t0 = time.perf_counter()
        _ = model.encode(texts)
        latencies.append(time.perf_counter() - t0)

    return {
        "iterations": iterations,
        "n_chunks": len(texts),
        "mean_ms": statistics.mean(latencies) * 1000,
        "p50_ms": _percentile(latencies, 50) * 1000,
        "p99_ms": _percentile(latencies, 99) * 1000,
        "min_ms": min(latencies) * 1000,
        "max_ms": max(latencies) * 1000,
        "peak_rss_mb": _peak_rss_mb(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--model-dir", default=os.environ.get("EMBEDDING_MODEL_DIR", "/app/rag/models/bge-m3"))
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument(
        "--threads",
        type=int,
        default=int(os.environ.get("BGE_M3_INTRA_OP_THREADS", "0")) or None,
        help="Expected thread count (echoed in output for clarity).",
    )
    args = parser.parse_args()

    model_dir = Path(args.model_dir)
    if not (model_dir / "model.onnx").exists():
        print(
            f"ERROR: model.onnx not found at {model_dir}. "
            f"Set --model-dir or EMBEDDING_MODEL_DIR.",
            file=sys.stderr,
        )
        return 2

    print(f"[bench] BGE_M3_INTRA_OP_THREADS = {args.threads or 'unset (default)'}", flush=True)
    print(f"[bench] corpus = {DOC_HASH} (Phase 12 Task D verified 48 chunks)", flush=True)
    print(f"[bench] loading chunks from Qdrant {args.qdrant_host}:{args.qdrant_port}...", flush=True)

    texts = _fetch_chunks(args.qdrant_host, args.qdrant_port)
    print(f"[bench] fetched {len(texts)} chunks", flush=True)

    if not texts:
        print("ERROR: no chunks fetched. Is doc_hash/version correct?", file=sys.stderr)
        return 3

    print(f"[bench] loading OnnxBgeM3 from {model_dir}...", flush=True)
    model = OnnxBgeM3(model_dir)
    print(f"[bench] model loaded; sparse_mode={model.sparse_mode}", flush=True)

    stats = _bench_encode(model, texts, args.iterations)
    stats["threads_env"] = args.threads
    stats["doc_hash"] = DOC_HASH
    stats["model_dir"] = str(model_dir)

    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
