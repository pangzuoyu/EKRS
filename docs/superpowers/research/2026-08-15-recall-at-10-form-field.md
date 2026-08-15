# Phase 12 §七 Item 3 — form_field boost recall@10 baseline

_Generated: 2026-08-15_

Per-anchor recall@10 = (queries where expected chunk appears in top-10) / (total queries) for that anchor type.

## Per-anchor recall@10

| anchor_type | total | ON in top-10 | OFF in top-10 | Δ (ON − OFF) |
|-------------|-------|--------------|---------------|---------------|
| form_field | 15 | 15 | 0 | +15 |
| column_header | 15 | 0 | 0 | 0 |
| heading | 15 | 0 | 0 | 0 |

## Per-query detail

| bundle | anchor | query | expected | ON rank | OFF rank |
|--------|--------|-------|----------|---------|----------|
| synth0000 | form_field | 'Lot 0' | synth0000-0000 | 1 | — |
| synth0000 | column_header | 'A105' | synth0000-0001 | — | — |
| synth0000 | heading | 'Material Specification' | synth0000-0002 | — | — |
| synth0001 | form_field | 'Lot 1' | synth0001-0000 | 1 | — |
| synth0001 | column_header | 'A105' | synth0001-0001 | — | — |
| synth0001 | heading | 'Material Specification' | synth0001-0002 | — | — |
| synth0002 | form_field | 'Lot 2' | synth0002-0000 | 1 | — |
| synth0002 | column_header | 'A105' | synth0002-0001 | — | — |
| synth0002 | heading | 'Material Specification' | synth0002-0002 | — | — |
| synth0003 | form_field | 'Lot 3' | synth0003-0000 | 1 | — |
| synth0003 | column_header | 'A105' | synth0003-0001 | — | — |
| synth0003 | heading | 'Material Specification' | synth0003-0002 | — | — |
| synth0004 | form_field | 'Lot 4' | synth0004-0000 | 1 | — |
| synth0004 | column_header | 'A105' | synth0004-0001 | — | — |
| synth0004 | heading | 'Material Specification' | synth0004-0002 | — | — |
| synth0005 | form_field | 'Lot 5' | synth0005-0000 | 1 | — |
| synth0005 | column_header | 'A105' | synth0005-0001 | — | — |
| synth0005 | heading | 'Material Specification' | synth0005-0002 | — | — |
| synth0006 | form_field | 'Lot 6' | synth0006-0000 | 1 | — |
| synth0006 | column_header | 'A105' | synth0006-0001 | — | — |
| synth0006 | heading | 'Material Specification' | synth0006-0002 | — | — |
| synth0007 | form_field | 'Lot 7' | synth0007-0000 | 1 | — |
| synth0007 | column_header | 'A105' | synth0007-0001 | — | — |
| synth0007 | heading | 'Material Specification' | synth0007-0002 | — | — |
| synth0008 | form_field | 'Lot 8' | synth0008-0000 | 1 | — |
| synth0008 | column_header | 'A105' | synth0008-0001 | — | — |
| synth0008 | heading | 'Material Specification' | synth0008-0002 | — | — |
| synth0009 | form_field | 'Lot 9' | synth0009-0000 | 1 | — |
| synth0009 | column_header | 'A105' | synth0009-0001 | — | — |
| synth0009 | heading | 'Material Specification' | synth0009-0002 | — | — |
| synth0010 | form_field | 'Lot 10' | synth0010-0000 | 1 | — |
| synth0010 | column_header | 'A105' | synth0010-0001 | — | — |
| synth0010 | heading | 'Material Specification' | synth0010-0002 | — | — |
| synth0011 | form_field | 'Lot 11' | synth0011-0000 | 1 | — |
| synth0011 | column_header | 'A105' | synth0011-0001 | — | — |
| synth0011 | heading | 'Material Specification' | synth0011-0002 | — | — |
| synth0012 | form_field | 'Lot 12' | synth0012-0000 | 1 | — |
| synth0012 | column_header | 'A105' | synth0012-0001 | — | — |
| synth0012 | heading | 'Material Specification' | synth0012-0002 | — | — |
| synth0013 | form_field | 'Lot 13' | synth0013-0000 | 1 | — |
| synth0013 | column_header | 'A105' | synth0013-0001 | — | — |
| synth0013 | heading | 'Material Specification' | synth0013-0002 | — | — |
| synth0014 | form_field | 'Lot 14' | synth0014-0000 | 1 | — |
| synth0014 | column_header | 'A105' | synth0014-0001 | — | — |
| synth0014 | heading | 'Material Specification' | synth0014-0002 | — | — |

## Verdict

PASS — form_field boost improves recall@10 by 15/45 queries.
