"""Phase 12 Task C: filename → doc_type classifier.

Pure module. Reads JSON rules (default = sibling
``doc_classifier_rules.json``; override via
``EKRS_DOC_CLASSIFIER_RULES_PATH``), applies first-match-wins regex to
the filename, returns ``ClassificationResult(doc_type, priority)``.

R4 mapping (per spec §R4 mapping):
  national_standard=100, industry_standard=80, enterprise_spec=60,
  lot_checklist=60, project_spec=40, unknown=40

Failure modes (per spec §Error Handling):
- Invalid regex in JSON config → Pydantic ValidationError at module
  import (fail-fast in CI).
- Missing/corrupt ``index.json`` → ``load_index_file_name`` returns
  ``None`` (caller maps to ``"unknown"`` doc_type; pipeline does NOT
  fail).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "doc_classifier_rules.json"


# --- Pydantic config models ----------------------------------------------


class DocClassifierRule(BaseModel):
    """Single regex rule: matches against filename; emits doc_type + priority."""

    model_config = ConfigDict(extra="ignore")

    pattern: str
    doc_type: str
    priority: int = Field(ge=0, le=100)

    @field_validator("pattern")
    @classmethod
    def _validate_regex(cls, v: str) -> str:
        """Compile-test at config-load time — fail-fast on bad regex."""
        try:
            re.compile(v, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex pattern {v!r}: {e}") from e
        return v


class DocClassifierDefault(BaseModel):
    """Fallback fired when no rule matches — no pattern required."""

    model_config = ConfigDict(extra="ignore")

    doc_type: str
    priority: int = Field(ge=0, le=100)


class DocClassifierRules(BaseModel):
    """Bundle of rules + a default that fires when no rule matches."""

    model_config = ConfigDict(extra="ignore")

    rules: List[DocClassifierRule]
    default: DocClassifierDefault


# --- Public API ----------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Output of :func:`classify`. Immutable for safe sharing."""

    doc_type: str
    priority: int


def load_rules(path: Optional[Path] = None) -> DocClassifierRules:
    """Load ``DocClassifierRules`` from JSON config.

    Path resolution order:
    1. ``EKRS_DOC_CLASSIFIER_RULES_PATH`` env var (if set)
    2. ``path`` argument (if provided)
    3. ``DEFAULT_RULES_PATH`` (sibling JSON file)

    Args:
        path: Optional explicit path to a JSON config file. Overridden by
            ``EKRS_DOC_CLASSIFIER_RULES_PATH`` env var when set.

    Returns:
        Validated ``DocClassifierRules`` instance.

    Raises:
        FileNotFoundError: Resolved path does not exist.
        json.JSONDecodeError: File is not valid JSON.
        pydantic.ValidationError: Schema mismatch or invalid regex pattern.
    """
    env_path = os.getenv("EKRS_DOC_CLASSIFIER_RULES_PATH")
    if env_path:
        chosen = Path(env_path)
    elif path is not None:
        chosen = path
    else:
        chosen = DEFAULT_RULES_PATH
    raw = json.loads(chosen.read_text())
    return DocClassifierRules(**raw)


def classify(filename: str, rules: DocClassifierRules) -> ClassificationResult:
    """First-match-wins regex classification.

    Empty filename → default. Case-insensitive (re.IGNORECASE baked into
    the compiled pattern at config-load time).

    Args:
        filename: Document filename (e.g. ``"GB150-2011.pdf"``).
        rules: Loaded classifier config.

    Returns:
        ``ClassificationResult`` for the first matching rule, or the
        configured default when nothing matches (or filename is empty).
    """
    if not filename:
        return ClassificationResult(
            doc_type=rules.default.doc_type, priority=rules.default.priority,
        )
    for rule in rules.rules:
        if re.search(rule.pattern, filename, re.IGNORECASE):
            return ClassificationResult(doc_type=rule.doc_type, priority=rule.priority)
    return ClassificationResult(
        doc_type=rules.default.doc_type, priority=rules.default.priority,
    )


def load_index_file_name(output_path: Path) -> Optional[str]:
    """Read ``output_path/index.json`` and return its ``file_name`` field.

    Returns None (with WARNING log) on:
    - File missing
    - File corrupt (json.JSONDecodeError)
    - ``file_name`` field missing

    Args:
        output_path: Directory expected to contain ``index.json``.

    Returns:
        ``file_name`` string from the index, or ``None`` when missing/
        corrupt/empty.
    """
    idx = output_path / "index.json"
    if not idx.exists():
        logger.warning("index.json missing at %s — defaulting doc_type to 'unknown'", idx)
        return None
    try:
        data = json.loads(idx.read_text())
    except json.JSONDecodeError as e:
        logger.warning("index.json corrupt at %s: %s — defaulting to 'unknown'", idx, e)
        return None
    fn = data.get("file_name")
    if not fn:
        logger.warning("index.json missing file_name at %s — defaulting to 'unknown'", idx)
        return None
    return str(fn)