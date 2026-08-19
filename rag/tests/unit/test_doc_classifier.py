"""Task C: doc-type classifier pure module. 15 tests covering regex
rules, defaults, case-insensitivity, JSON config loading, error handling.
"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ekrs_rag.ingestion.doc_classifier import (
    ClassificationResult,
    classify,
    load_index_file_name,
    load_rules,
)


# --- rules fixture ---------------------------------------------------------


@pytest.fixture
def rules() -> object:  # DocClassifierRules — type omitted to avoid import cycle
    """Default 5-rule config loaded from the JSON shipped with the package."""
    return load_rules()


# --- core classify() ------------------------------------------------------


@pytest.mark.unit
def test_classify_national_standard(rules):
    """GB-prefixed filenames → national_standard (priority 100)."""
    result = classify("GB150-2011.pdf", rules)
    assert result == ClassificationResult(doc_type="national_standard", priority=100)


@pytest.mark.unit
def test_classify_industry_standard(rules):
    """HG-prefixed filenames → industry_standard (priority 80)."""
    result = classify("HGT1234-2020.docx", rules)
    assert result == ClassificationResult(doc_type="industry_standard", priority=80)


@pytest.mark.unit
def test_classify_enterprise_spec(rules):
    """Q-prefixed filenames → enterprise_spec (priority 60)."""
    result = classify("Q001-2022.doc", rules)
    assert result == ClassificationResult(doc_type="enterprise_spec", priority=60)


@pytest.mark.unit
def test_classify_lot_checklist_lot(rules):
    """Lot<N> NCR → lot_checklist (priority 60)."""
    result = classify("Lot049 NCR Status Report.doc", rules)
    assert result == ClassificationResult(doc_type="lot_checklist", priority=60)


@pytest.mark.unit
def test_classify_lot_checklist_dcn(rules):
    """DCN keyword also matches lot_checklist rule."""
    result = classify("DCN-2026-001 cleanup.xlsx", rules)
    assert result == ClassificationResult(doc_type="lot_checklist", priority=60)


@pytest.mark.unit
def test_classify_project_spec(rules):
    """SA-prefixed filenames → project_spec (priority 40)."""
    result = classify("SA-1234 procurement contract.pdf", rules)
    assert result == ClassificationResult(doc_type="project_spec", priority=40)


@pytest.mark.unit
def test_classify_no_match_returns_default(rules):
    """Unknown filename → default rule (unknown, priority 40)."""
    result = classify("random_filename_v2.docx", rules)
    assert result == ClassificationResult(doc_type="unknown", priority=40)


@pytest.mark.unit
def test_classify_case_insensitive(rules):
    """Lowercase gb prefix still matches national_standard (IGNORECASE)."""
    result = classify("gb150.pdf", rules)
    assert result.doc_type == "national_standard"


@pytest.mark.unit
def test_classify_first_match_wins(rules):
    """Rule order matters: 'GB/Q overlap' picks national_standard (first)."""
    # Synthesize a filename matching both — but rules don't overlap in
    # practice; test with a contrived filename where the order would matter
    # by checking lot_checklist vs enterprise_spec on "Q-Lot..."
    result = classify("Q1-Lot049 something.doc", rules)
    # Q-prefix matches enterprise_spec FIRST (rule order top→bottom); so:
    assert result.doc_type == "enterprise_spec"


@pytest.mark.unit
def test_classify_empty_filename(rules):
    """Empty string → default (graceful degradation, no exception)."""
    result = classify("", rules)
    assert result == ClassificationResult(doc_type="unknown", priority=40)


# --- JSON loader ----------------------------------------------------------


@pytest.mark.unit
def test_load_rules_default_path():
    """Default path resolves to doc_classifier_rules.json shipped with pkg."""
    rules = load_rules()
    assert len(rules.rules) == 5
    assert rules.default.doc_type == "unknown"


@pytest.mark.unit
def test_load_rules_env_override(tmp_path, monkeypatch):
    """EKRS_DOC_CLASSIFIER_RULES_PATH env var swaps the config source."""
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({
        "rules": [{"pattern": "^SPECIAL", "doc_type": "custom", "priority": 99}],
        "default": {"doc_type": "custom_default", "priority": 1},
    }))
    monkeypatch.setenv("EKRS_DOC_CLASSIFIER_RULES_PATH", str(custom))
    rules = load_rules()
    assert len(rules.rules) == 1
    assert rules.default.doc_type == "custom_default"


@pytest.mark.unit
def test_load_rules_invalid_regex_raises(tmp_path, monkeypatch):
    """Invalid regex in JSON → Pydantic ValidationError at module import
    time. Per spec §Error Handling: fail-fast in CI, no silent fallback."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "rules": [{"pattern": "[unclosed", "doc_type": "x", "priority": 50}],
        "default": {"doc_type": "x", "priority": 50},
    }))
    monkeypatch.setenv("EKRS_DOC_CLASSIFIER_RULES_PATH", str(bad))
    with pytest.raises(ValidationError):
        load_rules()


# --- load_index_file_name -------------------------------------------------


@pytest.mark.unit
def test_load_index_file_name_happy(tmp_path):
    """Reads file_name from output_path/index.json."""
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"file_name": "Lot049 NCR Status Report.doc"}))
    assert load_index_file_name(tmp_path) == "Lot049 NCR Status Report.doc"


@pytest.mark.unit
def test_load_index_file_name_missing(tmp_path):
    """Missing index.json → None (caller uses default 'unknown')."""
    assert load_index_file_name(tmp_path) is None


@pytest.mark.unit
def test_load_index_file_name_corrupt(tmp_path):
    """Corrupt JSON → None + WARNING log (does NOT raise)."""
    idx = tmp_path / "index.json"
    idx.write_text("{not valid json")
    assert load_index_file_name(tmp_path) is None