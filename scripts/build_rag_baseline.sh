#!/usr/bin/env bash
#
# build_rag_baseline.sh — Phase 8 T8-3a
#
# Rebuilds the RAG Docker image and pins it as the locked-down
# baseline. Writes deployment/rag-image.baseline.json with:
#   - image_sha256 (the canonical reference for T8-3b smoke tests)
#   - content-addressable short tag (model SHA prefix + Dockerfile
#     SHA prefix) so any drift in either input produces a new tag
#     and the smoke script catches it.
#
# Use when:
#   - rag/models/bge-m3/** changes (new model export)
#   - rag/Dockerfile changes (new COPY / ENV / ARG)
#   - deployment/docker-compose.yml changes (build args change)
#   - EkRS_RELEASE_CUT (Promote a new baseline to a stable tag)
#
# Outputs (idempotent):
#   1. Local image tag:  ekrs-rag:t8-3a-<model_prefix>-<dockerfile_prefix>
#   2. JSON manifest:    deployment/rag-image.baseline.json
#   3. Stdout line:      baseline:<short_tag> sha256:<image_sha>
#
# Exit codes:
#   0 — image built, manifest written, content SHA verified
#   1 — Docker build failed
#   2 — Image SHA manifest mismatch (model file drift between
#       repo and in-image)
#   3 — jq / sha256sum / docker not on PATH
#
# Restricted-network override (China dev machines):
#   PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
#   PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
#   ./scripts/build_rag_baseline.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/rag/Dockerfile"
MODEL_DIR="${REPO_ROOT}/rag/models/bge-m3"
MANIFEST="${REPO_ROOT}/deployment/rag-image.baseline.json"

for cmd in docker sha256sum awk grep date; do
    command -v "$cmd" >/dev/null || { echo "FATAL: $cmd not on PATH" >&2; exit 3; }
done
[ -f "$DOCKERFILE" ] || { echo "FATAL: Dockerfile missing at $DOCKERFILE" >&2; exit 3; }
[ -f "$MODEL_DIR/bge-m3.sha256" ] || { echo "FATAL: bge-m3.sha256 missing" >&2; exit 3; }

PYTHON_BASE_IMAGE="${PYTHON_BASE_IMAGE:-python:3.11-slim}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"

MODEL_SHA_PREFIX=$(awk '$2=="model.onnx" {print substr($1,1,12)}' "$MODEL_DIR/bge-m3.sha256")
DOCKERFILE_SHA_PREFIX=$(sha256sum "$DOCKERFILE" | awk '{print substr($1,1,12)}')
SHORT_TAG="t8-3a-${MODEL_SHA_PREFIX}-${DOCKERFILE_SHA_PREFIX}"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "=== Phase 8 T8-3a baseline build ==="
echo "  PYTHON_BASE_IMAGE = ${PYTHON_BASE_IMAGE}"
echo "  PIP_INDEX_URL     = ${PIP_INDEX_URL}"
echo "  Short tag         = ${SHORT_TAG}"
echo "  Build context     = ${REPO_ROOT}"

docker build \
    --build-arg "PYTHON_BASE_IMAGE=${PYTHON_BASE_IMAGE}" \
    --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}" \
    -t "ekrs-rag:${SHORT_TAG}" \
    -f "$DOCKERFILE" \
    "$REPO_ROOT" || { echo "FATAL: docker build failed" >&2; exit 1; }

# Tag the stable "t8-3a-baseline" alias too so smoke scripts can use
# a stable name even after a fresh build.
docker tag "ekrs-rag:${SHORT_TAG}" "ekrs-rag:t8-3a-baseline"

IMAGE_SHA=$(docker inspect "ekrs-rag:${SHORT_TAG}" --format '{{.Id}}' | sed 's/sha256://')
IMAGE_SIZE=$(docker inspect "ekrs-rag:${SHORT_TAG}" --format '{{.Size}}')

# Verify the SHA manifest inside the image matches the repo (catches
# truncated COPY or .dockerignore regression).
IN_IMAGE_SHA=$(docker run --rm "ekrs-rag:${SHORT_TAG}" cat /opt/ekrs/models/bge-m3/bge-m3.sha256)
REPO_SHA=$(cat "$MODEL_DIR/bge-m3.sha256")
if [ "$IN_IMAGE_SHA" != "$REPO_SHA" ]; then
    echo "FATAL: bge-m3.sha256 in image differs from repo" >&2
    diff <(echo "$IN_IMAGE_SHA") <(echo "$REPO_SHA") >&2 || true
    exit 2
fi

cat > "$MANIFEST" <<EOF
{
  "_comment": "Phase 8 T8-3a baseline: locked-down reference image. Regenerate with scripts/build_rag_baseline.sh after ANY change to rag/models/bge-m3/, rag/Dockerfile, or deployment/docker-compose.yml. T8-3b smoke script reads this manifest to confirm the running image matches.",
  "tag": "${SHORT_TAG}",
  "image_sha256": "${IMAGE_SHA}",
  "image_size_bytes": ${IMAGE_SIZE},
  "bge_m3_model_sha256_prefix": "${MODEL_SHA_PREFIX}",
  "dockerfile_sha256_prefix": "${DOCKERFILE_SHA_PREFIX}",
  "build_args": {
    "PYTHON_BASE_IMAGE": "${PYTHON_BASE_IMAGE}",
    "PIP_INDEX_URL": "${PIP_INDEX_URL}"
  },
  "captured_at_utc": "${TS}",
  "verify_command": "docker run --rm ekrs-rag:${SHORT_TAG} cat /opt/ekrs/models/bge-m3/bge-m3.sha256"
}
EOF

echo
echo "baseline:${SHORT_TAG} sha256:${IMAGE_SHA}"
echo "manifest:${MANIFEST}"
echo "OK"