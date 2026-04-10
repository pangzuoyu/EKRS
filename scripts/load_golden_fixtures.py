"""Load golden set fixture documents into Qdrant for CI testing.

Usage:
    python scripts/load_golden_fixtures.py [--qdrant-host localhost] [--qdrant-port 6333]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

# Add rag/ to path so we can import ekrs_rag
sys.path.insert(0, str(Path(__file__).parent.parent / "rag"))

from qdrant_client import QdrantClient, models

from ekrs_shared.models import Chunk

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Golden set fixture documents — raw text blocks that appear in golden queries
GOLDEN_FIXTURES = [
    {
        "doc_hash": "golden_temp_001",
        "version": 1,
        "blocks": [
            {
                "block_id": "gb_temp_001",
                "text": "温度不得超过80°C",
                "scope_path": ["national", "GB"],
                "page_numbers": [1],
            },
            {
                "block_id": "gb_temp_002",
                "text": "本规范适用于高温工作环境",
                "scope_path": ["national", "GB"],
                "page_numbers": [1],
            },
        ],
    },
    {
        "doc_hash": "golden_pressure_001",
        "version": 1,
        "blocks": [
            {
                "block_id": "gb_press_001",
                "text": "压力不低于1.0MPa",
                "scope_path": ["national", "GB"],
                "page_numbers": [2],
            },
        ],
    },
    {
        "doc_hash": "golden_range_001",
        "version": 1,
        "blocks": [
            {
                "block_id": "gb_range_001",
                "text": "工作温度范围10°C至80°C",
                "scope_path": ["national", "GB"],
                "page_numbers": [3],
            },
        ],
    },
    {
        "doc_hash": "golden_diameter_001",
        "version": 1,
        "blocks": [
            {
                "block_id": "gb_dia_001",
                "text": "直径为25mm",
                "scope_path": ["enterprise", "Acme"],
                "page_numbers": [1],
            },
        ],
    },
    {
        "doc_hash": "golden_national_001",
        "version": 1,
        "blocks": [
            {
                "block_id": "nat_001",
                "text": "温度不得超过100°C",
                "scope_path": ["national", "CN"],
                "page_numbers": [1],
            },
        ],
    },
    {
        "doc_hash": "golden_enterprise_001",
        "version": 1,
        "blocks": [
            {
                "block_id": "ent_001",
                "text": "温度不得超过60°C",
                "scope_path": ["enterprise", "Acme"],
                "page_numbers": [1],
            },
        ],
    },
]


def chunks_from_fixture(fixture: dict) -> list[Chunk]:
    """Convert a fixture into a list of Chunks."""
    chunks = []
    for block in fixture["blocks"]:
        chunk = Chunk(
            text=block["text"],
            scope_path=block["scope_path"],
            source_block_ids=[block["block_id"]],
            page_numbers=block["page_numbers"],
            token_count=len(block["text"]) // 4,
            doc_hash=fixture["doc_hash"],
            version=fixture["version"],
        )
        chunks.append(chunk)
    return chunks


def upsert_chunks(chunks: list[Chunk], client: QdrantClient, collection: str, vector_size: int) -> int:
    """Upsert chunks with dummy dense vectors."""
    points = []
    for chunk in chunks:
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chunk.doc_hash}:{chunk.version}:{chunk.source_block_ids}"))
        payload = {
            "text": chunk.text,
            "scope_path": chunk.scope_path,
            "source_block_ids": chunk.source_block_ids,
            "token_count": chunk.token_count,
            "doc_hash": chunk.doc_hash,
            "version": chunk.version,
            "page_numbers": chunk.page_numbers,
        }
        points.append(models.PointStruct(
            id=point_id,
            vector={"dense": [0.0] * vector_size},
            payload=payload,
        ))

    batch_size = 100
    total = 0
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        client.upsert(collection_name=collection, points=batch)
        total += len(batch)
    return total


def main():
    parser = argparse.ArgumentParser(description="Load golden set fixtures into Qdrant")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--collection", default="rag_documents")
    parser.add_argument("--vector-size", type=int, default=384)
    args = parser.parse_args()

    client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)

    # Ensure collection exists
    try:
        client.get_collection(args.collection)
        logger.info("Collection %s already exists", args.collection)
    except Exception:
        logger.info("Creating collection %s with vector_size=%d", args.collection, args.vector_size)
        client.create_collection(
            collection_name=args.collection,
            vectors_config={
                "dense": models.VectorParams(
                    size=args.vector_size,
                    distance=models.Distance.COSINE,
                ),
            },
        )

    # Clear existing golden fixtures (delete by doc_hash prefix)
    for fixture in GOLDEN_FIXTURES:
        try:
            client.delete(
                collection_name=args.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="doc_hash",
                                match=models.MatchValue(value=fixture["doc_hash"]),
                            ),
                        ],
                    ),
                ),
            )
        except Exception:
            pass

    # Load fixtures
    total_chunks = 0
    for fixture in GOLDEN_FIXTURES:
        chunks = chunks_from_fixture(fixture)
        n = upsert_chunks(chunks, client, args.collection, args.vector_size)
        total_chunks += n
        logger.info("Loaded %d chunks for %s", n, fixture["doc_hash"])

    logger.info("Done. Total chunks loaded: %d", total_chunks)


if __name__ == "__main__":
    main()
