# Phase 8 T8-3a: PYTHON_BASE_IMAGE and PIP_INDEX_URL are overridable
# for networks where Docker Hub (docker.io) and PyPI
# (files.pythonhosted.org) are unreachable. China-network dev
# machines commonly use:
#   PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
#   PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# Defaults match the canonical references so a vanilla `docker build`
# works on any host with full Docker Hub + PyPI access (CI runners,
# dev laptops in unrestricted networks).
#
# Override example for restricted networks:
#   docker build \
#     --build-arg PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
#     --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
#     -t ekrs-rag:dev .
#
# Build context (set by deployment/docker-compose.yml) is the REPO ROOT
# (one level above this Dockerfile). COPY paths below are evaluated
# relative to that context, NOT relative to rag/. This is what makes
# `COPY shared`, `COPY rag`, and `COPY rag/models/bge-m3/` work.
ARG PYTHON_BASE_IMAGE=python:3.11-slim
ARG PIP_INDEX_URL=https://pypi.org/simple

FROM ${PYTHON_BASE_IMAGE}

# ARGs declared before FROM are not visible inside the build stages
# unless redeclared. We need PIP_INDEX_URL inside the RUN pip install
# steps, so declare it again here. PYTHON_BASE_IMAGE is not used in
# subsequent stages (it was only consumed by FROM).
ARG PIP_INDEX_URL

WORKDIR /app

# Install shared package first (cache layer)
COPY shared /app/shared
RUN pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        /app/shared

# Install RAG package
COPY rag /app/rag
RUN pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        /app/rag

# Phase 8 T8-3a: vendor the bge-m3 ONNX model + sparse head + SHA
# manifest into the image at a stable path. The rag entrypoint
# resolves EMBEDDING_MODEL_DIR (/opt/ekrs/models/bge-m3) at startup
# and verifies against bge-m3.sha256 (D1). Image size grows by
# ~2.3 GB; this is acceptable because the production image is the
# right place for the model — shipping it separately would either
# require a sidecar service (extra container) or rely on a host
# mount (which is fragile under k8s scheduling).
COPY rag/models/bge-m3/ /opt/ekrs/models/bge-m3/

ENV EMBEDDING_MODEL_DIR=/opt/ekrs/models/bge-m3 \
    EMBEDDING_MODEL=bge-m3

WORKDIR /app/rag

EXPOSE 8000

CMD ["python", "-m", "ekrs_rag.main"]