"""Offline validator for `PARSER_TOKEN` / `ADMIN_KEY` rotations (Phase 8 T8-2).

Scope (per docs/superpowers/plans/2026-07-23-phase8-scope.md T8-2):

- Reads OLD + NEW tokens from CLI args (never from disk, env, or
  network). The validator MUST be fully offline — security tooling
  that reads secrets from disk would itself become a credential leak.
- Computes longest-common-prefix ratio. Rejects when the ratio is
  ≥ 80% of the shorter token (catches typo-grade rotations: a
  single-character change in a 32-char token = 96.9% shared prefix;
  an "I added 1 char" or "I replaced suffix" mistake hits 100% in
  the prefix zone).
- Rejects when `old == new` (no-op rotation defeats the purpose and
  often signals the operator forgot to actually generate a new token).
- Enforces a minimum length of 32 chars (matches production rules
  in `ekrs_rag.security.parser_token.MIN_TOKEN_LENGTH` and the Pydantic
  Settings config validators).
- Emits a JSON report with `rotation_timestamp`, `kind`, lengths,
  shared-prefix stats, verdict, reason, and the list of deployment
  units that must be updated (`parser` and/or `rag`).
- Prints JSON to stdout; nothing to stderr (so the report is
  pipe-clean into `jq` / log aggregators).

CLI:
    python -m ekrs_rag.ops.validate_rotation \\
        --old "<OLD_PARSER_TOKEN>" --new "<NEW_PARSER_TOKEN>" --kind parser

Exit codes:
    0  verdict == "accept"            (rotation is safe to proceed)
    1  verdict == "reject"           (typo-grade; do not proceed)
    2  CLI / argparse error           (missing or invalid flag)
    3  Validation error              (TokenTooShortError, UnknownKindError)

Why it lives under `ekrs_rag.ops` and not `scripts/`:
    The functional logic of the validator belongs to the RAG service
    (token rotation is RAG's auth surface), so it is unit-tested under
    `rag/tests/unit/test_validate_rotation.py`. The thin entry-point
    shim `scripts/validate_rotation.py` invokes `main()` so operators
    have a stable script name they can call from the runbook.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import TypedDict

# Production rules (match ekrs_rag.security.parser_token.MIN_TOKEN_LENGTH
# and the Pydantic validator on settings.PARSER_TOKEN / settings.ADMIN_KEY).
MIN_TOKEN_LENGTH: int = 32

# Whitelisted kinds. Adding a new kind here is a contract change — the
# operator-facing SOP (docs/SECRET-ROTATION.md) must list the new kind
# in step 0 ("which kind am I rotating?").
REQUIRED_KINDS: tuple[str, ...] = ("parser", "admin")

# Deployment units that must be updated when a given kind rotates.
#   parser → both `parser` (caller) and `rag` (server) env vars
#   admin  → `rag` only (admin endpoints live in the RAG service)
REQUIRED_UNITS: tuple[str, ...] = ("parser", "admin", "rag")

# Typo-grade cutoff: if the longest-common-prefix is >= this fraction of
# the SHORTER token's length, reject. 0.80 is the empirical value chosen
# from the real-world "1-char change in a 32-char secret" scenario,
# which yields 0.969 (well above the bar) and "1-char change in a 5-char
# secret" which is not a real secret anyway (rejected on length first).
_TYPO_RATIO_THRESHOLD: float = 0.80


class TokenTooShortError(ValueError):
    """Raised when either the old or new token is shorter than
    MIN_TOKEN_LENGTH. The validator refuses to operate on weak
    secrets regardless of how similar they look, so it cannot leak
    partial-prefix info about low-entropy strings."""


class UnknownKindError(ValueError):
    """Raised when `--kind` is not in REQUIRED_KINDS. This is a CLI
    contract error, not a rotation problem; it is reported via exit 3
    so the operator's automation can branch on it."""


class _RotationReportRequired(TypedDict):
    """Required keys of the rotation report.

    `rotation_timestamp`, `kind`, `verdict`, `reason`, and
    `required_units` are all required strings / lists. Numeric
    stats (`*_length`, `shared_prefix_*`) are ints + float.
    """

    rotation_timestamp: str
    kind: str
    old_token_length: int
    new_token_length: int
    shared_prefix_length: int
    shared_prefix_ratio: float
    verdict: str
    reason: str
    required_units: list[str]


class RotationReport(_RotationReportRequired, total=False):
    """Public return type of `validate_rotation` / `build_report`.
    Subclasses nothing — adding optional fields would be a breaking
    change for downstream jq scripts that pin field names.
    """


def _longest_common_prefix(a: str, b: str) -> int:
    """Return the length of the longest shared prefix between `a` and
    `b`. O(min(len(a), len(b))) — exits as soon as a mismatch is
    observed."""
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    return limit


def _required_units_for(kind: str) -> list[str]:
    if kind == "parser":
        return ["parser", "rag"]
    if kind == "admin":
        return ["rag"]
    # Defensive: validate_rotation() guards this before reaching here.
    raise UnknownKindError(f"unknown rotation kind: {kind!r}")


def validate_rotation(
    old: str,
    new: str,
    kind: str,
    *,
    now: datetime | None = None,
) -> RotationReport:
    """Validate a planned token rotation; return a JSON-serializable
    report dict.

    Args:
        old: The current (existing) secret as a string.
        new: The proposed new secret as a string.
        kind: One of `REQUIRED_KINDS` — drives the required_units
            field in the report.
        now: Override for the rotation timestamp (defaults to
            `datetime.now(timezone.utc)`). Useful in tests.

    Returns:
        dict with keys:
            rotation_timestamp, kind, old_token_length, new_token_length,
            shared_prefix_length, shared_prefix_ratio, verdict, reason,
            required_units.

    Raises:
        TokenTooShortError: Either token is below MIN_TOKEN_LENGTH.
        UnknownKindError: `kind` is not in REQUIRED_KINDS.
    """
    if kind not in REQUIRED_KINDS:
        raise UnknownKindError(
            f"unknown kind {kind!r}; allowed: {list(REQUIRED_KINDS)}"
        )
    if len(old) < MIN_TOKEN_LENGTH:
        raise TokenTooShortError(
            f"old token is too short ({len(old)} chars); "
            f"minimum is {MIN_TOKEN_LENGTH}"
        )
    if len(new) < MIN_TOKEN_LENGTH:
        raise TokenTooShortError(
            f"new token is too short ({len(new)} chars); "
            f"minimum is {MIN_TOKEN_LENGTH}"
        )

    shared_prefix = _longest_common_prefix(old, new)
    shorter = min(len(old), len(new))
    ratio = shared_prefix / shorter if shorter else 0.0

    when = now or datetime.now(timezone.utc)
    # ISO-8601 with trailing Z (UTC, second precision). Stable enough
    # for audit-log join keys without exposing the operator's local
    # timezone offset.
    rotation_timestamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if old == new:
        verdict = "reject"
        reason = "old and new tokens are identical; rotate to a freshly generated secret"
    elif ratio >= _TYPO_RATIO_THRESHOLD:
        verdict = "reject"
        reason = (
            f"shared prefix is {shared_prefix} of {shorter} chars "
            f"({ratio:.1%}); typo-grade rotation — generate a fresh "
            f"unrelated secret"
        )
    else:
        verdict = "accept"
        reason = "tokens are sufficiently distinct"

    return {
        "rotation_timestamp": rotation_timestamp,
        "kind": kind,
        "old_token_length": len(old),
        "new_token_length": len(new),
        "shared_prefix_length": shared_prefix,
        "shared_prefix_ratio": round(ratio, 6),
        "verdict": verdict,
        "reason": reason,
        "required_units": _required_units_for(kind),
    }


# Public alias — `validate_rotation` is the canonical verb; `build_report`
# exists so callers who prefer the noun form can import it without
# surprising anyone.
build_report = validate_rotation


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_rotation",
        description=(
            "Validate a PARSER_TOKEN or ADMIN_KEY rotation. "
            "Returns a JSON report on stdout; exits 0 if the rotation "
            "is safe, 1 if it looks like a typo."
        ),
    )
    parser.add_argument(
        "--old",
        required=True,
        help="Current secret (will NOT be persisted or logged).",
    )
    parser.add_argument(
        "--new",
        required=True,
        help="Proposed new secret (will NOT be persisted or logged).",
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=list(REQUIRED_KINDS),
        help="Which secret is being rotated; drives required_units.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the intended exit code so callers can
    `sys.exit(main())` or assert on it directly in tests."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        report = validate_rotation(old=args.old, new=args.new, kind=args.kind)
    except (TokenTooShortError, UnknownKindError) as exc:
        # Contract violations are distinct from a "reject" verdict —
        # the operator's CLI invocation was wrong; report that as exit 3
        # so failed validations don't look like normal "reject" rotations.
        error_report = {
            "rotation_timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "verdict": "error",
            "reason": str(exc),
        }
        sys.stdout.write(json.dumps(error_report, separators=(",", ":")) + "\n")
        return 3

    sys.stdout.write(json.dumps(report, separators=(",", ":")) + "\n")

    if report["verdict"] == "accept":
        return 0
    if report["verdict"] == "reject":
        return 1
    # Defensive — `validate_rotation` only emits the two verdicts above.
    return 1


__all__ = (
    "MIN_TOKEN_LENGTH",
    "REQUIRED_KINDS",
    "REQUIRED_UNITS",
    "TokenTooShortError",
    "UnknownKindError",
    "validate_rotation",
    "build_report",
    "main",
)


if __name__ == "__main__":
    sys.exit(main())
