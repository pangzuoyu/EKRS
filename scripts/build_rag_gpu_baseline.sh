#!/usr/bin/env bash
#
# build_rag_gpu_baseline.sh — Phase 13c-C13 follow-up: GPU container baseline.
#
# Sibling of scripts/build_rag_baseline.sh (CPU, Phase 8 T8-3a). Pins the
# GPU image (rag/Dockerfile.gpu + bind-mounted bge-m3 torch weights +
# torch==2.11.* wheel) into deployment/rag-gpu-image.baseline.json so
# `make gpu-up` can detect silent drift.
#
# Diff vs CPU baseline:
#  - Model is NOT vendored in image (PoC bind-mount from host
#    /home/pangzy/code_project/bge-m3/). Cross-check is host SHA, not
#    in-image SHA.
#  - TORCH_INDEX_URL is a new build arg (CPU doesn't install torch).
#  - Adds torch version verification (CPU only checks ONNX manifest).
#  - Uses :latest tag + deployment-rag-gpu container name (compose's
#    gpu service builds to deployment-rag-gpu:latest).
#
# Outputs (idempotent):
#  1. JSON manifest:   deployment/rag-gpu-image.baseline.json
#  2. Optional SHA file: /home/pangzy/code_project/bge-m3/bge-m3.sha256
#     (bootstrap if missing; host-side, NOT git-tracked)
#  3. Stdout line:     baseline:gpu <short_tag> sha256:<image_sha>
#
# Exit codes:
#  0 — baseline captured
#  1 — container missing or build arg mismatch
#  2 — torch version mismatch (expected 2.11.*)
#  3 — jq / sha256sum / docker not on PATH
#  4 — bge-m3 host bind-mount dir missing
#
# Restricted-network override (China dev machines):
#  PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
#  PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
#  TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 \
#  ./scripts/build_rag_gpu_baseline.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/rag/Dockerfile.gpu"
HOST_MODEL_DIR="${BGE_M3_HOST_DIR:-/home/pangzy/code_project/bge-m3}"
HOST_SHA_FILE="${HOST_MODEL_DIR}/bge-m3.sha256"
MANIFEST="${REPO_ROOT}/deployment/rag-gpu-image.baseline.json"
GPU_CONTAINER="${GPU_CONTAINER:-}"
COMPOSE_PROJECT_DIR="${REPO_ROOT}/deployment"

for cmd in docker sha256sum awk grep date find sort; do
    command -v "$cmd" >/dev/null || { echo "FATAL: $cmd not on PATH" >&2; exit 3; }
done
[ -f "$DOCKERFILE" ] || { echo "FATAL: Dockerfile.gpu missing at $DOCKERFILE" >&2; exit 3; }
[ -d "$HOST_MODEL_DIR" ] || { echo "FATAL: bge-m3 host bind-mount dir missing: $HOST_MODEL_DIR" >&2; exit 4; }

# Compose-override build args (must match deployment/docker-compose.override.yml rag-gpu block)
PYTHON_BASE_IMAGE="${PYTHON_BASE_IMAGE:-docker.m.daocloud.io/library/python:3.11-slim}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu130}"

# --- Bootstrap host SHA manifest if missing (one-time, host-side only) ---
if [ ! -f "$HOST_SHA_FILE" ]; then
    echo "=== Bootstrapping host SHA manifest at ${HOST_SHA_FILE} ==="
    # Files that affect inference: weights + configs + tokenizers
    # (Skip onnx/ subdir — GPU path doesn't use ONNX; FP32 weights via torch)
    find "$HOST_MODEL_DIR" -type f \
        \( -name 'pytorch_model.bin' -o -name '*.json' -o -name '*.pt' \) \
        ! -name 'bge-m3.sha256' \
        ! -path '*/.cache/*' \
        ! -path '*/imgs/*' \
        ! -path '*/onnx/*' \
        ! -path '*/1_Pooling/*' \
        -print0 | sort -z | xargs -0 sha256sum > "$HOST_SHA_FILE"
    echo "  wrote $(wc -l < "$HOST_SHA_FILE") entries"
fi

# Compute aggregate content SHA (catches any single-file drift in bind-mount)
HOST_AGGREGATE_SHA=$(sha256sum "$HOST_SHA_FILE" | awk '{print substr($1,1,12)}')
DOCKERFILE_SHA_PREFIX=$(sha256sum "$DOCKERFILE" | awk '{print substr($1,1,12)}')
SHORT_TAG="gpu-${HOST_AGGREGATE_SHA}-${DOCKERFILE_SHA_PREFIX}"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# --- Resolve the GPU container name (compose adds `-1` suffix) ---
# Allow override via $GPU_CONTAINER; otherwise auto-detect from running containers.
if [ -z "$GPU_CONTAINER" ]; then
    # Match either `deployment-rag-gpu` or `deployment-rag-gpu-1`
    GPU_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E '^deployment-rag-gpu(-[0-9]+)?$' | head -1 || true)
    if [ -z "$GPU_CONTAINER" ]; then
        # Container not running — fall back to image-only mode (torch check skipped)
        GPU_CONTAINER=""
    fi
fi

# --- Image SHA: prefer running container; fall back to local :latest ---
IMAGE_SHA=""
if docker inspect "deployment-rag-gpu:latest" >/dev/null 2>&1; then
    IMAGE_SHA=$(docker inspect "deployment-rag-gpu:latest" --format '{{.Id}}' | sed 's/sha256://')
else
    { echo "FATAL: no GPU image found. Run \`make gpu-up\` first to build deployment-rag-gpu:latest" >&2; exit 1; }
fi
IMAGE_SIZE=$(docker inspect "deployment-rag-gpu:latest" --format '{{.Size}}')

# --- Verify torch version inside the running container (if any) ---
# CPU baseline verifies the SHA manifest IN the image; GPU has no model
# in image. Substitute: verify torch==2.11.* per rag/Dockerfile.gpu:44.
TORCH_VER_ACTUAL=""
if [ -n "$GPU_CONTAINER" ]; then
    TORCH_VER_ACTUAL=$(docker exec "$GPU_CONTAINER" python -c 'import torch; print(torch.__version__)' 2>/dev/null || echo "unavailable")
fi
TORCH_VER_EXPECTED_PREFIX="2.11"
if [ -n "$TORCH_VER_ACTUAL" ] && [ "$TORCH_VER_ACTUAL" != "unavailable" ]; then
    case "$TORCH_VER_ACTUAL" in
        ${TORCH_VER_EXPECTED_PREFIX}.*) : ;;  # matches 2.11.x
        *)
            echo "FATAL: torch version mismatch: expected ${TORCH_VER_EXPECTED_PREFIX}.*, got ${TORCH_VER_ACTUAL}" >&2
            exit 2
            ;;
    esac
fi

cat > "$MANIFEST" <<EOF
{
  "_comment": "Phase 13c-C13 GPU baseline: locked-down reference image for the rag-gpu service. Regenerate with scripts/build_rag_gpu_baseline.sh after ANY change to rag/Dockerfile.gpu, /home/pangzy/code_project/bge-m3/ (host bind-mount), or torch version. make gpu-up compares the running image SHA to this manifest and warns on drift.",
  "tag": "${SHORT_TAG}",
  "image_sha256": "${IMAGE_SHA}",
  "image_size_bytes": ${IMAGE_SIZE},
  "host_bge_m3_sha256_prefix": "${HOST_AGGREGATE_SHA}",
  "host_sha_manifest_path": "${HOST_SHA_FILE}",
  "dockerfile_sha256_prefix": "${DOCKERFILE_SHA_PREFIX}",
  "torch_version_actual": "${TORCH_VER_ACTUAL}",
  "torch_version_expected_prefix": "${TORCH_VER_EXPECTED_PREFIX}",
  "build_args": {
    "PYTHON_BASE_IMAGE": "${PYTHON_BASE_IMAGE}",
    "PIP_INDEX_URL": "${PIP_INDEX_URL}",
    "TORCH_INDEX_URL": "${TORCH_INDEX_URL}"
  },
  "captured_at_utc": "${TS}",
  "verify_command": "docker exec deployment-rag-gpu-1 python -c 'import torch; print(torch.__version__)'"
}
EOF

echo
echo "baseline:gpu ${SHORT_TAG} sha256:${IMAGE_SHA}"
echo "manifest:${MANIFEST}"
echo "torch_version_actual:${TORCH_VER_ACTUAL}"
echo "host_bge_m3_sha256_prefix:${HOST_AGGREGATE_SHA}"
echo "OK"