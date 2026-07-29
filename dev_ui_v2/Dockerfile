# Phase 11 T11-4 — dev_ui_v2 production image.
#
# Multi-stage: Node 20 alpine builds the Vite SPA, then nginx 1.27 alpine
# serves the static bundle and reverse-proxies /v1/* and /healthz to the
# RAG backend. Final image is ~40 MB (nginx base + static assets).
#
# Build context: dev_ui_v2/ (parent of public/, src/, scripts/, package.json).
# Compose wiring: deployment/docker-compose.yml `dev_ui_v2` service.
#
# Registry mirrors: NODE_BASE_IMAGE and NGINX_BASE_IMAGE are ARGs so the
# docker-compose.override.yml can swap them for Chinese mirrors on
# restricted networks (same pattern as rag's PYTHON_BASE_IMAGE).

# Global ARGs (declared before any FROM so they can be consumed by both stages).
# Each stage must re-declare the ARG to use it after the FROM boundary.
ARG NODE_BASE_IMAGE=node:20-alpine
ARG NGINX_BASE_IMAGE=nginx:1.27-alpine

# --- Stage 1: build SPA ---------------------------------------------------
FROM ${NODE_BASE_IMAGE} AS build

WORKDIR /app

# Cache deps first so source changes don't bust the install layer.
# The mirrored node:20-alpine ships npm 10.8.2 (has an "Exit handler
# never called!" bug on this kernel) — upgrade to npm 11 globally.
# package-lock.json is pre-pinned to registry.npmmirror.com (rewritten
# from the original tencent mirror, which is unreachable from this
# network). --include=dev ensures devDeps (typescript, vite, eslint)
# land — all required by the production build.
COPY package.json package-lock.json ./
RUN npm install -g npm@11 \
    && npm ci --include=dev --no-audit --no-fund \
    && echo "--- npm ci ok ---" \
    && node_modules/.bin/tsc --version \
    && node_modules/.bin/vite --version

# Copy the rest of the source. .dockerignore excludes node_modules,
# dist, tests, playwright-report, .git, coverage.
COPY . .

# Production build. msw/browser is dynamically imported only when
# import.meta.env.DEV is true, so the prod dist tree-shakes it.
# Invoke the local binaries directly — the mirrored npm ci sometimes
# doesn't make `tsc`/`vite` available on PATH even with --include=dev.
RUN ./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build

# --- Stage 2: serve with nginx -------------------------------------------
# Re-declare the ARG so ${NGINX_BASE_IMAGE} resolves inside this stage.
ARG NGINX_BASE_IMAGE=nginx:1.27-alpine
FROM ${NGINX_BASE_IMAGE} AS runtime

# Custom nginx config: SPA fallback + reverse proxy to RAG backend.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Static assets from the Vite build. /usr/share/nginx/html is the default
# nginx document root.
COPY --from=build /app/dist /usr/share/nginx/html

# The MSW service worker (tests/mocks/browser.ts) is dev-only — the production
# build under /app/dist does NOT include mockServiceWorker.js (Vite only
# copies /public/ contents that were committed, and that's the msw init
# template). No purge needed.

# Healthcheck: confirm the SPA index is served. The /healthz route would
# also work but proxies to the backend, which can be down during deploy —
# the SPA index is the canonical "container is up" signal.
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -q --spider http://localhost:80/ || exit 1

EXPOSE 80

# nginx:alpine default CMD is fine.
