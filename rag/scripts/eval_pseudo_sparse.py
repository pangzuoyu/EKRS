"""Pseudo-sparse recall@K sanity check (Phase 7 T2 follow-up).

Creates a controlled corpus of N=30 engineering-spec chunks with
known scope_path labels and 10 queries with known expected chunk IDs.
Measures recall@5 / recall@10 for two modes:
  A. dense-only
  B. dense + pseudo-sparse (current OnnxBgeM3 path)

Reports per-mode recall + the delta. If pseudo-sparse recall drops
by more than the threshold below, the conclusion is that the
self-similarity sparse approximation is actively harming retrieval
and the bge-m3 ONNX loader should fall back to dense-only (or use
BAAI's official sparse ONNX).

Usage (manual):
  cd rag && python scripts/eval_pseudo_sparse.py
"""
from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient, models

# Make rag package importable when running this script directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from ekrs_rag.retrieval.embedding_service import (  # noqa: E402
    EmbeddingService,
)
from ekrs_rag.retrieval.onnx_bge_m3 import _SPECIAL_TOKEN_IDS  # noqa: E402

logging.basicConfig(level=logging.WARNING)

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION = "rag_eval_pseudo_sparse"  # isolated collection for this eval

# Corpus: 30 chunks covering 6 engineering topics. Each topic has a query
# and 3-5 chunks, of which the query is expected to match ≥2.
CORPUS = [
    # Topic: 温度上限 (temperature limits)
    ("temperature", "national",
     "高温环境下温度不得超过80°C"),
    ("temperature", "national",
     "低温环境温度不得低于-20°C"),
    ("temperature", "industry",
     "压力容器设计温度上限为200°C"),
    ("temperature", "industry",
     "蒸汽管道运行温度上限为150°C"),
    ("temperature", "project",
     "本项目工艺介质温度范围-10°C至50°C"),
    ("temperature", "reference",
     "ASME标准规定碳钢许用温度上限427°C"),

    # Topic: 压力范围 (pressure ranges)
    ("pressure", "national",
     "高压管道压力不得超过10MPa"),
    ("pressure", "national",
     "中压管道压力范围0.5MPa至4MPa"),
    ("pressure", "industry",
     "天然气管道设计压力6.4MPa"),
    ("pressure", "industry",
     "蒸汽管道压力上限1.6MPa"),
    ("pressure", "project",
     "本装置操作压力0.8MPa至1.2MPa"),
    ("pressure", "reference",
     "API标准LPG储罐设计压力1.0MPa"),

    # Topic: 材料 (material specs)
    ("material", "national",
     "受压元件应采用低碳钢或低合金钢"),
    ("material", "national",
     "高温合金适用于800°C以上工况"),
    ("material", "industry",
     "石化装置推荐使用316L不锈钢"),
    ("material", "industry",
     "管道法兰材料应与管子本体一致"),
    ("material", "project",
     "本项目塔器主体材料选用Q345R"),
    ("material", "reference",
     "ASTM A240规定304不锈钢板材标准"),

    # Topic: 流速 (flow velocity)
    ("flow", "national",
     "工艺管道流速不宜超过3m/s"),
    ("flow", "national",
     "气体管道流速上限25m/s"),
    ("flow", "industry",
     "蒸汽管道流速推荐30m/s至50m/s"),
    ("flow", "project",
     "本管线设计流速2.5m/s"),
    ("flow", "reference",
     "API 610规定泵入口流速上限4.5m/s"),

    # Topic: 防腐 (corrosion protection)
    ("corrosion", "national",
     "埋地管道外防腐采用三层PE结构"),
    ("corrosion", "industry",
     "酸性介质应选用双相不锈钢"),
    ("corrosion", "industry",
     "阴极保护电位应保持在-850mV以下"),
    ("corrosion", "project",
     "本罐区内壁防腐采用环氧树脂涂料"),
    ("corrosion", "reference",
     "NACE MR0175规定酸性环境材料要求"),

    # Decoys
    ("general", "project",
     "本项目工程位于沿海地区"),
    ("general", "project",
     "项目总投资额5.6亿元人民币"),
]

# 6 queries × expected chunk indices (in CORPUS). Each query expects at
# least 2 of its topic chunks to be in top-K.
QUERIES = [
    ("高温环境下温度上限",
     [0, 1, 2]),       # 温度 + 高温 + 上限
    ("压力管道设计压力范围",
     [6, 7, 8, 10]),   # 压力 + 管道 + 设计 + 范围
    ("石化装置材料选择",
     [12, 14, 15]),    # 材料 + 石化 + 装置
    ("工艺管道流速标准",
     [18, 19, 20]),    # 流速 + 管道 + 标准
    ("埋地管道防腐要求",
     [24, 25]),        # 防腐 + 管道
    ("不锈钢材料规范",
     [14, 15, 17]),    # 不锈钢 + 材料
]


def _make_point_id(doc_id: str, version: int, text: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS,
                          f"{doc_id}:{version}:{text[:50]}"))


def _dense_only_search(
    client: QdrantClient, query_dense: list[float], top_k: int
) -> list[str]:
    """Dense-only Prefetch using only the dense vector."""
    result = client.query_points(
        collection_name=COLLECTION,
        prefetch=[models.Prefetch(
            query=query_dense, using="dense", limit=top_k
        )],
        query=models.NearestQuery(nearest=query_dense),
        using="dense",
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )
    return [p.id for p in result.points]


def _hybrid_search(
    client: QdrantClient,
    query_dense: list[float],
    query_sparse: dict[int, float],
    top_k: int,
) -> list[str]:
    """Hybrid: Prefetch(dense) + Prefetch(sparse) + FusionQuery(RRF)."""
    indices = sorted(query_sparse.keys())
    values = [query_sparse[i] for i in indices]
    sparse_q = models.SparseVector(indices=indices, values=values)
    result = client.query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(query=query_dense, using="dense", limit=top_k),
            models.Prefetch(query=sparse_q, using="sparse", limit=top_k),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )
    return [p.id for p in result.points]


def _make_sparse_query(dense_emb, token_emb, attention_mask, input_ids):
    """Replicate OnnxBgeM3 sparse computation for query encoding."""
    importance = (token_emb * dense_emb).sum(axis=-1)
    importance = np.clip(importance, 0.0, None)
    is_special = np.isin(input_ids, list(_SPECIAL_TOKEN_IDS))
    keep = attention_mask.astype(bool) & (~is_special)
    out: dict[int, float] = {}
    for tok_id, score in zip(input_ids[keep], importance[keep]):
        out[int(tok_id)] = float(score)
    return out


def _encode_query(es: EmbeddingService, text: str) -> tuple[list[float], dict[int, float]]:
    """Encode query using OnnxBgeM3 internals to get dense + sparse."""
    model = es._model  # OnnxBgeM3 instance
    enc = model._tokenizer(
        [text], return_tensors="np", padding=True, truncation=True, max_length=512
    )
    sess = model._session
    token_emb, sent_emb = sess.run(
        None,
        {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"]},
    )
    norms = np.linalg.norm(sent_emb, axis=-1, keepdims=True)
    dense = sent_emb / np.clip(norms, 1e-9, None)
    sparse = _make_sparse_query(
        dense[0], token_emb[0], enc["attention_mask"][0], enc["input_ids"][0]
    )
    return dense[0].tolist(), sparse


def main() -> int:
    es = EmbeddingService()
    if es.is_dummy:
        print("ERROR: EmbeddingService in dummy mode (model not loaded)")
        return 1

    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    # Recreate collection for clean eval
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": models.VectorParams(size=1024, distance=models.Distance.COSINE)},
        sparse_vectors_config={"sparse": models.SparseVectorParams(index=models.SparseIndexParams(on_disk=False))},
    )

    # Index corpus
    print(f"Indexing {len(CORPUS)} corpus chunks ...")
    texts = [t for _, _, t in CORPUS]
    encoded = es.encode(texts)
    points = []
    # Qdrant requires UUID or unsigned integer point IDs.
    eval_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    idx_to_id: dict[int, str] = {}
    for (topic, scope, text), vec in zip(CORPUS, encoded):
        idx = CORPUS.index((topic, scope, text))
        point_id = str(uuid.uuid5(eval_uuid, f"eval-{idx:03d}"))
        idx_to_id[idx] = point_id
        points.append(models.PointStruct(
            id=point_id,
            vector={"dense": vec.dense, "sparse": es.to_qdrant_sparse(vec.sparse)},
            payload={"text": text, "topic": topic, "scope_path": scope, "idx": idx},
        ))
    client.upsert(collection_name=COLLECTION, points=points)

    # Eval
    dense_hits = {"5": 0, "10": 0}
    hybrid_hits = {"5": 0, "10": 0}
    per_query = []

    for q, expected_idxs in QUERIES:
        dense_q, sparse_q = _encode_query(es, q)
        exp_ids = {idx_to_id[i] for i in expected_idxs}

        d5 = _dense_only_search(client, dense_q, 5)
        d10 = _dense_only_search(client, dense_q, 10)
        h5 = _hybrid_search(client, dense_q, sparse_q, 5)
        h10 = _hybrid_search(client, dense_q, sparse_q, 10)

        d5_hit = len(set(d5) & exp_ids)
        d10_hit = len(set(d10) & exp_ids)
        h5_hit = len(set(h5) & exp_ids)
        h10_hit = len(set(h10) & exp_ids)

        for k in ("5", "10"):
            if d5_hit and k == "5":
                pass
        dense_hits["5"] += d5_hit
        dense_hits["10"] += d10_hit
        hybrid_hits["5"] += h5_hit
        hybrid_hits["10"] += h10_hit
        per_query.append({
            "q": q,
            "expected": len(expected_idxs),
            "dense@5": d5_hit, "dense@10": d10_hit,
            "hybrid@5": h5_hit, "hybrid@10": h10_hit,
        })

    # Report
    print()
    print(f"{'Query':30s} {'exp':4s} {'d@5':4s} {'d@10':5s} {'h@5':4s} {'h@10':5s}")
    print("-" * 60)
    for r in per_query:
        print(f"{r['q'][:28]:30s} {r['expected']:4d} {r['dense@5']:4d} {r['dense@10']:5d} {r['hybrid@5']:4d} {r['hybrid@10']:5d}")

    total_exp = sum(r["expected"] for r in per_query)
    print()
    print(f"{'Mode':10s} {'recall@5':10s} {'recall@10':10s}")
    print("-" * 40)
    print(f"{'dense':10s} {dense_hits['5']/total_exp*100:9.1f}% {dense_hits['10']/total_exp*100:9.1f}%")
    print(f"{'hybrid':10s} {hybrid_hits['5']/total_exp*100:9.1f}% {hybrid_hits['10']/total_exp*100:9.1f}%")

    delta_5 = (hybrid_hits["5"] - dense_hits["5"]) / total_exp * 100
    delta_10 = (hybrid_hits["10"] - dense_hits["10"]) / total_exp * 100
    print()
    print(f"Hybrid delta vs dense: @5={delta_5:+.1f}%  @10={delta_10:+.1f}%")

    if delta_10 < -5.0:
        print("VERDICT: pseudo-sparse hurts recall by >5%. Consider BAAI official sparse ONNX.")
        return 2
    print("VERDICT: pseudo-sparse is acceptable (delta within ±5%).")
    return 0


if __name__ == "__main__":
    sys.exit(main())