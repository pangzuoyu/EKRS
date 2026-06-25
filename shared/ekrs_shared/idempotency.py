"""幂等键生成工具."""
from __future__ import annotations

import hashlib


def request_id_from_trace(trace_id: str, doc_hash: str, version: int) -> str:
    """生成稳定的幂等键: md5(trace_id|doc_hash|version) hex."""
    raw = f"{trace_id}|{doc_hash}|{version}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()
