# Phase 12 §七 Item 3 — form_field boost recall@10 baseline

_Generated: 2026-08-19_

Per-anchor recall@10 = (queries where expected chunk appears in top-10) / (total queries) for that anchor type.

## Per-anchor recall@10

| anchor_type | total | ON in top-10 | OFF in top-10 | Δ (ON − OFF) |
|-------------|-------|--------------|---------------|---------------|
| form_field | 7 | 6 | 6 | 0 |
| column_header | 0 | 0 | 0 | 0 |
| heading | 0 | 0 | 0 | 0 |

## Per-query detail

| bundle | anchor | query | expected | ON rank | OFF rank |
|--------|--------|-------|----------|---------|----------|
| 000151778ca35475 | form_field | 'Lot 0' | 00015177-0002 | 1 | 2 |
| 03733760454ba6b7 | form_field | 'Lot 11' | 03733760-0002 | 1 | 1 |
| 0569ee094a3746b1 | form_field | 'Lot 44' | 0569ee09-0002 | 1 | 1 |
| 05a17ee938e0eddb | form_field | 'Lot 16' | 05a17ee9-0002 | 1 | 2 |
| 06089e20e1f6ca14 | form_field | 'Lot 10' | 06089e20-0002 | — | — |
| 07aee7a42e608e76 | form_field | 'Lot 43' | 07aee7a4-0002 | 1 | 1 |
| 09649e4676322569 | form_field | 'Lot 52' | 09649e46-0002 | 1 | 2 |

## Verdict

NEUTRAL — boost has no measurable effect at top-10.
