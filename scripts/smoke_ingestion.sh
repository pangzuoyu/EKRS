#!/usr/bin/env bash
# smoke_ingestion.sh — Phase 8 T8-3b
#
# End-to-end happy-path smoke for the RAG ingestion pipeline.
# Generates a 6-block JSONL, POSTs to /v1/ingestion/notify, polls
# /v1/ingestion/status until terminal, checks audit.log for
# qdrant_write_failed events attributed to the same trace_id, and
# verifies the parser-side callback was POSTed by RAG with
# status == "completed".
#
# Usage:
#   bash scripts/smoke_ingestion.sh [RAG_URL]
#   RAG_URL=http://localhost:8000 bash scripts/smoke_ingestion.sh
#
# Default RAG_URL: http://localhost:8000  (matches make dev).
#
# Exit codes:
#   0 — full happy path; all 4 contract conditions met
#   1 — general failure (cURL error, malformed JSON, missing token, etc.)
#   2 — /v1/ingestion/notify returned non-2xx after retries
#   3 — /v1/ingestion/status polling never reached terminal
#   4 — audit.log contained qdrant_write_failed for this trace_id
#   5 — callback server did not receive status=completed within timeout
#
# Each step emits [STEP N] <message> on its own line so triage doesn't
# require reading the full output. Token is read from $PARSER_TOKEN
# via safe-piping pattern (no echo to stdout; passed to curl via -H).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="${REPO_ROOT}/scripts"

RAG_URL="${1:-${RAG_URL:-http://localhost:8000}}"
TOKEN="${PARSER_TOKEN:?PARSER_TOKEN env var required (must be ≥32 chars)}"
DOC_HASH="smoke_$(date +%s)_$$"
VERSION=1
TIMESTAMP=$(date -Iseconds)
OUT_DIR="/tmp/ekrs_smoke/${DOC_HASH}/${TIMESTAMP}"
CALLBACK_PORT="${CALLBACK_PORT:-18765}"
CALLBACK_HOST="127.0.0.1"
CALLBACK_URL="http://${CALLBACK_HOST}:${CALLBACK_PORT}/cb"
CALLBACK_LOG="${OUT_DIR}/callback.jsonl"

# ---- helpers ---------------------------------------------------------

step() { printf '[STEP %d] %s\n' "$1" "$2" >&2; }
die()  { printf '[STEP %d][FAIL] %s\n' "$1" "$2" >&2; exit "$3"; }

# ---- preflight: token length (matches core/config.py validator) -----

if [ "${#TOKEN}" -lt 32 ]; then
    die 1 "PARSER_TOKEN must be ≥32 chars (got ${#TOKEN})" 1
fi

# ---- preflight: RAG reachable -----------------------------------------

step 1 "RAG reachable at ${RAG_URL}"
if ! curl -s --max-time 5 "${RAG_URL}/health" >/dev/null; then
    die 1 "RAG /health unreachable at ${RAG_URL} (is docker compose up?)" 1
fi

# ---- start mock callback server --------------------------------------

step 2 "Starting mock callback server on ${CALLBACK_URL}"
mkdir -p "${OUT_DIR}"
: > "${CALLBACK_LOG}"

# python -m http.server lacks POST capture; use a tiny inline server.
# Background process writes each incoming POST body to ${CALLBACK_LOG}
# (one JSON object per line). Stopped in the cleanup trap.
python3 - "${CALLBACK_HOST}" "${CALLBACK_PORT}" "${CALLBACK_LOG}" <<'PYEOF' &
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

host, port, log_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]

class H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode("utf-8", errors="replace")
        try:
            entry = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            entry = {"_raw": body}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
    def log_message(self, *_args):  # silence stderr
        return

HTTPServer((host, port), H).serve_forever()
PYEOF

CALLBACK_PID=$!
trap 'kill "${CALLBACK_PID}" 2>/dev/null || true' EXIT

# Wait briefly for the server to bind.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s --max-time 1 -o /dev/null -X POST \
        -H "Content-Type: application/json" \
        -d '{}' "${CALLBACK_URL}"; then
        break
    fi
    sleep 0.1
done

# ---- generate JSONL --------------------------------------------------

step 3 "Generating mock JSONL at ${OUT_DIR}/data.jsonl"
cat > "${OUT_DIR}/data.jsonl" <<'JSONL_END'
{"doc_id":"smoke_doc","block_id":"b001","type":"header","content":{"raw":"测试章节","md_preview":"# 测试章节"},"metadata":{"page_number":1,"heading_path":["测试章节"]},"lineage":{"parser_version":"smoke","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"smoke_doc","block_id":"b002","type":"text","content":{"raw":"施工温度不得超过60°C,养护时间不少于7天。","md_preview":"施工温度不得超过60°C,养护时间不少于7天。"},"metadata":{"page_number":1,"heading_path":["测试章节","1.1 一般规定"]},"lineage":{"parser_version":"smoke","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"smoke_doc","block_id":"b003","type":"table","content":{"raw":"","md_preview":"| 参数 | 标准值 | 单位 |\n|------|--------|------|\n| 抗压强度 | 30 | MPa |","structured":[["参数","标准值","单位"],["抗压强度","30","MPa"]]},"metadata":{"page_number":2,"heading_path":["测试章节","1.2 材料要求"]},"lineage":{"parser_version":"smoke","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"smoke_doc","block_id":"b004","type":"kv","content":{"raw":"最大水灰比: 0.55","md_preview":"最大水灰比: 0.55","structured":{"最大水灰比":"0.55"}},"metadata":{"page_number":2,"heading_path":["测试章节","1.2 材料要求"]},"lineage":{"parser_version":"smoke","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"smoke_doc","block_id":"b005","type":"text","content":{"raw":"高温环境下入模温度不宜超过35°C。","md_preview":"高温环境下入模温度不宜超过35°C。"},"metadata":{"page_number":3,"heading_path":["测试章节","1.3 高温施工"]},"lineage":{"parser_version":"smoke","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"smoke_doc","block_id":"b006","type":"text","content":{"raw":"本章描述施工一般要求和注意事项。","md_preview":"本章描述施工一般要求和注意事项。"},"metadata":{"page_number":3,"heading_path":["测试章节","1.3 高温施工"]},"lineage":{"parser_version":"smoke","strategy":"test","steps":[]},"uncertainty_score":0.0}
JSONL_END
touch "${OUT_DIR}/.ready"

# ---- POST /v1/ingestion/notify --------------------------------------

step 4 "Building notification payload via lib_smoke"
PAYLOAD_FILE="${OUT_DIR}/notify.json"
python3 "${SCRIPTS_DIR}/lib_smoke.py" build-payload \
    --doc-hash "${DOC_HASH}" \
    --output-path "${OUT_DIR}" \
    --callback-url "${CALLBACK_URL}" \
    --version "${VERSION}" \
    > "${PAYLOAD_FILE}"
TRACE_ID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["trace_id"])' "${PAYLOAD_FILE}")
step 4 "trace_id=${TRACE_ID}"

step 5 "POST /v1/ingestion/notify (retry up to 3x on transport errors)"
NOTIFY_BODY=""
for attempt in 1 2 3; do
    if HTTP_CODE=$(curl -s -o "${OUT_DIR}/notify_response.json" -w "%{http_code}" \
        --max-time 10 \
        -X POST "${RAG_URL}/v1/ingestion/notify" \
        -H "Content-Type: application/json" \
        -H "X-Parser-Token: ${TOKEN}" \
        --data-binary "@${PAYLOAD_FILE}"); then
        if [[ "${HTTP_CODE}" =~ ^2 ]]; then
            break
        fi
    fi
    step 5 "attempt ${attempt}: HTTP ${HTTP_CODE}, retrying in 1s"
    sleep 1
done
if [[ "${HTTP_CODE:-0}" =~ ^2 ]]; then
    step 5 "notify accepted (HTTP ${HTTP_CODE})"
else
    die 5 "notify returned HTTP ${HTTP_CODE} after 3 retries" 2
fi

# ---- poll /v1/ingestion/status/<doc_hash> ----------------------------

step 6 "Polling /v1/ingestion/status/${DOC_HASH} (timeout 30s)"
STATUS=""
STATUS_DEADLINE=$(( $(date +%s) + 30 ))
while [ "$(date +%s)" -lt "${STATUS_DEADLINE}" ]; do
    if STATUS_BODY=$(curl -s --max-time 5 \
        "${RAG_URL}/v1/ingestion/status/${DOC_HASH}" 2>/dev/null); then
        STATUS=$(echo "${STATUS_BODY}" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("status", ""))
except Exception:
    print("")
' 2>/dev/null)
        case "${STATUS}" in
            completed|failed)
                step 6 "status reached terminal: ${STATUS}"
                break
                ;;
            *)
                step 6 "status=${STATUS:-<unknown>}, retrying"
                ;;
        esac
    fi
    sleep 0.5
done
if [ "${STATUS}" != "completed" ] && [ "${STATUS}" != "failed" ]; then
    die 6 "status polling never reached terminal within 30s" 3
fi
if [ "${STATUS}" = "failed" ]; then
    die 6 "ingestion terminal status=failed (HTTP body in ${OUT_DIR}/status_final.json)" 3
fi

# ---- audit.log scan for qdrant_write_failed --------------------------

step 7 "Scanning audit.log for qdrant_write_failed (trace_id=${TRACE_ID})"
# In the docker compose stack, audit.log is mounted into the rag
# container at /var/log/ekrs/audit.log; from the host it is usually
# at <repo>/rag/audit.log or a docker volume. Try common locations.
AUDIT_CANDIDATES=(
    "${REPO_ROOT}/rag/audit.log"
    "/var/log/ekrs/audit.log"
)
AUDIT_PATH=""
for cand in "${AUDIT_CANDIDATES[@]}"; do
    if [ -r "${cand}" ]; then
        AUDIT_PATH="${cand}"
        break
    fi
done
if [ -z "${AUDIT_PATH}" ]; then
    step 7 "audit.log not reachable from this host (skipping audit check)"
else
    step 7 "audit.log path: ${AUDIT_PATH}"
    FAILURES=$(python3 "${SCRIPTS_DIR}/lib_smoke.py" check-audit \
        --audit-path "${AUDIT_PATH}" --trace-id "${TRACE_ID}")
    if [ -n "${FAILURES}" ]; then
        die 7 "audit.log contained qdrant_write_failed entries:
${FAILURES}" 4
    fi
    step 7 "audit clean for trace_id=${TRACE_ID}"
fi

# ---- verify mock callback received status=completed ------------------

step 8 "Waiting for callback server to receive status=completed"
CALLBACK_DEADLINE=$(( $(date +%s) + 10 ))
while [ "$(date +%s)" -lt "${CALLBACK_DEADLINE}" ]; do
    if [ -s "${CALLBACK_LOG}" ]; then
        CALLBACK_STATUS=$(python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    for line in f:
        try:
            print(json.loads(line).get("status", ""))
        except Exception:
            pass
' "${CALLBACK_LOG}" | head -1)
        if [ "${CALLBACK_STATUS}" = "completed" ]; then
            step 8 "callback received status=completed"
            step 9 "smoke PASS (doc_hash=${DOC_HASH}, trace_id=${TRACE_ID})"
            exit 0
        fi
    fi
    sleep 0.2
done
die 8 "callback server did not receive status=completed within 10s
log file: ${CALLBACK_LOG}" 5