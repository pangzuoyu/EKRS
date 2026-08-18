#!/usr/bin/env python3
"""Phase 12-A follow-up: extract ground-truth labels for real-infra recall@10.

Per-bundle, per-anchor (form_field / column_header / heading) ground-truth
labels so the real-infra round of ``scripts/recall_at_10_form_field_baseline.py``
measures true recall instead of rank stability (the placeholder gap at
script lines 430-438 in the version BEFORE this commit).

Heuristics live in :mod:`ekrs_rag.ground_truth` (pure functions, fully
unit-tested). This script is the I/O shell:

  1. Read the doc-to-md bundle manifest (``recommended_first`` array).
  2. For each bundle, paginate Qdrant scroll on ``doc_hash`` and collect
     all chunks as payload dicts.
  3. Apply 3 heuristics per bundle. Per-anchor SKIP + WARNING when the
     heuristic returns ``None`` (does NOT fail the whole batch).
  4. Write sidecar JSON to
     ``scripts/long_tail_lot_check_152.ground_truth.json``
     (does NOT mutate the original manifest — doc-to-md coordination cost
     stays at zero per the 2026-08-18 ruling).

Usage:
    python scripts/extract_ground_truth_labels.py \\
        --manifest /home/pangzy/code_project/doc-to-md/scripts/long_tail_lot_check_152.json \\
        --output scripts/long_tail_lot_check_152.ground_truth.json \\
        --limit 1    # spot-check one bundle first; remove for full 15

Exit codes:
    0 — manifest written (even if some anchors were SKIPPED)
    2 — Qdrant connection refused
    3 — bundle manifest missing
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path(
    "/home/pangzy/code_project/doc-to-md/scripts/long_tail_lot_check_152.json"
)
DEFAULT_OUTPUT = REPO_ROOT / "scripts" / "long_tail_lot_check_152.ground_truth.json"
SCROLL_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Qdrant fetch
# ---------------------------------------------------------------------------


def fetch_chunks_for_doc(
    qdrant_client: Any,
    collection_name: str,
    doc_hash: str,
) -> List[Dict[str, Any]]:
    """Paginate Qdrant scroll (no doc_hash filter) then Python-filter
    by ``doc_hash`` prefix.

    The manifest's ``bundle_id`` is a 16-char hex prefix; the actual
    Qdrant ``doc_hash`` carries a ``_r<timestamp>`` suffix added by the
    parser during ingestion-notify. Qdrant 1.11 doesn't expose native
    prefix matching on the ``doc_hash`` keyword field, so we fetch all
    points (paginated) and filter locally via
    :func:`ekrs_rag.ground_truth.filter_chunks_by_doc_prefix`.

    For 15 bundles × ~4k points this is ~60k startswith checks — well
    under 1s of CPU. If the collection grows past ~100k points, replace
    this with a per-bundle ``MatchText`` query (requires text index on
    ``doc_hash``) or maintain a ``doc_hash → bundle_id`` mapping table
    in the collection metadata.
    """
    from qdrant_client import models  # local import — keep script CLI-friendly

    chunks: List[Dict[str, Any]] = []
    offset: Optional[Any] = None
    while True:
        results, next_offset = qdrant_client.scroll(
            collection_name=collection_name,
            scroll_filter=models.Filter(must=[]),  # no filter — fetch all
            limit=SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in results:
            if point.payload is not None:
                chunks.append(dict(point.payload))
        if next_offset is None or not results:
            break
        offset = next_offset
    return filter_chunks_by_doc_prefix(chunks, doc_hash)


def build_qdrant_client():
    """Construct QdrantManager from ``Settings`` (env-var-driven).

    Mirrors the runtime construction in :mod:`ekrs_rag.main`:
    ``QdrantManager(host=, port=, collection_name=, embedding_service=)``.
    The embedding_service is required (Phase 6B B3); the script wraps it
    in a local instance (the extractor only reads via scroll, not via
    ``.search`` / ``.upsert``, so the model is never actually invoked).
    """
    from ekrs_rag.core.config import Settings
    from ekrs_rag.retrieval.qdrant_client import QdrantManager
    from ekrs_rag.retrieval.embedding_service import EmbeddingService

    settings = Settings()
    qm = QdrantManager(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
        collection_name=settings.COLLECTION_NAME,
        embedding_service=EmbeddingService(),
        auto_reindex=False,
    )
    return qm._client, qm


# ---------------------------------------------------------------------------
# Anchor extraction (delegates to ekrs_rag.ground_truth pure functions)
# ---------------------------------------------------------------------------


# Both helpers live in :mod:`ekrs_rag.ground_truth` so they can be unit-
# tested without the script's I/O stack. The script is a thin wrapper that
# fetches chunks from Qdrant and merges results into the sidecar manifest.
from ekrs_rag.ground_truth import (  # noqa: E402 — used further down
    build_sidecar,
    extract_anchors_for_bundle,
    filter_chunks_by_doc_prefix,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract ground-truth labels for recall@10 baseline.",
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help="Path to bundle manifest JSON (default: doc-to-md long-tail manifest)",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Sidecar output path (default: scripts/long_tail_lot_check_152.ground_truth.json)",
    )
    parser.add_argument(
        "-n", "--limit", type=int, default=15,
        help="Number of bundles to label (default 15 per Phase 12 §七 Item 3)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet the heavy EKRS noise unless DEBUG
    logging.getLogger("ekrs_rag").setLevel(
        logging.DEBUG if args.verbose else logging.WARNING,
    )

    if not args.manifest.exists():
        logger.error("Bundle manifest missing: %s", args.manifest)
        return 3

    data = json.loads(args.manifest.read_text())
    raw = data.get("recommended_first") or data.get("full_list") or []
    bundles = raw[: args.limit]
    logger.info("Loaded %d bundles from %s (limit=%d)", len(bundles), args.manifest, args.limit)

    try:
        _client, qdrant = build_qdrant_client()
    except Exception as exc:
        logger.error("Qdrant init failed: %s", exc)
        return 2

    collection_name = qdrant._collection_name
    ground_truth_by_bundle: Dict[str, List[Dict[str, Any]]] = {}
    for bundle in bundles:
        bundle_id = bundle["bundle_id"]
        try:
            chunks = fetch_chunks_for_doc(qdrant._client, collection_name, bundle_id)
        except Exception as exc:
            logger.warning(
                "SKIP whole bundle %s — Qdrant scroll failed: %s",
                bundle_id, exc,
            )
            ground_truth_by_bundle[bundle_id] = []
            continue
        if not chunks:
            logger.warning(
                "SKIP whole bundle %s — no chunks in Qdrant (not ingested yet?)",
                bundle_id,
            )
            ground_truth_by_bundle[bundle_id] = []
            continue
        anchors = extract_anchors_for_bundle(bundle, chunks)
        ground_truth_by_bundle[bundle_id] = anchors
        logger.info(
            "%s — %d/%d anchors labeled",
            bundle_id, len(anchors), 3,
        )

    sidecar = build_sidecar(bundles, ground_truth_by_bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
    total_anchors = sum(len(v) for v in ground_truth_by_bundle.values())
    skipped = len(bundles) * 3 - total_anchors
    print(f"Wrote ground-truth sidecar to {args.output}")
    print(f"  bundles: {len(bundles)}")
    print(f"  anchors labeled: {total_anchors} / {len(bundles) * 3}")
    print(f"  anchors skipped: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())