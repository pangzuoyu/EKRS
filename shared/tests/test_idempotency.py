"""Tests for idempotency key generator."""
from ekrs_shared.idempotency import request_id_from_trace


def test_same_inputs_same_id():
    a = request_id_from_trace("t1", "doc_abc", 3)
    b = request_id_from_trace("t1", "doc_abc", 3)
    assert a == b
    assert len(a) == 32  # hex md5


def test_different_doc_different_id():
    a = request_id_from_trace("t1", "doc_abc", 3)
    b = request_id_from_trace("t1", "doc_xyz", 3)
    assert a != b


def test_different_version_different_id():
    a = request_id_from_trace("t1", "doc_abc", 3)
    b = request_id_from_trace("t1", "doc_abc", 4)
    assert a != b
