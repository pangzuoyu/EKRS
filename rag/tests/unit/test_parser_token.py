"""Unit tests for rag.ekrs_rag.security.parser_token helpers."""
import pytest

from ekrs_rag.security.parser_token import (
    CallbackAuthMissingError,
    build_callback_headers,
    safe_compare,
)


def test_safe_compare_equal_returns_true():
    assert safe_compare("a" * 32, "a" * 32) is True


def test_safe_compare_different_length_returns_false():
    assert safe_compare("a" * 31, "a" * 32) is False


def test_safe_compare_equal_length_different_value_returns_false():
    assert safe_compare("a" * 32, "b" * 32) is False


def test_safe_compare_empty_inputs():
    assert safe_compare("", "") is True  # both empty is technically equal
    assert safe_compare("", "a") is False
    assert safe_compare("a", "") is False


def test_safe_compare_non_string_returns_false():
    assert safe_compare(123, "abc") is False  # type: ignore[arg-type]
    assert safe_compare("abc", None) is False  # type: ignore[arg-type]


def test_build_callback_headers_returns_token(monkeypatch):
    monkeypatch.setenv("PARSER_TOKEN", "x" * 32)
    headers = build_callback_headers()
    assert headers["X-Parser-Token"] == "x" * 32
    assert "X-EKRS-Version" in headers


def test_build_callback_headers_raises_on_empty(monkeypatch):
    monkeypatch.setenv("PARSER_TOKEN", "")
    with pytest.raises(CallbackAuthMissingError):
        build_callback_headers()


def test_build_callback_headers_raises_on_short(monkeypatch):
    monkeypatch.setenv("PARSER_TOKEN", "short")
    with pytest.raises(CallbackAuthMissingError):
        build_callback_headers()