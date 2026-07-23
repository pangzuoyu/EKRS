#!/usr/bin/env python3
"""Thin entry-point shim for the rotation validator (Phase 8 T8-2).

Why a shim and not the real script: the implementation lives under
`rag/ekrs_rag/ops/validate_rotation.py` so it can be unit-tested as a
proper Python module under `rag/tests/unit/test_validate_rotation.py`.
This file gives operators a stable script name to call from the SOP
(`docs/SECRET-ROTATION.md`).

Usage (see docs/SECRET-ROTATION.md for the full procedure):

    python scripts/validate_rotation.py \\
        --old "$PARSER_TOKEN" --new "$NEW_PARSER_TOKEN" --kind parser

Exit codes:

    0  accept  — rotation is safe to proceed
    1  reject  — typo-grade; do not proceed
    2  CLI / argparse error
    3  validation error (token too short, unknown kind)

The full module lives at `ekrs_rag.ops.validate_rotation`. This shim
exists so the runbook can spell out a script path that does not move.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add rag/ to sys.path so this entry point can resolve `ekrs_rag` when
# invoked from anywhere in the repo. Matches the precedent set by
# `scripts/load_golden_fixtures.py` (operator tooling that depends on
# rag/ but is launched as a top-level script).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "rag"))

from ekrs_rag.ops.validate_rotation import main  # noqa: E402  (sys.path adjusted above)

if __name__ == "__main__":
    sys.exit(main())
