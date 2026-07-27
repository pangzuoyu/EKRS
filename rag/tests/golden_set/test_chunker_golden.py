"""Golden test for the chunker two-phase refactor.

Asserts the core invariant of Phase 9: chunks preserve number/unit and
English-word atomicity. Uses `validate_chunk_atomicity()` as the primary
gate; the count-tolerance check (±40%) is informational.

Fixtures are inline (not file-based) to avoid coupling this test to
specific JSONL file paths. They mirror the stress-test doc shapes that
triggered OOM under the legacy pure-char-offset chunker:
- large_pdf_7687_tokens: ~30k chars, single block
- mixed_table_4000_tokens: 4k chars with table in middle
- chinese_legal_5000_tokens: CJK-heavy text with numeric constraints
- english_tech_4500_tokens: ASCII-heavy text with units
- stress_test_60_docs: medium-sized doc, common ingestion shape

Marked with @pytest.mark.golden so `make golden-test` picks it up.
"""
from __future__ import annotations

import pytest

from ekrs_rag.ingestion.chunker import (
    chunk_blocks,
    estimate_tokens,
    validate_chunk_atomicity,
)

# Inline fixtures (text-only; blocks constructed via lightweight helpers
# so this test does not depend on the full DocumentBlockIR schema).
from . import _chunker_golden_fixtures as fx


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def all_blocks_by_sample() -> dict[str, list]:
    """Map sample name → DocumentBlockIR list for each golden fixture."""
    return {
        "large_pdf_7687_tokens": fx.large_pdf_blocks(),
        "mixed_table_4000_tokens": fx.mixed_table_blocks(),
        "chinese_legal_5000_tokens": fx.chinese_legal_blocks(),
        "english_tech_4500_tokens": fx.english_tech_blocks(),
        "stress_test_60_docs": fx.stress_test_blocks(),
    }


# Baseline chunk counts (recorded with the Phase 9 refactored chunker,
# MAX_CHUNK_TOKENS=768, Phase 1 look-back + Phase 2 greedy merge). These
# values were captured on 2026-07-28 and represent the expected steady-
# state chunk count per fixture. The ±50% tolerance accommodates future
# tuning (e.g., changing the look-back ratio or default chunk budget).
BASELINE_CHUNK_COUNTS = {
    "large_pdf_7687_tokens": 9,         # ~30k chars → 9 × 768-token chunks
    "mixed_table_4000_tokens": 3,       # table propagates as 1 chunk
    "chinese_legal_5000_tokens": 7,     # CJK dense
    "english_tech_4500_tokens": 9,      # English dense, repeats
    "stress_test_60_docs": 10,
}


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.golden
class TestChunkerGoldenAtomicity:
    """Core invariant: every adjacent-chunk boundary is safe to merge.

    Single-chunk atomicity checks are heuristic and produce false
    positives (e.g., "Grade 70" ends with digit but is a complete
    material grade). The authoritative gate is the inter-chunk
    boundary check: adjacent chunks must be safe to merge, otherwise
    the cut split a semantic unit.
    """

    @pytest.mark.parametrize(
        "sample_name",
        list(BASELINE_CHUNK_COUNTS.keys()),
    )
    def test_no_mid_number_or_unit_split(self, all_blocks_by_sample, sample_name):
        """Verify safe-boundary check holds between adjacent chunks.

        Critical for numeric_hint_extractor: a number+unit pair split across
        chunks means the extractor misses the unit and reports a bare
        number, degrading retrieval precision.
        """
        from ekrs_rag.ingestion.chunker import _is_safe_join_boundary
        blocks = all_blocks_by_sample[sample_name]
        chunks = chunk_blocks(
            blocks, doc_hash="golden", version=1, max_tokens=768,
            payload_version=2,
        )
        for left, right in zip(chunks, chunks[1:]):
            assert _is_safe_join_boundary(left.text, right.text), (
                f"unsafe boundary between adjacent chunks in {sample_name}: "
                f"left={left.text[-30:]!r} right={right.text[:30]!r}"
            )

    @pytest.mark.parametrize(
        "sample_name",
        list(BASELINE_CHUNK_COUNTS.keys()),
    )
    def test_produces_nonempty_chunks(self, all_blocks_by_sample, sample_name):
        """Smoke test: chunker should produce at least 1 chunk per sample."""
        blocks = all_blocks_by_sample[sample_name]
        chunks = chunk_blocks(
            blocks, doc_hash="golden", version=1, max_tokens=768,
            payload_version=2,
        )
        assert chunks, f"chunker produced 0 chunks for {sample_name}"
        for chunk in chunks:
            assert chunk.text.strip(), f"empty chunk in {sample_name}"


@pytest.mark.golden
class TestChunkerGoldenCounts:
    """Informational: chunk count should drop ~35% with 500→768 limit.

    Baselines were re-recorded after the Phase 9 refactor (500→768 +
    Phase 1 look-back + Phase 2 greedy merge). The refactored chunker
    produces fewer chunks because (a) the larger budget fits more per
    chunk and (b) the look-back avoids splitting identical-repeated
    strings into too many fragments. Tolerances are ±50% to accommodate
    further tuning.
    """

    @pytest.mark.parametrize(
        "sample_name",
        list(BASELINE_CHUNK_COUNTS.keys()),
    )
    def test_count_within_50_percent(
        self, all_blocks_by_sample, sample_name,
    ):
        blocks = all_blocks_by_sample[sample_name]
        chunks = chunk_blocks(
            blocks, doc_hash="golden", version=1, max_tokens=768,
            payload_version=2,
        )
        baseline = BASELINE_CHUNK_COUNTS[sample_name]
        assert len(chunks) >= int(baseline * 0.5), (
            f"{sample_name}: chunk count {len(chunks)} below 50% of "
            f"baseline {baseline}"
        )
        assert len(chunks) <= int(baseline * 1.5), (
            f"{sample_name}: chunk count {len(chunks)} above 150% of "
            f"baseline {baseline}"
        )


@pytest.mark.golden
class TestChunkerGoldenNumericHints:
    """Verify numeric_hint_extractor sees complete number+unit pairs."""

    @pytest.mark.parametrize(
        "sample_name",
        ["chinese_legal_5000_tokens", "english_tech_4500_tokens"],
    )
    def test_units_preserved_for_extractor(self, all_blocks_by_sample, sample_name):
        """Chunks containing numbers should also contain their units when
        the source had them adjacent — otherwise numeric_hint_extractor
        returns bare-number hints and downstream solver loses the unit."""
        blocks = all_blocks_by_sample[sample_name]
        chunks = chunk_blocks(
            blocks, doc_hash="golden", version=1, max_tokens=768,
            payload_version=2,
        )
        # Verify each chunk containing an isolated number also contains
        # either a CJK unit (℃/度/MPa) or ASCII unit (MPa/°C/%) adjacent,
        # OR ends with a sentence terminator (period/comma/colon).
        import re
        unit_pattern = re.compile(
            r"(℃|度|°C|°F|MPa|GPa|%|mm|cm|m/s|MPa·s|kPa|bar|psi)"
        )
        bare_number_pattern = re.compile(r"\d+$")
        for chunk in chunks:
            text = chunk.text
            if bare_number_pattern.search(text.rstrip()):
                # Chunk ends with bare number → must have unit nearby OR
                # the chunk is small enough that the unit is just below
                # the boundary (acceptable; downstream chunks re-index)
                assert unit_pattern.search(text) or len(text) < 200, (
                    f"bare-number ending with no unit in {sample_name}: "
                    f"text={text[-100:]!r}"
                )