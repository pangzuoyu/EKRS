"""Phase 13b T5.4 — @pytest.mark.heavy wrapper that chains T5.1 → T5.2 → T5.3.

End-to-end acceptance suite entry point. Marked ``heavy`` so it's excluded
from the default addopts (mirrors Phase 12 Task D / Phase 8 T3b pattern).
Run via:

    cd rag && pytest -m heavy tests/integration/test_phase13b_t5_e2e.py -v

Plan: docs/superpowers/plans/2026-08-24-phase13b-T5-e2e-acceptance.md §T5.4
"""
from __future__ import annotations

from pathlib import Path

import pytest


# Defaults point at the same dev stack as the rest of the integration tests.
DEFAULT_CORPUS_ROOT = Path("/home/pangzy/code_project/doc-to-md/output/text")
DEFAULT_RAG_URL = "http://localhost:8000"
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_FTS_PATH = Path("/app/rag/fts.sqlite")
DEFAULT_AUDIT_LOG = Path("/app/rag/audit.log")
DEFAULT_CALLBACK_URL = "http://parser:9000/callback"


@pytest.mark.heavy
def test_phase13b_t5_full_e2e(tmp_path: Path) -> None:
    """End-to-end Phase 13b T5 acceptance: bench + equiv + failover.

    Each step imports the matching script and calls its ``run()`` function
    so a partial failure (one phase failing) still surfaces a clean trace.
    The scripts return typed report objects (PhaseReport / EquivReport /
    FailoverReport) so we can assert against attributes without parsing
    their JSON summary files.
    """
    pytest.importorskip("scripts._phase13b_common")
    pytest.importorskip("scripts.phase13b_poc_bench")
    pytest.importorskip("scripts.phase13b_equiv_check")
    pytest.importorskip("scripts.phase13b_failover_test")

    # Late imports — script modules aren't pytest-discoverable normally,
    # so add scripts/ to sys.path before importing.
    import sys
    SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))

    import os
    token = os.environ.get("PARSER_TOKEN", "")
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not token:
        pytest.skip("PARSER_TOKEN env var unset; skipping T5 e2e")

    from scripts.phase13b_poc_bench import run as run_bench  # type: ignore[import-untyped]
    from scripts.phase13b_equiv_check import run as run_equiv  # type: ignore[import-untyped]
    from scripts.phase13b_failover_test import run as run_failover  # type: ignore[import-untyped]

    # ---- T5.1: bench (Phase A + Phase B on 28-doc Phase12-v10 subset) ----
    phase_a, phase_b = run_bench(
        corpus_root=DEFAULT_CORPUS_ROOT,
        qdrant_url=DEFAULT_QDRANT_URL,
        fts_path=DEFAULT_FTS_PATH,
        rag_url=DEFAULT_RAG_URL,
        token=token,
        callback_url=DEFAULT_CALLBACK_URL,
        admin_key=admin_key or None,
        phase="full",
    )
    # Acceptance line #7: largest doc ≤ 30s.
    assert phase_b.largest_doc_ms <= 30_000, (
        f"Phase B largest doc {phase_b.largest_doc_ms}ms > 30s budget"
    )
    # Acceptance line #8: GPU memory peak ≤ 6 GB.
    assert phase_b.gpu_memory_peak_bytes <= 6 * 1024**3, (
        f"Phase B GPU peak {phase_b.gpu_memory_peak_bytes} > 6GB"
    )
    assert phase_b.n_failed == 0, "Phase B had failures"
    assert phase_a.n_failed == 0, "Phase A had failures"

    # ---- T5.2: retrieval equivalence (Phase A vs Phase B) ----
    equiv = run_equiv(
        corpus_root=DEFAULT_CORPUS_ROOT,
        gt_path=Path(__file__).resolve().parent.parent.parent
        / "deployment" / "phase12-recall-gt.json",
        rag_url=DEFAULT_RAG_URL,
        token=token,
        qdrant_url=DEFAULT_QDRANT_URL,
        collection="rag_documents",
        sample_n=20,
        seed=42,
    )
    # Acceptance line #10: Top-10 Jaccard ≥ 0.99.
    assert equiv.mean_top10_jaccard >= 0.99, (
        f"top10_jaccard {equiv.mean_top10_jaccard:.4f} < 0.99"
    )
    # Acceptance line #9: cosine ≥ 0.999 + sparse Jaccard ≥ 0.95.
    assert equiv.mean_cosine >= 0.999, (
        f"mean_cosine {equiv.mean_cosine:.4f} < 0.999"
    )
    assert equiv.mean_sparse_jaccard >= 0.95, (
        f"mean_sparse_jaccard {equiv.mean_sparse_jaccard:.4f} < 0.95"
    )

    # ---- T5.3: failover (gpu→cpu transition detection ≤ 30s) ----
    if not admin_key:
        pytest.skip("ADMIN_KEY unset; T5.3 skipped (per risk #3)")

    failover = run_failover(
        rag_url=DEFAULT_RAG_URL,
        token=token,
        admin_key=admin_key,
        callback_url=DEFAULT_CALLBACK_URL,
        audit_log_path=DEFAULT_AUDIT_LOG,
        probe_interval_s=5,
        concurrent_docs=10,
    )
    # Acceptance line #11: transition detection ≤ 30s.
    assert failover.transition_detection_ms <= 30_000, (
        f"transition_detection_ms {failover.transition_detection_ms:.0f} > 30s"
    )
    assert failover.all_succeeded, "concurrent docs had failures"
    assert failover.at_least_one_cpu, "no CPU path observed post-failover"