#!/usr/bin/env bash
# mock_parser_notify.sh — Generate sample DocumentBlock IR JSONL and POST notification
#
# Usage: bash scripts/mock_parser_notify.sh [RAG_URL]
#
# Generates 6 sample blocks (header, text, table, kv, text with constraint,
# text with no numbers) and sends them as a complete ingestion notification.

set -euo pipefail

RAG_URL="${1:-http://localhost:8000}"
TOKEN="${PARSER_TOKEN:-change-me-to-a-secure-random-string-32chars}"
DOC_HASH="mock_test_$(date +%s)"
VERSION=1
TIMESTAMP=$(date -Iseconds)
OUTPUT_DIR="/tmp/ekrs_mock/${DOC_HASH}/${TIMESTAMP}"
CALLBACK_URL="${RAG_URL}/v1/callback"

mkdir -p "${OUTPUT_DIR}"

# Generate DocumentBlock IR JSONL
cat > "${OUTPUT_DIR}/data.jsonl" << 'JSONL_END'
{"doc_id":"mock_doc_001","block_id":"b001","type":"header","content":{"raw":"第3章 混凝土工程","md_preview":"# 第3章 混凝土工程"},"metadata":{"page_number":1,"heading_path":["第3章 混凝土工程"]},"lineage":{"parser_version":"mock","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"mock_doc_001","block_id":"b002","type":"text","content":{"raw":"混凝土养护温度不得超过80°C，养护时间不少于7天。浇筑完成后应在12小时内开始养护。","md_preview":"混凝土养护温度不得超过80°C，养护时间不少于7天。浇筑完成后应在12小时内开始养护。"},"metadata":{"page_number":1,"heading_path":["第3章 混凝土工程","3.1 一般规定"]},"lineage":{"parser_version":"mock","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"mock_doc_001","block_id":"b003","type":"table","content":{"raw":"","md_preview":"| 参数 | 标准值 | 单位 |\n|------|--------|------|\n| 抗压强度 | 30 | MPa |\n| 抗拉强度 | 3 | MPa |\n| 弹性模量 | 30000 | MPa |","structured":[["参数","标准值","单位"],["抗压强度","30","MPa"],["抗拉强度","3","MPa"],["弹性模量","30000","MPa"]]},"metadata":{"page_number":2,"heading_path":["第3章 混凝土工程","3.2 材料要求"]},"lineage":{"parser_version":"mock","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"mock_doc_001","block_id":"b004","type":"kv","content":{"raw":"最大水灰比: 0.6","md_preview":"最大水灰比: 0.6","structured":{"最大水灰比":"0.6"}},"metadata":{"page_number":2,"heading_path":["第3章 混凝土工程","3.2 材料要求"]},"lineage":{"parser_version":"mock","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"mock_doc_001","block_id":"b005","type":"text","content":{"raw":"高温环境下，混凝土入模温度不宜超过35°C。环境温度高于40°C时应采取降温措施。钢筋间距不小于25mm。","md_preview":"高温环境下，混凝土入模温度不宜超过35°C。环境温度高于40°C时应采取降温措施。钢筋间距不小于25mm。"},"metadata":{"page_number":3,"heading_path":["第3章 混凝土工程","3.3 高温施工"]},"lineage":{"parser_version":"mock","strategy":"test","steps":[]},"uncertainty_score":0.0}
{"doc_id":"mock_doc_001","block_id":"b006","type":"text","content":{"raw":"本章节描述了混凝土施工的一般要求和注意事项。","md_preview":"本章节描述了混凝土施工的一般要求和注意事项。"},"metadata":{"page_number":3,"heading_path":["第3章 混凝土工程","3.3 高温施工"]},"lineage":{"parser_version":"mock","strategy":"test","steps":[]},"uncertainty_score":0.0}
JSONL_END

# Create .ready marker
touch "${OUTPUT_DIR}/.ready"

echo "Generated mock JSONL at: ${OUTPUT_DIR}/data.jsonl"
echo "Lines: $(wc -l < "${OUTPUT_DIR}/data.jsonl")"
echo ""

# Send notification
echo "Sending notification to ${RAG_URL}/v1/ingestion/notify ..."
curl -s -X POST "${RAG_URL}/v1/ingestion/notify" \
  -H "Content-Type: application/json" \
  -H "X-Parser-Token: ${TOKEN}" \
  -d "{
    \"trace_id\": \"mock-$(date +%s)\",
    \"doc_hash\": \"${DOC_HASH}\",
    \"version\": ${VERSION},
    \"output_path\": \"${OUTPUT_DIR}\",
    \"callback_url\": \"${CALLBACK_URL}\"
  }" | python3 -m json.tool 2>/dev/null || echo "(response not JSON)"

echo ""
echo "Check status:"
echo "  curl ${RAG_URL}/v1/ingestion/status/${DOC_HASH}"
