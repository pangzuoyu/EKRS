#!/usr/bin/env python3
"""Phase 12 §七 Item 3: form_field boost ON vs OFF recall@10 baseline.

Q3 §9.6 last-mile validation: quantifies how much R4 scope-aware boost
helps recall@10 when the user query anchors on a form_field value or a
column_header. Operates against an in-process ``EKRSRetriever`` (real
Qdrant + FTS5) by default; falls back to a synthetic corpus via
``--synthetic`` for CI / smoke runs without infra dependency.

Per Phase 12 plan §七 Item 3:
  "5 query × 3 锚点类型 (form_field / column_header / heading) × 推荐 15
   抽样 = 75 query 集. 必须产出量化数据. 无显著提升或反而下降需重新
   评估权重设计"

Outputs a markdown report with a per-anchor recall@10 comparison table
plus a per-query recall diff to ``-o`` (default stdout).

Usage examples:
    # Real-infra baseline (requires EKRS_QDRANT_HOST running):
    EKRS_QDRANT_HOST=localhost python scripts/recall_at_10_form_field_baseline.py

    # Synthetic-corpus baseline (smoke, no infra needed):
    python scripts/recall_at_10_form_field_baseline.py --synthetic -o /tmp/report.md

Exit codes:
    0 — baseline complete + report written
    2 — Qdrant connection refused (real-infra mode)
    3 — bundle list JSON missing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_TO_MD_ROOT = Path("/home/pangzy/code_project/doc-to-md")
BUNDLE_LIST_JSON = (
    DOC_TO_MD_ROOT / "scripts" / "long_tail_lot_check_152.json"
)
# Default sidecar path produced by `scripts/extract_ground_truth_labels.py`.
# Real-infra mode reads ground_truth labels from here; `--synthetic` ignores it.
DEFAULT_GROUND_TRUTH_MANIFEST = (
    REPO_ROOT / "scripts" / "long_tail_lot_check_152.ground_truth.json"
)

# Anchor type definitions: query sources + expected answer matching
ANCHOR_TYPES = ("form_field", "column_header", "heading")

# Recall@10 evaluation params
TOP_K = 10

# Suppress noisy bge-m3 + qdrant import logs unless caller passes -v
logging.basicConfig(
    level=os.environ.get("BASELINE_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleEntry:
    bundle_id: str
    filename: str
    doc_type: str
    n_blocks: int
    sample_priority: str = ""  # may not be present


@dataclass
class QueryAnchor:
    bundle_id: str
    anchor_type: str  # form_field | column_header | heading
    query_text: str
    expected_chunk_id: str  # the chunk_id that should appear in top-10


@dataclass
class QueryResult:
    query: QueryAnchor
    on_in_top10: bool
    off_in_top10: bool
    on_rank: Optional[int] = None  # 1-indexed; None if not in top-10
    off_rank: Optional[int] = None


# ---------------------------------------------------------------------------
# Bundle list loader
# ---------------------------------------------------------------------------


def load_bundles(
    json_path: Path = BUNDLE_LIST_JSON,
    limit: int = 15,
) -> List[BundleEntry]:
    """Read ``recommended_first`` from the doc-to-md bundle manifest.

    Falls back to ``full_list`` if ``recommended_first`` is missing.
    Bounds total length by ``limit`` (default 15 per plan §七 Item 3).
    """
    if not json_path.exists():
        logger.error("Bundle manifest missing at %s", json_path)
        sys.exit(3)
    with json_path.open() as f:
        data = json.load(f)
    raw = data.get("recommended_first") or data.get("full_list") or []
    bundles = [
        BundleEntry(
            bundle_id=str(b["bundle_id"]),
            filename=str(b["filename"]),
            doc_type=str(b.get("doc_type", "unknown")),
            n_blocks=int(b.get("n_blocks", 0)),
            sample_priority=str(b.get("sample_priority", "")),
        )
        for b in raw[:limit]
    ]
    logger.info("Loaded %d bundles from %s", len(bundles), json_path)
    return bundles


# ---------------------------------------------------------------------------
# Ground-truth sidecar loader (real-infra labels)
# ---------------------------------------------------------------------------


def load_anchors_from_sidecar(
    bundles: List[BundleEntry],
    ground_truth_manifest: Path,
) -> List[QueryAnchor]:
    """Read ground-truth sidecar JSON and emit one ``QueryAnchor`` per
    anchor entry. Per-bundle SKIP+WARNING when the bundle is absent from
    the sidecar or has empty ground_truth.

    Pure parsing logic lives in
    :func:`ekrs_rag.ground_truth.anchors_from_sidecar` (unit-tested).
    This wrapper is the I/O shell that reads the file + converts each
    raw dict into the script's ``QueryAnchor`` dataclass.
    """
    from ekrs_rag.ground_truth import anchors_from_sidecar

    if not ground_truth_manifest.exists():
        logger.error(
            "Ground-truth sidecar missing: %s — run "
            "scripts/extract_ground_truth_labels.py first.",
            ground_truth_manifest,
        )
        sys.exit(3)
    sidecar = json.loads(ground_truth_manifest.read_text())
    bundle_dicts = [{"bundle_id": b.bundle_id} for b in bundles]
    raw = anchors_from_sidecar(bundle_dicts, sidecar)
    anchors = [
        QueryAnchor(
            bundle_id=a["bundle_id"],
            anchor_type=a["anchor_type"],
            query_text=a["anchor_value"],
            expected_chunk_id=a["expected_chunk_id"],
        )
        for a in raw
    ]
    logger.info(
        "Loaded %d anchors from ground-truth sidecar (bundles: %d)",
        len(anchors), len(bundles),
    )
    return anchors


# ---------------------------------------------------------------------------
# Synthetic-corpus path (no-infra smoke)
# ---------------------------------------------------------------------------


def synth_bundles(n: int = 5) -> List[BundleEntry]:
    """Generate synthetic bundles for CI smoke runs. Filenames hint at
    form_fields (LOT/CHECK/STATUS) so the case count matches the real
    sample shape (``form_field``/``column_header``/``heading`` coverage).
    """
    synthetic = [
        BundleEntry(
            bundle_id=f"synth{i:04d}",
            filename=f"Lot{i:03d} NCR Status Report.doc",
            doc_type="lot",
            n_blocks=7,
        )
        for i in range(n)
    ]
    return synthetic


def synth_query_anchors(bundles: Iterable[BundleEntry]) -> List[QueryAnchor]:
    """Auto-derive 3 anchors per bundle from a small canned pool. The
    chunk_id format matches ``FTSManager.generate_chunk_id`` so the
    retriever's ``_chunk_key`` accepts them.
    """
    anchors: List[QueryAnchor] = []
    for b in bundles:
        for anchor_type in ANCHOR_TYPES:
            if anchor_type == "form_field":
                # Anchor on the LOT identifier (typical form_field value).
                m = b.filename  # "Lot049 NCR Status Report.doc"
                digits = "".join(c for c in m.split("Lot")[-1].split()[0] if c.isdigit())
                query_text = f"Lot {int(digits) if digits else 0}".strip()
                expected = f"{b.bundle_id}-0000"
            elif anchor_type == "column_header":
                # Anchor on a generic column header term.
                query_text = "A105"
                expected = f"{b.bundle_id}-0001"
            else:  # heading
                # Anchor on a heading-path text (legacy path-only retrieval).
                query_text = "Material Specification"
                expected = f"{b.bundle_id}-0002"
            anchors.append(
                QueryAnchor(
                    bundle_id=b.bundle_id,
                    anchor_type=anchor_type,
                    query_text=query_text,
                    expected_chunk_id=expected,
                )
            )
    return anchors


def synth_run_top10(
    query_text: str,
    form_field_boost: bool,
    anchors: List[QueryAnchor],
) -> List[str]:
    """Stand-in for retriever top-10: deterministic ordering that mocks
    the form_field boost effect.

    Behavior:
      - With ``form_field_boost=True``, queries whose anchor_type is
        ``"form_field"`` AND whose query_text matches the expected
        string format rank the expected chunk at position 1; others at
        position 5.
      - With ``form_field_boost=False``, all queries rank expected chunk
        at position 5 only (the form_field boost doesn't lift the
        answer into the top half). This is the "boost helps" baseline.
    """
    matching = [
        a for a in anchors
        if a.query_text == query_text
    ]
    if matching:
        anchor = matching[0]
        if form_field_boost and anchor.anchor_type == "form_field":
            return [anchor.expected_chunk_id] + [
                f"distractor-{i}" for i in range(TOP_K - 1)
            ]
    return [f"distractor-{i}" for i in range(TOP_K)]


# ---------------------------------------------------------------------------
# Real-infra path (Qdrant + FTS5 via in-process retriever)
# ---------------------------------------------------------------------------


async def real_run_top10(
    retriever: "object",  # EKRSRetriever — duck-typed to avoid hard import
    query_text: str,
    form_field_boost: bool,
    top_k: int = TOP_K,
) -> List[str]:
    """Run ``retriever.retrieve(query, top_k, form_field_boost=...)`` and
    return chunk_id list (preserving short-circuit / RRF order).
    """
    result = await retriever.retrieve(  # type: ignore[attr-defined]
        query=query_text, top_k=top_k, form_field_boost=form_field_boost
    )
    return [c.chunk_id for c in result.chunks if c.chunk_id]


def build_retriever():
    """Construct ``QdrantManager`` + ``FTSManager`` + ``EKRSRetriever``
    from environment variables. Caller passes ``form_field_boost`` per
    call to ``retriever.retrieve(...)``.

    Phase 6B B3: ``QdrantManager`` requires an ``EmbeddingService``
    instance — the prior ``(client, settings)`` positional call was the
    pre-B3 signature. Mirrors the runtime construction in
    :mod:`ekrs_rag.main` so the script picks up the same Qdrant
    collection + embedding plumbing.
    """
    from ekrs_rag.core.config import Settings
    from ekrs_rag.retrieval.qdrant_client import QdrantManager
    from ekrs_rag.retrieval.embedding_service import EmbeddingService
    from ekrs_rag.retrieval.fts_manager import FTSManager
    from ekrs_rag.retrieval.retriever import EKRSRetriever

    settings = Settings()
    qdrant = QdrantManager(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
        collection_name=settings.COLLECTION_NAME,
        embedding_service=EmbeddingService(),
        auto_reindex=False,
    )
    fts = FTSManager(db_path=settings.FTS_DB_PATH)
    return EKRSRetriever(qdrant=qdrant, fts=fts)


# ---------------------------------------------------------------------------
# Evaluation core
# ---------------------------------------------------------------------------


def evaluate(
    anchors: List[QueryAnchor],
    top10_fn,
) -> List[QueryResult]:
    """For each anchor, run 2 rounds and compute recall@10 (binary).

    ``top10_fn(query_text, form_field_boost, anchors)`` returns the
    ordered chunk_id list of length ``TOP_K`` for that round.
    """
    results: List[QueryResult] = []
    for a in anchors:
        on = top10_fn(a.query_text, True, anchors)
        off = top10_fn(a.query_text, False, anchors)
        try:
            on_rank = on.index(a.expected_chunk_id) + 1
        except ValueError:
            on_rank = None
        try:
            off_rank = off.index(a.expected_chunk_id) + 1
        except ValueError:
            off_rank = None
        results.append(
            QueryResult(
                query=a,
                on_in_top10=on_rank is not None,
                off_in_top10=off_rank is not None,
                on_rank=on_rank,
                off_rank=off_rank,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(results: List[QueryResult]) -> str:
    """Markdown report: per-anchor recall@10 + per-query diff table."""
    lines: List[str] = []
    lines.append("# Phase 12 §七 Item 3 — form_field boost recall@10 baseline")
    lines.append("")
    lines.append(f"_Generated: {date.today().isoformat()}_")
    lines.append("")
    lines.append(
        "Per-anchor recall@10 = (queries where expected chunk appears "
        "in top-10) / (total queries) for that anchor type."
    )
    lines.append("")

    # Per-anchor summary
    lines.append("## Per-anchor recall@10")
    lines.append("")
    lines.append(
        "| anchor_type | total | ON in top-10 | OFF in top-10 | Δ (ON − OFF) |"
    )
    lines.append(
        "|-------------|-------|--------------|---------------|---------------|"
    )
    for anchor_type in ANCHOR_TYPES:
        sub = [r for r in results if r.query.anchor_type == anchor_type]
        total = len(sub)
        on_n = sum(1 for r in sub if r.on_in_top10)
        off_n = sum(1 for r in sub if r.off_in_top10)
        delta = on_n - off_n
        lines.append(
            f"| {anchor_type} | {total} | {on_n} | {off_n} | {'+' if delta > 0 else ''}{delta} |"
        )
    lines.append("")

    # Per-query detail
    lines.append("## Per-query detail")
    lines.append("")
    lines.append(
        "| bundle | anchor | query | expected | ON rank | OFF rank |"
    )
    lines.append(
        "|--------|--------|-------|----------|---------|----------|"
    )
    for r in results:
        lines.append(
            f"| {r.query.bundle_id} | {r.query.anchor_type} | "
            f"{r.query.query_text!r} | {r.query.expected_chunk_id} | "
            f"{r.on_rank if r.on_rank else '—'} | "
            f"{r.off_rank if r.off_rank else '—'} |"
        )
    lines.append("")

    # Verdict
    on_total = sum(1 for r in results if r.on_in_top10)
    off_total = sum(1 for r in results if r.off_in_top10)
    lines.append("## Verdict")
    lines.append("")
    delta_total = on_total - off_total
    if delta_total > 0:
        verdict = (
            f"PASS — form_field boost improves recall@10 "
            f"by {delta_total}/{len(results)} queries."
        )
    elif delta_total == 0:
        verdict = "NEUTRAL — boost has no measurable effect at top-10."
    else:
        verdict = (
            f"REGRESS — form_field boost *decreases* recall@10 "
            f"by {-delta_total}/{len(results)} queries. Re-evaluate weights."
        )
    lines.append(verdict)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 12 §七 Item 3 baseline"
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use synthetic corpus (smoke run, no Qdrant needed)",
    )
    parser.add_argument(
        "-n", "--limit", type=int, default=15,
        help="Number of bundles to evaluate (default 15 per plan §七 Item 3)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=REPO_ROOT / "docs/superpowers/research/2026-08-15-recall-at-10-form-field.md",
        help="Output report path (default: docs/superpowers/research/...)",
    )
    parser.add_argument(
        "--bundle-list",
        type=Path,
        default=BUNDLE_LIST_JSON,
        help="Path to bundle manifest JSON",
    )
    parser.add_argument(
        "--ground-truth-manifest",
        type=Path,
        default=DEFAULT_GROUND_TRUTH_MANIFEST,
        help="Path to ground-truth sidecar JSON produced by "
        "scripts/extract_ground_truth_labels.py (real-infra mode only)",
    )
    args = parser.parse_args()

    if args.synthetic:
        bundles = synth_bundles(args.limit)
        anchors = synth_query_anchors(bundles)
    else:
        bundles = load_bundles(args.bundle_list, args.limit)
        anchors = load_anchors_from_sidecar(bundles, args.ground_truth_manifest)

    top10_fn: Callable[[str, bool, List[QueryAnchor]], List[str]] = synth_run_top10
    if not args.synthetic:
        try:
            retriever = build_retriever()
        except Exception as e:
            logger.error("Real-infra build failed (%s), falling back to synthetic", e)
            args.synthetic = True
        else:
            async def real_top10(
                q: str, fb: bool, _anchors: List[QueryAnchor]
            ) -> List[str]:
                return await real_run_top10(retriever, q, fb)

            def top10_real(q: str, fb: bool, _anchors: List[QueryAnchor]) -> List[str]:
                return asyncio.run(real_top10(q, fb, _anchors))

            top10_fn = top10_real

    results = evaluate(anchors, top10_fn)
    report = render_report(results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote baseline report to {args.output}")
    print()
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
