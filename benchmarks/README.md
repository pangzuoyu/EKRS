# Benchmarks

Long-running benchmarks excluded from `make test` / PR CI. Run on
nightly heavy CI or locally when investigating perf regressions.

## How to run

```bash
make bench-chunker        # 10k synthetic docs through the chunker
```

Or directly:

```bash
cd rag && pytest ../benchmarks/test_chunker_10k.py -v -s
# `-s` lets the [CHUNKER-10K] print lines reach the terminal
```

To tighten the p99 threshold without code changes:

```bash
export EKRS_BENCH_CHUNKER_P99_THRESHOLD_SEC=2.0
make bench-chunker
```

## What's measured

| Field | What it tells you |
|-------|-------------------|
| `per_doc_p50_seconds` | Median per-document chunking latency |
| `per_doc_p95_seconds` | 95th-percentile latency (typical "long tail") |
| `per_doc_p99_seconds` | 99th-percentile latency (regression guardrail) |
| `per_doc_max_seconds` | Worst-case single doc |
| `chunks_per_second` | Aggregate throughput (parallel-friendly metric) |
| `max_rss_bytes` | Peak resident set size during the run |

## Report location

Each run writes a JSON file to `benchmarks/results/chunker-10k-<timestamp>.json`.
These files are the canonical baseline records — diff against them
to track perf trends.

## Report schema

```json
{
  "schema": "chunker-10k-1.0",
  "timestamp": "2026-07-24T12:00:00Z",
  "seed": 42,
  "n_documents": 10000,
  "blocks_per_doc_mean": 20.0,
  "total_seconds": 12.345,
  "per_doc_p50_seconds": 0.0008,
  "per_doc_p95_seconds": 0.0019,
  "per_doc_p99_seconds": 0.0027,
  "per_doc_max_seconds": 0.0450,
  "chunks_per_second": 12345.0,
  "max_rss_bytes": 268435456,
  "threshold_p99_seconds": 5.0,
  "threshold_passed": true,
  "platform_note": "ru_maxrss in KB (Linux), converted to bytes"
}
```

## How to interpret

- **`threshold_passed`** is the regression signal. False = something
  changed; look at `total_seconds` and `per_doc_p99_seconds` to see
  how big the regression is.
- **`blocks_per_doc_mean`** documents the synthetic corpus shape.
  When the real parser changes block semantics, regenerate this
  constant so the bench continues to exercise the same shape.
- **`platform_note`** records how `ru_maxrss` was interpreted. Linux
  reports kilobytes; macOS reports bytes. The JSON stores bytes;
  the note tells downstream tooling what unit the raw value was
  in.

## Adding a new benchmark

Follow the same pattern:

1. Add `benchmarks/test_<thing>_10k.py` (or whatever size you choose)
2. Mark with `@pytest.mark.heavy` at module level (pytestmark)
3. Write the JSON to `benchmarks/results/<thing>-<timestamp>.json`
4. Add a `make bench-<thing>` target
5. Update this README

Heavy benchmarks are excluded from `make test` (and PR CI) but run
in the nightly heavy workflow.
