"""EKRS RAG — Streamlit debug UI (Phase 7 T5).

Decision §2: folded into rag/[dev] extra (not dev_ui/pyproject.toml) so
production Docker images stay slim. Run with:

    pip install -e rag[dev]
    streamlit run dev_ui/app.py

The UI reads/writes the same RAG API the production service exposes
(``/v1/ingestion/*``, ``/v1/constraints``) — no separate DB or auth path.
For ``/v1/admin/*`` endpoints an ``X-Admin-Key`` header is forwarded when
the operator supplies it via the sidebar.

For provisioning-style mutations (overrides, document metadata), this UI
is a read-only viewer; writes must still flow through ``/v1/admin/*`` to
preserve the audit trail and ``X-Admin-Key`` auth invariant.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE_DEFAULT = "http://localhost:8000"
GOLDEN_SET_PATH = (
    Path(__file__).resolve().parent.parent
    / "rag"
    / "tests"
    / "golden_set"
    / "golden_set.json"
)


def _api_base() -> str:
    """Resolve API base URL from env or sidebar."""
    return os.environ.get("EKRS_API_BASE", API_BASE_DEFAULT)


def _admin_key() -> str | None:
    """Read admin key from session (set by sidebar) or env."""
    return st.session_state.get("admin_key") or os.environ.get("EKRS_ADMIN_KEY")


def _admin_headers() -> dict[str, str]:
    key = _admin_key()
    return {"X-Admin-Key": key} if key else {}


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="EKRS RAG — Dev UI",
    page_icon="🛠️",
    layout="wide",
)

with st.sidebar:
    st.header("Service")
    api_base = st.text_input(
        "API base URL",
        value=_api_base(),
        help="Override with EKRS_API_BASE env var if needed.",
    )
    admin_key_input = st.text_input(
        "X-Admin-Key (optional)",
        type="password",
        help="Forwarded to /v1/admin/* endpoints. Leave empty for read-only.",
    )
    if admin_key_input:
        st.session_state["admin_key"] = admin_key_input

    health = None
    try:
        r = httpx.get(f"{api_base}/healthz", timeout=2.0)
        health = r.status_code
    except Exception as e:
        health = f"unreachable ({e.__class__.__name__})"
    st.metric("Service health", health if health == 200 else "down" if health else "—")


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_ingest, tab_query, tab_golden, tab_overlays = st.tabs(
    ["📥 文档入库", "🔍 约束查询", "📊 黄金集验证", "🧩 覆盖关系"]
)

# --- Tab 1: Ingest ----------------------------------------------------------

with tab_ingest:
    st.subheader("Trigger parser notification")
    st.caption(
        "POST /v1/ingestion/notify — the production callback the parser "
        "would send. Requires a JSONL file already present under "
        "SHARED_STORAGE_PATH."
    )

    col1, col2 = st.columns(2)
    with col1:
        doc_hash = st.text_input("doc_hash", value="demo_doc_001")
        output_path = st.text_input(
            "output_path",
            value="/shared/demo/output",
            help="Must be inside SHARED_STORAGE_PATH.",
        )
    with col2:
        version = st.number_input("version", min_value=1, value=1, step=1)
        callback_url = st.text_input(
            "callback_url (optional)",
            value="",
            help="Parser callback to POST the ingest result to.",
        )

    if st.button("Submit notification", type="primary"):
        payload: dict[str, Any] = {
            "doc_hash": doc_hash,
            "version": int(version),
            "output_path": output_path,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        try:
            r = httpx.post(
                f"{api_base}/v1/ingestion/notify",
                json=payload,
                timeout=10.0,
            )
            st.write(f"**Status**: {r.status_code}")
            st.json(r.json())
        except Exception as e:
            st.error(f"Request failed: {e}")

    st.divider()
    st.subheader("Check task status")
    check_hash = st.text_input("doc_hash to check", value=doc_hash, key="check_hash")
    if st.button("Query /v1/ingestion/status"):
        try:
            r = httpx.get(
                f"{api_base}/v1/ingestion/status/{check_hash}",
                timeout=5.0,
            )
            st.write(f"**Status**: {r.status_code}")
            st.json(r.json())
        except Exception as e:
            st.error(f"Request failed: {e}")


# --- Tab 2: Constraint query ------------------------------------------------

with tab_query:
    st.subheader("POST /v1/constraints")
    st.caption("Three-gate pipeline: recall → extract → solve.")

    query_text = st.text_area(
        "Query", value="高温环境下温度限制", height=80,
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        strict = st.checkbox("strict (R6)", value=False)
    with col2:
        top_k = st.number_input("top_k", min_value=1, max_value=200, value=40)
    with col3:
        trace_id = st.text_input("trace_id (optional)", value="")

    if st.button("Run query", type="primary"):
        payload: dict[str, Any] = {
            "query": query_text,
            "context": {},
            "strict": strict,
            "top_k": int(top_k),
        }
        if trace_id:
            payload["trace_id"] = trace_id
        try:
            r = httpx.post(
                f"{api_base}/v1/constraints",
                json=payload,
                timeout=30.0,
            )
            st.write(f"**Status**: {r.status_code}")
            if r.status_code == 200:
                resp = r.json()
                st.write(f"**Mode**: `{resp.get('mode')}`")
                st.write(f"**Primary branch**: `{resp.get('primary_branch')}`")
                if resp.get("conflicts"):
                    st.warning(f"Conflicts detected: {len(resp['conflicts'])}")
                    st.json(resp["conflicts"])
                st.write("**Branches:**")
                st.json(resp.get("branches", {}))
                if resp.get("trace"):
                    with st.expander("Trace (debug)"):
                        st.json(resp["trace"])
            else:
                st.json(r.json())
        except Exception as e:
            st.error(f"Request failed: {e}")


# --- Tab 3: Golden set validation -------------------------------------------

with tab_golden:
    st.subheader("Golden set regression")
    st.caption(
        f"Runs `tests/golden_set/golden_set.json` ({42} cases) against the "
        "live API and reports per-case pass/fail against the embedded "
        "`expected` and `gates` blocks."
    )

    if not GOLDEN_SET_PATH.exists():
        st.error(f"Golden set not found at {GOLDEN_SET_PATH}")
    else:
        golden = json.loads(GOLDEN_SET_PATH.read_text(encoding="utf-8"))
        st.write(f"Loaded **{len(golden)}** cases.")

        if st.button("Run golden set", type="primary"):
            results: list[dict[str, Any]] = []
            progress = st.progress(0.0, text="Starting…")
            for i, case in enumerate(golden):
                progress.progress(
                    (i + 1) / len(golden),
                    text=f"Running {case['name']} ({i+1}/{len(golden)})",
                )
                payload = {
                    "query": case["query"],
                    "context": {},
                    "strict": case.get("strict", False),
                    "top_k": 40,
                }
                try:
                    r = httpx.post(
                        f"{api_base}/v1/constraints",
                        json=payload,
                        timeout=30.0,
                    )
                    passed = r.status_code == 200
                    error: str | None = None
                    if not passed:
                        error = f"HTTP {r.status_code}: {r.text[:200]}"
                    results.append({
                        "name": case["name"],
                        "status": "PASS" if passed else "FAIL",
                        "http": r.status_code,
                        "error": error,
                    })
                except Exception as e:
                    results.append({
                        "name": case["name"],
                        "status": "ERROR",
                        "http": None,
                        "error": str(e),
                    })
            progress.empty()

            passed_count = sum(1 for r in results if r["status"] == "PASS")
            failed_count = len(results) - passed_count
            col1, col2 = st.columns(2)
            col1.metric("Passed", passed_count)
            col2.metric("Failed", failed_count)

            st.dataframe(results, use_container_width=True)


# --- Tab 4: Provision overrides (read-only viewer) --------------------------

with tab_overlays:
    st.subheader("Provision overrides (provision_overrides)")
    st.caption(
        "Read-only view of the DocumentRepo `provision_overrides` table. "
        "Writes must go through `/v1/admin/*` to preserve the audit trail."
    )

    if not _admin_key():
        st.info("Set X-Admin-Key in the sidebar to load overrides.")
    else:
        st.warning(
            "There is no /v1/admin/overrides endpoint yet — this tab is a "
            "placeholder. Use `sqlite3` or DocumentRepo directly until T7+ "
            "ships an admin endpoint."
        )


# ---------------------------------------------------------------------------
# Smoke entry (for `python dev_ui/app.py` accidental invocation)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Smoke guard (NOT used by `streamlit run`; harmless under AppTest)
# ---------------------------------------------------------------------------

# Streamlit's AppTest runner invokes this script via runpy, which sets
# __name__ to "__main__" in some Streamlit versions. This guard detects
# plain `python dev_ui/app.py` via sys.argv[0] and bails out with a hint.
# `streamlit run dev_ui/app.py` keeps argv[0] == "streamlit" so the guard
# is skipped. AppTest in newer Streamlit versions skips __main__ entirely.
if (
    __name__ == "__main__"
    and "streamlit" not in sys.argv[0].lower()
    and "app_test" not in sys.argv[0].lower()
):  # pragma: no cover
    print(
        "dev_ui is a Streamlit app. Run: streamlit run dev_ui/app.py",
        file=sys.stderr,
    )
    sys.exit(1)