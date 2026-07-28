"""FTS5 BM25 keyword retrieval — Phase 10 T10a-1.

Mirrors Qdrant chunk payload to a local SQLite FTS5 virtual table, providing
a parallel keyword-recall path that the retriever will fuse with the vector
search via RRF (T10a-4). T10a-1 boundary: schema + CRUD + BM25 normalization +
unit tests. Pipeline wiring, RRF fusion, and retriever integration are
T10a-2 / T10a-3 / T10a-4 respectively.

Iron Rules compliance:
- R1: FTS does not participate in hint extraction (chunk model untouched).
- R2: Solver interface unchanged (FTS only feeds retrieval).
- R5: SQLite FTS5 virtual table is not a graph DB.
- R6: BM25 is deterministic, not inference.
- R7: scope_path indexed column, scope_filter restricts search.
- R8: status='illegal' filtering on retrieval only — never trim authority.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from ekrs_shared.models import Chunk


class FTSManager:
    """SQLite FTS5 全文索引管理器 — Phase 10 T10a-1.

    镜像 Qdrant chunk payload 到 FTS5 虚拟表. 与 Qdrant 写入并行, 但不
    替代 (R1: FTS 不参与 hint 提取, R5: 不参与 KG).
    """

    SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
        chunk_id UNINDEXED,
        block_id UNINDEXED,
        text,
        scope_path,
        status UNINDEXED,
        doc_hash UNINDEXED,
        payload_json UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'
    );
    """

    def __init__(self, db_path: Path) -> None:
        # check_same_thread=False: shared across FastAPI worker threads.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(self.SCHEMA)
        self._conn.commit()

    def upsert(
        self,
        chunk_id: str,
        block_id: str,
        chunk: Chunk,
        payload: dict,
        *,
        status: str = "active",
    ) -> None:
        """Insert or replace an FTS row. status='active' default (R8)."""
        scope_str = " ".join(chunk.scope_path) if chunk.scope_path else ""
        import json

        self._conn.execute(
            "INSERT INTO blocks_fts "
            "(chunk_id, block_id, text, scope_path, status, doc_hash, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                block_id,
                chunk.text,
                scope_str,
                status,
                chunk.doc_hash,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def search(
        self,
        query: str,
        *,
        limit: int = 40,
        scope_filter: Optional[list[str]] = None,
    ) -> list[tuple[str, float]]:
        """BM25 keyword search. Returns [(chunk_id, normalized_score), ...].

        score normalization: |bm25| / (1 + |bm25|), floor 0.01.
        R8: status='illegal' rows excluded.
        R7: scope_filter restricts to OR'd scope_path terms.
        """
        match_expr = self._build_match_expr(query, scope_filter)
        if match_expr is None:
            return []

        sql = (
            "SELECT chunk_id, "
            "abs(bm25(blocks_fts)) / (1 + abs(bm25(blocks_fts))) AS score "
            "FROM blocks_fts "
            "WHERE blocks_fts MATCH ? AND status != 'illegal' "
            "ORDER BY bm25(blocks_fts) ASC LIMIT ?"
        )
        rows = self._conn.execute(sql, (match_expr, limit)).fetchall()
        return [(row[0], max(row[1], 0.01)) for row in rows]

    @staticmethod
    def _build_fts5_query(query: str) -> Optional[str]:
        """Build FTS5 MATCH expression (QMD buildFTS5Query port).

        positive terms: '"term"*' (quoted + prefix wildcard) AND-joined.
        negative terms: '"term"' (no prefix) appended with NOT.
        Returns None if no positive terms.
        """
        positive: list[str] = []
        negative: list[str] = []
        for term in query.strip().split():
            if term.startswith("-"):
                sanitized = term[1:].lower()
                if sanitized:
                    negative.append(f'"{sanitized}"')
            else:
                sanitized = term.lower()
                if sanitized:
                    positive.append(f'"{sanitized}"*')
        if not positive:
            return None
        result = " AND ".join(positive)
        for neg in negative:
            result += f" NOT {neg}"
        return result

    @staticmethod
    def _build_match_expr(query: str, scope_filter: Optional[list[str]]) -> Optional[str]:
        """Compose full FTS5 MATCH expression with optional scope restriction.

        Column-restricted MATCH for scope_path uses FTS5 `column : "term"`
        syntax. Multi-term scope_filter ORs each restriction.
        """
        fts_query = FTSManager._build_fts5_query(query)
        if fts_query is None:
            return None
        if scope_filter:
            scope_expr = " OR ".join(f'scope_path : "{s}"' for s in scope_filter)
            if len(scope_filter) > 1:
                scope_expr = f"({scope_expr})"
            return f"{fts_query} AND {scope_expr}"
        return fts_query

    def delete_by_doc(self, doc_hash: str) -> int:
        """Delete all FTS rows for a doc_hash (paired with Qdrant delete)."""
        cur = self._conn.execute(
            "DELETE FROM blocks_fts WHERE doc_hash = ?", (doc_hash,)
        )
        self._conn.commit()
        return cur.rowcount

    def delete_by_chunk_id(self, chunk_id: str) -> int:
        """Delete a single FTS row by chunk_id (single-chunk rollback primitive)."""
        cur = self._conn.execute(
            "DELETE FROM blocks_fts WHERE chunk_id = ?", (chunk_id,)
        )
        self._conn.commit()
        return cur.rowcount

    def get_chunk_id(self, block_id: str) -> Optional[str]:
        """Bidirectional lookup: block_id → chunk_id (T10a-5 invariant)."""
        row = self._conn.execute(
            "SELECT chunk_id FROM blocks_fts WHERE block_id = ? LIMIT 1",
            (block_id,),
        ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    @staticmethod
    def generate_chunk_id(doc_hash: str, chunk_index: int) -> str:
        """Generate chunk_id = `{doc_hash[:8]}-{chunk_index:04d}`.

        T10a-1 owns the generator; T10a-5 owns the *when* (retriever-side timing).
        """
        return f"{doc_hash[:8]}-{chunk_index:04d}"