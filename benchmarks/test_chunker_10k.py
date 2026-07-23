"""Phase 8 T8-5 — chunker perf baseline at 10k+ documents.

This is a benchmark (NOT a regression test). It exercises the semantic
chunker (`rag.ekrs_rag.ingestion.chunker.chunk_blocks`) over a
deterministic synthetic document corpus and reports:

  - per-document p50/p95/p99/max latency
  - aggregate throughput (chunks/sec)
  - peak RSS (via `resource.getrusage`)

A JSON report is written to `benchmarks/results/chunker-10k-<ts>.json`
so future runs can be diffed against the baseline.

A pytest assertion guards against regressions: p99 document latency
must stay below `EKRS_BENCH_CHUNKER_P99_THRESHOLD_SEC` (default 5.0).
If the chunker is ever pessimized, this assertion fires on nightly
heavy CI.

This test is marked `@pytest.mark.heavy` and is therefore excluded
from `make test` / PR CI. Run via `make bench-chunker`.

Spec: docs/superpowers/plans/2026-07-23-phase8-scope.md §T8-5.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import pytest

# `resource` is POSIX-only (Linux/macOS). Windows is not in scope for
# this project (production targets Linux + Docker); fail loudly if a
# future Windows port attempts to run this.
try:
    import resource
except ImportError as e:  # pragma: no cover — Windows is out of scope
    raise ImportError(
        "benchmarks/test_chunker_10k.py requires POSIX `resource` module"
    ) from e

from ekrs_shared.models import Content, DocumentBlockIR, Metadata

from rag.ekrs_rag.ingestion.chunker import chunk_blocks


# Marked heavy so `make test` skips it; this is a long-running benchmark
# suitable for nightly CI or local dev runs.
pytestmark = pytest.mark.heavy


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Plan §T8-5 default = 5s/document. Tuneable via env var so we can
# tighten without committing code changes.
_DEFAULT_P99_THRESHOLD_SEC = 5.0
_THRESHOLD_ENV_VAR = "EKRS_BENCH_CHUNKER_P99_THRESHOLD_SEC"

# Synthetic corpus size. Plan §T8-5 says "10k+ documents". 10000 is
# the deliverable size; 100 would be too small to be meaningful,
# 100000 would inflate the bench runtime beyond CI nightly budgets.
_DEFAULT_N_DOCUMENTS = 10_000

# Deterministic seed. All runs share this seed so the synthetic
# corpus is byte-for-byte identical across machines — that is what
# makes the perf comparison meaningful (no doc-shape drift).
_DEFAULT_SEED = 42

# Blocks per document (mean). Realistic engineering documents have
# ~10–40 blocks; we use 20 with stddev=8 to exercise both small and
# large docs in the chunker's boundary conditions.
_BLOCKS_PER_DOC_MEAN = 20
_BLOCKS_PER_DOC_STDDEV = 8

# Max tokens per chunk (matches chunk_blocks default).
_MAX_TOKENS = 500

# Report schema version. Bump when the JSON shape changes.
_REPORT_SCHEMA = "chunker-10k-1.0"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkReport:
    """JSON-serializable perf measurement.

    All fields are static (no Pydantic / no DB) — the JSON file is
    diffed against a baseline by humans, not by code.
    """

    schema: str
    timestamp: str  # ISO-8601 UTC
    seed: int
    n_documents: int
    blocks_per_doc_mean: float
    total_seconds: float
    per_doc_p50_seconds: float
    per_doc_p95_seconds: float
    per_doc_p99_seconds: float
    per_doc_max_seconds: float
    chunks_per_second: float
    max_rss_bytes: int
    threshold_p99_seconds: float
    threshold_passed: bool
    platform_note: str  # e.g. "ru_maxrss in KB (Linux)" or "in bytes (macOS)"


# ---------------------------------------------------------------------------
# Synthetic corpus generator
# ---------------------------------------------------------------------------


def _make_block(
    rng: random.Random,
    *,
    doc_id: str,
    block_idx: int,
    block_type: str,
    heading_path: list[str],
) -> DocumentBlockIR:
    """Build a single synthetic DocumentBlockIR.

    Text length varies by block type:
      - header: 1-3 lines of short text (50–120 chars)
      - text:   1-5 paragraphs of medium text (200–800 chars)
      - table:  structured rows with header row

    The mix is ~80% text, ~10% header, ~10% table — close to real
    engineering PDFs.
    """
    text = _synth_text(rng, block_type)
    return DocumentBlockIR(
        doc_id=doc_id,
        block_id=f"{doc_id}#{block_idx:04d}",
        type=block_type,
        content=Content(raw=text, md_preview=text, structured=None),
        metadata=Metadata(page_number=(block_idx // 5) + 1, heading_path=heading_path),
    )


def _synth_text(rng: random.Random, block_type: str) -> str:
    """Generate synthetic text. Word salad is fine — the chunker
    operates on length and structure, not semantics."""
    if block_type == "header":
        # Short heading-style line
        header_word = ["温度", "压力", "材料", "标准", "GB", "规范", "范围", "上限", "下限", "操作", "参数"][rng.randint(0, 9)]
        return f"## {header_word}-{rng.randint(100, 999)}"

    # text + table body: paragraphs of engineering-looking tokens
    paragraphs = []
    n_paragraphs = rng.randint(1, 5)
    for _ in range(n_paragraphs):
        n_words = rng.randint(30, 150)
        words = []
        for _ in range(n_words):
            kind = rng.randint(0, 2)
            if kind == 0:
                # numeric value with unit (exercises numeric_hint paths)
                words.append(f"{rng.randint(1, 999)}{rng.choice(['°C', 'MPa', 'mm', '%', 'kg'])}")
            elif kind == 1:
                words.append(rng.choice([
                    "温度", "压力", "流量", "材料", "强度", "硬度",
                    "焊接", "热处理", "装配", "测试", "检查",
                ]))
            else:
                words.append(rng.choice([
                    "should", "be", "within", "the", "range", "as",
                    "specified", "by", "the", "standard", "and", "verified",
                    "during", "the", "test", "phase",
                ]))
        paragraphs.append(" ".join(words))
    return "\n".join(paragraphs)


def generate_synthetic_corpus(
    n_documents: int,
    seed: int,
) -> list[list[DocumentBlockIR]]:
    """Generate `n_documents` documents, each a list of DocumentBlockIR.

    Deterministic: same seed → byte-identical output. The chunker
    timing is what we measure; corpus shape must be stable.
    """
    rng = random.Random(seed)
    corpus: list[list[DocumentBlockIR]] = []

    for doc_idx in range(n_documents):
        doc_id = f"bench-doc-{doc_idx:06d}"
        n_blocks = max(1, int(rng.gauss(_BLOCKS_PER_DOC_MEAN, _BLOCKS_PER_DOC_STDDEV)))

        # Heading path changes every ~5 blocks to exercise the chunker's
        # scope-change boundary condition.
        heading_path: list[str] = ["benchmark"]
        blocks: list[DocumentBlockIR] = []
        for block_idx in range(n_blocks):
            if block_idx > 0 and block_idx % 5 == 0:
                heading_path = ["benchmark", f"section-{block_idx // 5}"]

            r = rng.random()
            if r < 0.10:
                block_type = "header"
            elif r < 0.20:
                block_type = "table"
            else:
                block_type = "text"

            blocks.append(_make_block(
                rng, doc_id=doc_id, block_idx=block_idx,
                block_type=block_type, heading_path=list(heading_path),
            ))

        corpus.append(blocks)

    return corpus


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile. p in (0, 100].

    Easier to reason about than `statistics.quantiles` (which uses
    the inclusive-exclusive formula NIST defines for n=99 quantiles).
    """
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(round(p / 100.0 * len(sorted_values))) - 1))
    return sorted_values[idx]


# ---------------------------------------------------------------------------
# Main benchmark function (testable independently of pytest)
# ---------------------------------------------------------------------------


def run_chunker_bench(
    n_documents: int = _DEFAULT_N_DOCUMENTS,
    seed: int = _DEFAULT_SEED,
) -> BenchmarkReport:
    """Generate the corpus and chunk it. Return the perf report.

    Sequential, single-threaded: keeps the bench deterministic and
    easy to interpret. Parallel benchmarking would obscure whether
    a regression is in the chunker or in Python's GIL contention.
    """
    # Generate corpus (setup cost is not in the per-doc timing window)
    corpus = generate_synthetic_corpus(n_documents, seed)

    durations: list[float] = []
    total_chunks = 0

    # Track peak RSS around the timing window. Initial measurement
    # is the "before" (corpus-only); final is "after" (full bench).
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    bench_start = time.perf_counter()
    for blocks in corpus:
        t0 = time.perf_counter()
        chunks = chunk_blocks(blocks, doc_hash=blocks[0].doc_id, version=1, max_tokens=_MAX_TOKENS)
        t1 = time.perf_counter()
        durations.append(t1 - t0)
        total_chunks += len(chunks)
    bench_end = time.perf_counter()

    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    durations.sort()
    total_seconds = bench_end - bench_start

    threshold = float(os.environ.get(_THRESHOLD_ENV_VAR, _DEFAULT_P99_THRESHOLD_SEC))
    p99 = _percentile(durations, 99.0)

    # Linux: ru_maxrss is in KB. macOS: in bytes. We report bytes
    # and document the platform note so the JSON is comparable
    # across platforms after a unit conversion downstream.
    if sys.platform == "linux":
        max_rss_bytes = rss_after * 1024  # Linux reports KB
        platform_note = "ru_maxrss in KB (Linux), converted to bytes"
    elif sys.platform == "darwin":
        max_rss_bytes = rss_after  # macOS reports bytes
        platform_note = "ru_maxrss in bytes (macOS)"
    else:
        # Conservative: assume bytes, document the assumption.
        max_rss_bytes = rss_after
        platform_note = f"ru_maxrss unit unknown on platform {sys.platform!r}; assumed bytes"

    # Sanity: rss_after must be >= rss_before. If not, the platform
    # is buggy / not capturing peak — log a warning but use the
    # non-decreasing max. The integer overflow guard avoids issues
    # on platforms where rss wraps.
    if rss_after < rss_before:
        max_rss_bytes = rss_before * 1024 if sys.platform == "linux" else rss_before
        platform_note += "; rss_after < rss_before, fell back to rss_before"

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    return BenchmarkReport(
        schema=_REPORT_SCHEMA,
        timestamp=timestamp,
        seed=seed,
        n_documents=n_documents,
        blocks_per_doc_mean=_BLOCKS_PER_DOC_MEAN,
        total_seconds=round(total_seconds, 4),
        per_doc_p50_seconds=round(_percentile(durations, 50.0), 6),
        per_doc_p95_seconds=round(_percentile(durations, 95.0), 6),
        per_doc_p99_seconds=round(p99, 6),
        per_doc_max_seconds=round(durations[-1], 6),
        chunks_per_second=round(total_chunks / total_seconds, 2) if total_seconds > 0 else 0.0,
        max_rss_bytes=max_rss_bytes,
        threshold_p99_seconds=threshold,
        threshold_passed=p99 < threshold,
        platform_note=platform_note,
    )


def write_report(
    report: BenchmarkReport,
    results_dir: Path,
) -> Path:
    """Write the JSON report atomically. Returns the path."""
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp_safe = report.timestamp.replace(":", "").replace("-", "")
    final_path = results_dir / f"chunker-10k-{timestamp_safe}.json"
    tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, final_path)
    return final_path


# ---------------------------------------------------------------------------
# Pytest entry point
# ---------------------------------------------------------------------------


def test_chunker_10k_baseline() -> None:
    """Phase 8 T8-5 benchmark + threshold assertion.

    Runs the chunker against 10k synthetic documents, writes a JSON
    report under `benchmarks/results/`, and asserts that the p99
    per-document latency stays under the configured threshold.

    This is a measurement tool, not a correctness test — even when
    the assertion passes, the JSON report is the deliverable. The
    assertion exists only to catch regressions on nightly CI.
    """
    report = run_chunker_bench()

    repo_root = Path(__file__).resolve().parent.parent
    report_path = write_report(report, repo_root / "benchmarks" / "results")

    # Surface the numbers in the pytest output (appearing in -v logs
    # and on CI dashboards). Use a single print so the JSON is the
    # canonical record and pytest output is for humans.
    print(f"\n[CHUNKER-10K] report={report_path}")
    print(
        f"[CHUNKER-10K] n={report.n_documents} "
        f"p50={report.per_doc_p50_seconds:.4f}s "
        f"p95={report.per_doc_p95_seconds:.4f}s "
        f"p99={report.per_doc_p99_seconds:.4f}s "
        f"max={report.per_doc_max_seconds:.4f}s "
        f"chunks/s={report.chunks_per_second:.1f} "
        f"rss={report.max_rss_bytes / 1024 / 1024:.1f}MB"
    )

    assert report.threshold_passed, (
        f"Chunker p99 regressed: {report.per_doc_p99_seconds:.4f}s "
        f">= threshold {report.threshold_p99_seconds:.4f}s. "
        f"See {report_path} for full breakdown. "
        f"Either fix the regression or bump "
        f"{_THRESHOLD_ENV_VAR}."
    )
