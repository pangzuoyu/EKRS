"""Unit tests for Phase 8 T8-2 — secret rotation offline validator.

Scope (per docs/superpowers/plans/2026-07-23-phase8-scope.md T8-2):
- Catches typo-grade rotations (new token differs from old by 1 char
  within the shared prefix window).
- Accepts distinct strings.
- Rejects equal strings.
- Parses args correctly (--old, --new, --kind, --timestamp).
- Emits a JSON report with rotation_timestamp + kind + units.

The validator MUST be offline — no env reads, no disk reads, no
network. Argparse is the only input channel. JSON to stdout only.
"""
from __future__ import annotations

import json
import re

import pytest

from ekrs_rag.ops.validate_rotation import (
    MIN_TOKEN_LENGTH,
    REQUIRED_KINDS,
    REQUIRED_UNITS,
    TokenTooShortError,
    UnknownKindError,
    build_report,
    main,
    validate_rotation,
)


# ---------------------------------------------------------------------------
# Rejection cases (typo-grade)
# ---------------------------------------------------------------------------


def test_equal_strings_are_rejected() -> None:
    """Old == new → must reject (no-op rotation defeats the purpose)."""
    report = validate_rotation(
        old="abcd1234efgh5678ijkl9012mnop3456",
        new="abcd1234efgh5678ijkl9012mnop3456",
        kind="parser",
    )
    assert report["verdict"] == "reject"
    assert "identical" in report["reason"].lower() or "equal" in report["reason"].lower()


def test_one_char_typo_is_rejected() -> None:
    """A single-char change within a 32-char token = shared prefix 31/32
    = 96.875% (well above the 80% bar). Must reject as typo-grade."""
    old = "abcd1234efgh5678ijkl9012mnop3456"
    # Flip the last character
    new = "abcd1234efgh5678ijkl9012mnop3457"
    report = validate_rotation(old=old, new=new, kind="parser")
    assert report["verdict"] == "reject"
    assert "typo" in report["reason"].lower() or "prefix" in report["reason"].lower()
    assert report["shared_prefix_length"] == 31
    assert report["shared_prefix_ratio"] == pytest.approx(31 / 32)


def test_eighty_percent_shared_prefix_is_rejected() -> None:
    """Boundary check: exactly 80% shared prefix = reject.

    Pair is exactly 40 chars on both sides; the first 32 chars match
    (32 / 40 = 80.0%) so we exercise the threshold without violating
    the >= MIN_TOKEN_LENGTH gate.
    """
    common = "01234567890123456789012345678901"  # 32 chars
    assert len(common) == 32
    old = common + "ABCDEFGH"   # 32+8 = 40
    new = common + "IJKLMNOP"   # first 32 match → ratio = 32/40 = 0.80
    assert len(old) == 40 and len(new) == 40
    report = validate_rotation(old=old, new=new, kind="parser")
    assert report["verdict"] == "reject"
    assert report["shared_prefix_length"] == 32
    assert report["shared_prefix_ratio"] == pytest.approx(0.80)


def test_just_under_eighty_percent_is_accepted() -> None:
    """Boundary check: 77.5% shared prefix = accept.

    Both 40 chars on the shorter side; shared prefix is 31 chars
    (31 / 40 = 77.5%, below the 80% bar).
    """
    common = "0123456789012345678901234567890"   # 31 chars
    assert len(common) == 31
    old = common + "ABCDEFGHI"   # 31+9 = 40
    new = common + "JKLMNOPQR"   # first 31 match → ratio = 31/40 = 0.775
    assert len(old) == 40 and len(new) == 40
    report = validate_rotation(old=old, new=new, kind="parser")
    assert report["verdict"] == "accept"
    assert report["shared_prefix_length"] == 31
    assert report["shared_prefix_ratio"] == pytest.approx(31 / 40)


# ---------------------------------------------------------------------------
# Acceptance cases (distinct strings)
# ---------------------------------------------------------------------------


def test_completely_distinct_strings_are_accepted() -> None:
    """No shared prefix at all → accept."""
    old = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    new = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    report = validate_rotation(old=old, new=new, kind="parser")
    assert report["verdict"] == "accept"
    assert report["shared_prefix_length"] == 0
    assert report["shared_prefix_ratio"] == 0.0


def test_short_shared_prefix_is_accepted() -> None:
    """A small shared prefix (well below 80%) → accept."""
    old = "ab" + "X" * 30
    new = "ab" + "Y" * 30
    report = validate_rotation(old=old, new=new, kind="parser")
    assert report["verdict"] == "accept"
    assert report["shared_prefix_length"] == 2


# ---------------------------------------------------------------------------
# Token length enforcement (matches PARSER_TOKEN / ADMIN_KEY production rules)
# ---------------------------------------------------------------------------


def test_short_old_token_is_rejected() -> None:
    """Old token < MIN_TOKEN_LENGTH → reject with TokenTooShortError.
    The validator refuses to operate on weak secrets regardless of
    prefix similarity."""
    with pytest.raises(TokenTooShortError) as exc:
        validate_rotation(
            old="too-short",
            new="a-completely-different-token-here-32chars",
            kind="parser",
        )
    assert "old" in str(exc.value).lower() and "short" in str(exc.value).lower()


def test_short_new_token_is_rejected() -> None:
    """New token < MIN_TOKEN_LENGTH → reject with TokenTooShortError."""
    with pytest.raises(TokenTooShortError) as exc:
        validate_rotation(
            old="a-completely-different-token-here-32chars",
            new="too-short",
            kind="parser",
        )
    assert "new" in str(exc.value).lower() and "short" in str(exc.value).lower()


def test_min_token_length_constant_is_thirty_two() -> None:
    """PARSER_TOKEN and ADMIN_KEY both enforce ≥32 chars; the validator
    must reject shorter strings BEFORE doing prefix analysis (avoid
    leaking partial-prefix info about weak secrets)."""
    assert MIN_TOKEN_LENGTH == 32


# ---------------------------------------------------------------------------
# Kind validation
# ---------------------------------------------------------------------------


def test_invalid_kind_is_rejected() -> None:
    """kind must be one of {parser, admin}; otherwise UnknownKindError."""
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    with pytest.raises(UnknownKindError):
        validate_rotation(old=a, new=b, kind="database")


def test_parser_kind_is_accepted() -> None:
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    report = validate_rotation(old=a, new=b, kind="parser")
    assert report["kind"] == "parser"


def test_admin_kind_is_accepted() -> None:
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    report = validate_rotation(old=a, new=b, kind="admin")
    assert report["kind"] == "admin"


def test_required_kinds_constant() -> None:
    """Pin the legal kinds to {parser, admin}; adding a new kind
    without updating USAGE.md + the SOP should fail this test."""
    assert REQUIRED_KINDS == ("parser", "admin")


# ---------------------------------------------------------------------------
# Report structure
# ---------------------------------------------------------------------------


def test_report_contains_required_fields() -> None:
    """The JSON report must carry: rotation_timestamp, kind,
    old_token_length, new_token_length, shared_prefix_length,
    shared_prefix_ratio, verdict, reason, required_units."""
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    report = validate_rotation(old=a, new=b, kind="parser")
    required = {
        "rotation_timestamp",
        "kind",
        "old_token_length",
        "new_token_length",
        "shared_prefix_length",
        "shared_prefix_ratio",
        "verdict",
        "reason",
        "required_units",
    }
    assert required.issubset(report.keys()), (
        f"missing fields: {required - report.keys()}"
    )


def test_report_is_json_serializable() -> None:
    """The report must be safely json.dumps-able — no Decimal, set, etc."""
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    report = validate_rotation(old=a, new=b, kind="parser")
    encoded = json.dumps(report)
    decoded = json.loads(encoded)
    assert decoded["verdict"] == "accept"


def test_report_omits_secrets() -> None:
    """The report MUST NOT include the raw tokens. Only prefix-truncated
    forms are allowed (never log or print full secrets)."""
    old = "abcd1234efgh5678ijkl9012mnop3456"
    new = "wxyz9876qrst5432uvwx1098lkar9876"
    report = validate_rotation(old=old, new=new, kind="parser")
    # The full strings must not appear in any field value
    encoded = json.dumps(report)
    assert old not in encoded
    assert new not in encoded


def test_required_units_parser_includes_both_units() -> None:
    """PARSER_TOKEN lives on both parser and rag units. Rotating it
    requires updating both. Required_units must enumerate them."""
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    report = validate_rotation(old=a, new=b, kind="parser")
    assert "parser" in report["required_units"]
    assert "rag" in report["required_units"]


def test_required_units_admin_is_rag_only() -> None:
    """ADMIN_KEY is consumed ONLY by the rag service. Rotating it
    affects the rag unit only."""
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    report = validate_rotation(old=a, new=b, kind="admin")
    assert report["required_units"] == ["rag"]


def test_required_units_constant_lists_both() -> None:
    """Pin the unit enumeration so USAGE.md + SOP stay aligned."""
    assert REQUIRED_UNITS == ("parser", "admin", "rag")


# ---------------------------------------------------------------------------
# CLI / argparse behavior
# ---------------------------------------------------------------------------


def test_cli_rejects_missing_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """No --old / --new → argparse error → exit 2 (argparse convention)."""
    import sys

    monkeypatch.setattr(sys, "argv", ["validate_rotation.py"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_cli_returns_0_on_accept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Accept verdict + valid JSON to stdout → return 0.

    `main()` returns the exit code (the `if __name__ == "__main__"`
    block does `sys.exit(main())`); tests assert the return value
    rather than the SystemExit code so they are robust to that
    convention.
    """
    import sys

    monkeypatch.setattr(sys, "argv", [
        "validate_rotation.py",
        "--old", "a-completely-different-token-here-32chars",
        "--new", "b-completely-different-token-here-32chars",
        "--kind", "parser",
    ])
    rc = main()
    assert rc == 0
    captured = capsys.readouterr()
    decoded = json.loads(captured.out)
    assert decoded["verdict"] == "accept"


def test_cli_returns_1_on_typo_reject(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject verdict → return 1 (distinct from argparse's 2)."""
    import sys

    monkeypatch.setattr(sys, "argv", [
        "validate_rotation.py",
        "--old", "abcd1234efgh5678ijkl9012mnop3456",
        "--new", "abcd1234efgh5678ijkl9012mnop3457",
        "--kind", "parser",
    ])
    rc = main()
    assert rc == 1
    captured = capsys.readouterr()
    decoded = json.loads(captured.out)
    assert decoded["verdict"] == "reject"


def test_cli_json_output_has_iso_timestamp(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The rotation_timestamp must be ISO-8601 (so audit tooling can
    parse it). Format: YYYY-MM-DDTHH:MM:SSZ."""
    import sys

    monkeypatch.setattr(sys, "argv", [
        "validate_rotation.py",
        "--old", "a-completely-different-token-here-32chars",
        "--new", "b-completely-different-token-here-32chars",
        "--kind", "parser",
    ])
    main()
    captured = capsys.readouterr()
    decoded = json.loads(captured.out)
    ts = decoded["rotation_timestamp"]
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts
    ), f"unexpected timestamp format: {ts!r}"


def test_build_report_is_alias_for_validate_rotation() -> None:
    """build_report and validate_rotation must produce identical dicts
    for the same inputs (alias pinned so external callers can use the
    public name without ambiguity)."""
    a = "a-completely-different-token-here-32chars"
    b = "b-completely-different-token-here-32chars"
    r1 = validate_rotation(old=a, new=b, kind="parser")
    r2 = build_report(old=a, new=b, kind="parser")
    # Timestamps differ; compare every non-timestamp field with
    # literal string keys so mypy can verify dict types.
    assert r1["kind"] == r2["kind"]
    assert r1["old_token_length"] == r2["old_token_length"]
    assert r1["new_token_length"] == r2["new_token_length"]
    assert r1["shared_prefix_length"] == r2["shared_prefix_length"]
    assert r1["shared_prefix_ratio"] == r2["shared_prefix_ratio"]
    assert r1["verdict"] == r2["verdict"]
    assert r1["reason"] == r2["reason"]
    assert r1["required_units"] == r2["required_units"]  # both lists, comparable
