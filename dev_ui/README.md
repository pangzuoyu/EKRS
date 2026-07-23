# dev_ui — Streamlit debug UI

Phase 7 T5 (Decision §2). Folded into `rag/` as a dev-only extra so
production Docker images stay slim.

## Quick start

```bash
# 1. Install the dev extra (adds streamlit + httpx if not already present)
pip install -e rag[dev]

# 2. Make sure RAG service is running (in another terminal)
make dev

# 3. Run the UI
streamlit run dev_ui/app.py
# → http://localhost:8501
```

The UI defaults to `http://localhost:8000` as the RAG base URL. Override
with the `EKRS_API_BASE` environment variable if your service is on a
different port.

## Tabs

| Tab | Purpose | Backed by |
|-----|---------|-----------|
| 文档入库 (Ingest) | Trigger mock parser notification, view task repo status | `POST /v1/ingestion/notify`, `GET /v1/ingestion/status/{doc_hash}` |
| 约束查询 (Constraints) | POST /v1/constraints against the live API, see multi-branch output | `POST /v1/constraints` |
| 黄金集验证 (Golden Set) | Run `tests/golden_set/golden_set.json` against live API, report pass/fail | `tests/golden_set/golden_set.json` |
| 覆盖关系 (Overlays) | Read-only view of `provision_overrides` rows in DocumentRepo | `DocumentRepo.get_provision_overrides` |

## Why dev-only?

- Reads from the same SQLite + Qdrant the API uses — bypassing it would
  duplicate auth/scoping logic.
- Writes via the existing API surface so audit log coverage stays uniform.
- Gated by being absent from production Docker images (rag[prod] does
  not install streamlit).

## Out of T5 scope

- Write UI for `provision_overrides` — operator changes should still go
  through `/v1/admin/...` endpoints to keep X-Admin-Key auth consistent.
- Login / auth — Streamlit runs on localhost, expected to be behind a
  reverse proxy if exposed externally.
- Test automation — Streamlit's `AppTest` framework is not wired in
  Phase 7; manual smoke testing is the bar.