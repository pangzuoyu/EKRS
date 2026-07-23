# Deployment

> Production deployment guide. Covers Kubernetes, bare-metal non-Docker, and
> the dim-migration procedure for embedding model changes.
> `make dev` (docker-compose) is for local development only — see
> `README.md` for that path.

---

## Requirements

### Python version

**Python 3.11 is required.** FlagEmbedding 1.2.13 (vendored under
`rag/models/bge-m3/`) depends on `onnxruntime<1.18.0` and `numpy<2.0`. Wheels
for these constraints are not consistently published for Python 3.12+, and
the bge-m3 ONNX model loader fails on 3.12 in CI. The nightly heavy-test
runner pins 3.11.

### Hardware

| Service | CPU | RAM | Disk |
|---------|-----|-----|------|
| rag (FastAPI) | 2 cores | 4 GB | 1 GB (audit.log rotates at 500 MB) |
| qdrant | 2 cores | 4 GB | 20 GB+ (depends on collection size; SSD strongly recommended) |
| redis | 0.5 core | 512 MB | 100 MB (no persistence required) |
| prometheus (optional) | 0.5 core | 1 GB | 10 GB (15-day retention typical) |

For > 1M chunks in Qdrant, scale `qdrant` to 4 cores / 8 GB.

### Required environment variables

```bash
PARSER_TOKEN=<≥32 char secret>          # auth header X-Parser-Token
ADMIN_KEY=<≥32 char secret>             # auth header X-Admin-Key
SHARED_STORAGE_PATH=/var/ekrs/parser_out  # JSONL input dir (read-only for RAG)
QDRANT_HOST=qdrant.svc.cluster.local
QDRANT_GRPC_PORT=6334
REDIS_URL=redis://redis.svc.cluster.local:6379/0
AUTO_REINDEX=false                      # MANDATORY in production
ENGINE_URL=https://parser.internal/callbacks/ingestion
```

Optional:

```bash
EKRS_DEBUG=false                        # MUST be false in production
METRICS_HOST=0.0.0.0                    # sidecar bind (always 0.0.0.0 in k8s)
METRICS_PORT=9090
AUDIT_LOG_PATH=/var/log/ekrs/audit.log
DEBUG_LOG_PATH=/var/log/ekrs/debug.log
PROMETHEUS_MULTIPROC_DIR=/var/run/ekrs/prom  # see K8s note below
# Phase 8 T8-3a: embedding model resolution. Default in the docker image is
# the vendored copy at /opt/ekrs/models/bge-m3; bare-metal deployments must
# either export EMBEDDING_MODEL_DIR pointing at a local checkout of
# rag/models/bge-m3/ or mount the vendored copy from the docker image.
EMBEDDING_MODEL=bge-m3
EMBEDDING_MODEL_DIR=/opt/ekrs/models/bge-m3
```

---

## Kubernetes deployment

### Persistent volumes

| PVC | Size | Mount | Owner |
|-----|------|-------|-------|
| `qdrant-data` | 50 Gi+ | `/qdrant/storage` | qdrant | `ReadWriteOnce` |
| `rag-audit` | 1 Gi | `/var/log/ekrs` | rag | `ReadWriteOnce` |
| `rag-prom` | 256 Mi | `/var/run/ekrs/prom` | rag | `ReadWriteOnce` (see below) |
| `rag-parser-mount` | (shared NFS) | `/var/ekrs/parser_out` | rag | `ReadOnlyMany` |

**`PROMETHEUS_MULTIPROC_DIR`** is special: `prometheus_client` multiproc
mode writes per-process counter files that the sidecar merges at scrape
time. The directory **must** be:

- Writable by the rag container UID
- On an `emptyDir` volume (NOT a PVC) — multiproc files are ephemeral;
  persisting them across pod restarts causes duplicate-counter bugs
- Shared between the main rag process and the metrics sidecar exporter

Example `emptyDir` stanza:

```yaml
volumeMounts:
  - name: prom-multiproc
    mountPath: /var/run/ekrs/prom
volumes:
  - name: prom-multiproc
    emptyDir:
      sizeLimit: 256Mi
```

### Ingress — token header forwarding

The RAG service authenticates two headers: `X-Parser-Token` (parser↔RAG)
and `X-Admin-Key` (admin endpoints). Most Ingress controllers **strip
custom headers by default**. For NGINX Ingress:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ekrs-rag
  annotations:
    nginx.ingress.kubernetes.io/configuration-snippet: |
      proxy_set_header X-Parser-Token $http_x_parser_token;
      proxy_set_header X-Admin-Key $http_x_admin_key;
      proxy_set_header X-Forwarded-Proto $scheme;
spec:
  rules:
    - host: rag.internal.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: rag
                port:
                  number: 8000
```

For other controllers (Traefik, HAProxy, ALB): equivalent
`request.set_header` / `proxy-set-header` config is required. Verify with:

```bash
curl -H "X-Parser-Token: $PARSER_TOKEN" https://rag.internal/v1/constraints \
  -X POST -d '{"query":"test"}' -H "Content-Type: application/json" -i
# Expect 200 (or 400 missing_context), NOT 401
```

A 401 on a known-good token is the canonical "Ingress ate my header"
symptom.

### Other Ingress controllers

The configurations below cover the three other commonly deployed
controllers. Each preserves both `X-Parser-Token` and `X-Admin-Key`.

#### Traefik (IngressRoute)

Traefik forwards all `X-*` headers by default; the Middleware below is
explicit documentation, not a functional requirement.

```yaml
apiVersion: traefik.containo.us/v1alpha1
kind: Middleware
metadata:
  name: ekrs-preserve-headers
spec:
  headers:
    customRequestHeaders:
      X-Parser-Token: ""   # Traefik 默认转发 X-* 头，此条目仅显式声明
      X-Admin-Key: ""
---
apiVersion: traefik.containo.us/v1alpha1
kind: IngressRoute
metadata:
  name: ekrs-rag
spec:
  routes:
    - kind: Rule
      match: Host(`rag.internal.example.com`)
      middlewares:
        - name: ekrs-preserve-headers
      services:
        - name: rag
          port: 8000
```

#### HAProxy (backend stanza)

```haproxy
backend rag_backend
    mode http
    http-request set-header X-Parser-Token %[req.hdr(X-Parser-Token)] if { req.hdr(X-Parser-Token) -m found }
    http-request set-header X-Admin-Key    %[req.hdr(X-Admin-Key)]    if { req.hdr(X-Admin-Key)    -m found }
    server rag1 rag:8000 check
```

No frontend changes required.

#### AWS ALB (IngressClass `alb`)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ekrs-rag
  annotations:
    kubernetes.io/ingress.class: alb
    alb.ingress.kubernetes.io/scheme: internal
    # ALB 默认透传所有 X-* 头，无需额外注解
spec:
  rules:
    - host: rag.internal.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: rag
                port:
                  number: 8000
```

### Prometheus scrape config

```yaml
scrape_configs:
  - job_name: ekrs-rag
    static_configs:
      - targets: ['rag:9090']  # sidecar exporter port, NOT 8000
    scrape_interval: 15s
```

The `/metrics` endpoint lives on a **separate port** (the sidecar
exporter, not the main RAG port). The main app on `:8000` no longer
exposes metrics (Phase 5.5 D).

### Health probes

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 30
readinessProbe:
  httpGet:
    path: /healthz
    port: 8000
  initialDelaySeconds: 15
  periodSeconds: 10
  failureThreshold: 3
```

`/healthz` returns **503** if audit log is not writable or AuditIndex is
not loaded — pod will not receive traffic until both come up.

### Resource limits

```yaml
resources:
  requests: { cpu: "500m", memory: "1Gi" }
  limits:   { cpu: "2",    memory: "4Gi" }
```

CPU-bound during bge-m3 ONNX inference on first ingest burst. Memory cap
is firm — bge-m3 ONNX session is ~1.5 GB resident.

---

## Docker image (Phase 8 T8-3a)

`rag/Dockerfile` builds an image with the bge-m3 ONNX model **vendored
inside** at `/opt/ekrs/models/bge-m3`. The image is the deployable
unit — no model download at runtime, no sidecar volume mount required.

### Build context

Build context is the **repo root** (one level above `rag/Dockerfile`),
so `COPY shared`, `COPY rag`, and `COPY rag/models/bge-m3/` are all
resolved against `EKRS/`. The compose file (`deployment/docker-compose.yml`)
sets this automatically:

```yaml
rag:
  build:
    context: ..
    dockerfile: rag/Dockerfile
    args:
      PYTHON_BASE_IMAGE: ${PYTHON_BASE_IMAGE:-python:3.11-slim}
      PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}
```

### Restricted-network build

For China-network dev machines where `docker.io` and `pypi.org` are
unreachable, two build args are overridable:

| Arg | Default | Common China-network override |
|-----|---------|------------------------------|
| `PYTHON_BASE_IMAGE` | `python:3.11-slim` | `docker.m.daocloud.io/library/python:3.11-slim` |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | `https://mirrors.aliyun.com/pypi/simple/` |

Override example:

```bash
docker build \
  --build-arg PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  -t ekrs-rag:dev .
```

The model files themselves are read from `rag/models/bge-m3/` on the
local filesystem — no model download at build time. bge-m3.sha256 is
verified byte-for-byte inside the image against the in-repo manifest
(see `tests/integration/test_docker_image.py`, marked `@pytest.mark.heavy`).

### Image size

Vendoring bge-m3 ONNX adds ~2.3 GB to the image. This is intentional:
shipping the model separately would either require a sidecar service
(extra container) or rely on a host mount (fragile under k8s
scheduling). The production image is the right place for it.

---

## Bare-metal / non-Docker deployment

Skip docker-compose; run each service directly.

```bash
# 1. Start qdrant (binary)
qdrant --grpc-port 6334 --uri http://0.0.0.0:6333 \
       --storage-snapshots-dir /var/lib/qdrant/snapshots &

# 2. Start redis
redis-server --port 6379 --daemonize yes

# 3. Start RAG (uvicorn, single worker — multiproc needs separate setup)
cd /opt/ekrs/rag
PROMETHEUS_MULTIPROC_DIR=/var/run/ekrs/prom \
  uvicorn ekrs_rag.main:app --host 0.0.0.0 --port 8000 \
  --workers 1   # see warning below
```

**Multi-worker warning**: `prometheus_client` multiproc mode requires
`PROMETHEUS_MULTIPROC_DIR` AND the sidecar exporter to run as a separate
process. With `--workers > 1`, each worker writes its own counter file
under `PROMETHEUS_MULTIPROC_DIR` and a sidecar process merges them at
scrape time. If you start `uvicorn --workers N` directly **without** the
sidecar, the `/metrics` endpoint on `:9090` will only show the worker's
own counters (or 0 if you scrape the main `:8000` port). Either:

1. Use 1 worker + sidecar exporter (simpler, recommended for < 100 QPS), or
2. Use N workers + start the sidecar as a separate process that reads
   `PROMETHEUS_MULTIPROC_DIR` and serves `:9090`.

See `rag/ekrs_rag/observability/metrics.py` for the sidecar entrypoint.

---

## Embedding dim migration (e.g., bge-small 384d → bge-m3 1024d)

> Phase 6B is the reference migration. The opposite direction (1024d → 384d)
> is **NOT supported** without a full re-ingestion of every chunk — see
> `docs/CHANGELOG.md` §Rollback strategy.

Procedure (when increasing dim):

1. **Stop ingestion.** Set `AUTO_REINDEX=false` (already the production
   default), and ensure no Parser notifications are pending. New ingest
   with the old dim will fail validation.
2. **Back up Qdrant** collection (snapshot):
   ```bash
   curl -X POST 'http://qdrant:6333/collections/<name>/snapshots'
   ```
   Keep the snapshot until the new collection is verified.
3. **Update `EMBEDDING_MODEL`** in `.env` / k8s ConfigMap to the new model.
4. **Recreate the collection** with new dim:
   ```bash
   curl -X DELETE 'http://qdrant:6333/collections/<name>'
   ```
   Or use `AUTO_REINDEX=true` **temporarily** for one restart to let
   `QdrantManager.ensure_collection` handle the rebuild — but **turn it
   off again** before the next deploy.
5. **Re-ingest** every document. There is no in-place dim conversion.
   Re-trigger the parser for each `doc_hash`:
   ```bash
   for h in $(list_all_doc_hashes); do
     curl -X POST https://parser.internal/reingest -d "{\"doc_hash\":\"$h\"}"
   done
   ```
6. **Verify** with the golden set (`make golden-test`) — recall semantics
   change slightly with embedding model swaps.
7. **Update `ekrs-handbook.md` §7.4** with the migration date and old/new
   dims.

Down-migration (1024d → 384d) is **not safe**: chunks embedded at 1024d
have no 384d representation. A downgrade requires wiping Qdrant and
re-ingesting from scratch under the old model.

---

## Production checklist

Before declaring a deployment "production ready":

- [ ] `AUTO_REINDEX=false` set
- [ ] `EKRS_DEBUG=false` set
- [ ] `PARSER_TOKEN` and `ADMIN_KEY` are ≥ 32 chars and rotated on a schedule
- [ ] `PROMETHEUS_MULTIPROC_DIR` is `emptyDir` (k8s) or tmpfs (bare metal)
- [ ] Ingress forwards `X-Parser-Token` and `X-Admin-Key` headers
- [ ] Audit log PVC mounted with `ReadWriteOnce` and 1 Gi capacity
- [ ] `/healthz` returns 200 from inside the cluster
- [ ] Prometheus scrape target shows `up`
- [ ] `make golden-test` passes against a smoke-ingested fixture set
- [ ] **`make smoke-ingestion` returns PASS** (Phase 8 T8-3b) — see "Post-deploy smoke" below
- [ ] Image SHA matches `deployment/rag-image.baseline.json` (Phase 8 T8-3a)
- [ ] Rollback tag identified (see `docs/CHANGELOG.md` §Rollback strategy)
- [ ] On-call has access to `audit.log` (PVC) and `tasks.db` (PVC)

### Post-deploy smoke

After every deploy, run the ingestion smoke as the canary step. It
exercises the full happy path in ≤ 60 s and exits non-zero on any
contract violation:

```bash
PARSER_TOKEN="$PARSER_TOKEN" bash scripts/smoke_ingestion.sh
```

The script (Phase 8 T8-3b) does the following:

1. Generates a 6-block mock JSONL at `/tmp/ekrs_smoke/<doc>/<ts>/data.jsonl`.
2. Starts a local mock callback server (Python, background) that
   records RAG's parser-side callback POST to a file.
3. Builds the `/v1/ingestion/notify` payload via
   `scripts/lib_smoke.py build-payload` (assigns a fresh uuid4
   `trace_id` for audit-log correlation).
4. POSTs the payload with `X-Parser-Token` (3 retries on transport
   errors).
5. Polls `/v1/ingestion/status/<doc_hash>` every 500 ms (timeout 30 s)
   until status ∈ {`completed`, `failed`}.
6. Scans `audit.log` for `qdrant_write_failed` events attributed to
   the same `trace_id`. Even on HTTP 200, this catches silent write
   failures (Phase 7 T1).
7. Verifies the mock callback server received a body with
   `status == "completed"`.

Each step emits a `[STEP N]` line on stderr so triage doesn't require
reading the full output. Exit codes (in `smoke_ingestion.sh` header):

| Code | Meaning |
|------|---------|
| 0 | Full happy path |
| 1 | Pre-flight (RAG unreachable, token missing, JSONL generation) |
| 2 | `/v1/ingestion/notify` returned non-2xx after 3 retries |
| 3 | Status polling never reached terminal / terminal = `failed` |
| 4 | `audit.log` contained `qdrant_write_failed` for this `trace_id` |
| 5 | Callback server did not receive `status=completed` within 10 s |

---

## Token rotation procedure (zero-downtime)

`PARSER_TOKEN` (and `ADMIN_KEY`) can be rotated without any service
downtime by exploiting comma-separated multi-token support in
`rag/ekrs_rag/api/auth.py`. During the rotation window, RAG accepts
both the old and new tokens; the parser fleet is migrated in two
phases, then the old token is removed.

### Prerequisites

- Parser fleet is horizontally scalable (multiple replicas / pods) so
  they can be restarted in batches without throughput loss.
- Secrets are stored in a Kubernetes Secret (or equivalent) referenced
  from the rag and parser deployments as the `PARSER_TOKEN` env var.
- RAG is on a build that supports comma-separated tokens (this commit
  onwards; pre-this-commit code requires a service restart for rotation).

### Procedure

1. **Append the new token** to the existing `PARSER_TOKEN` Secret value,
   comma-separated:

   ```bash
   # Old Secret value: "<old-token>"
   # New Secret value: "<old-token>,<new-token>"

   kubectl create secret generic ekrs-secrets \
     --from-literal=PARSER_TOKEN="$OLD_TOKEN,$NEW_TOKEN" \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

   The Pydantic validator in `rag/ekrs_rag/core/config.py` enforces the
   combined string is ≥ 32 chars, but does **not** validate each
   individual token. Treat the same minimum length convention (≥ 32
   chars) as the de-facto policy.

2. **Restart RAG pods** to pick up the new Secret value:

   ```bash
   kubectl rollout restart deploy/rag
   kubectl rollout status deploy/rag --timeout=120s
   ```

   After the rollout, RAG accepts both tokens.

3. **Rotate tokens in the Parser fleet** in batches:

   ```bash
   # Batch 1: drain + update + restart
   kubectl rollout restart deploy/parser
   kubectl rollout status deploy/parser --timeout=120s
   ```

   During this step the new Parser replicas use the new token; the
   remaining old replicas continue to use the old token. Both are
   accepted by RAG — zero failed requests.

4. **Verify rotation** by watching the audit log for any 401 events on
   the new token (should be zero) and for stale 401s on the old token
   (should drop to zero as old Parser replicas cycle out):

   ```bash
   # Tail auth-related failures
   kubectl logs -l app=rag -f | grep -i "401\|invalid.*token"
   ```

5. **Remove the old token** once the parser fleet has fully cycled
   (typically 5–15 minutes after step 3):

   ```bash
   kubectl create secret generic ekrs-secrets \
     --from-literal=PARSER_TOKEN="$NEW_TOKEN" \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl rollout restart deploy/rag
   ```

6. **Post-rotation check**: confirm the secret contains exactly one
   token and `auth.py` is parsing exactly one entry:

   ```bash
   kubectl exec deploy/rag -- \
     python -c "from ekrs_rag.api.auth import _parse_expected_tokens; \
                import os; \
                print(len(_parse_expected_tokens(os.environ['PARSER_TOKEN'])))"
   # Expect: 1
   ```

### Rollback

If something goes wrong mid-rotation, restore the previous Secret value
(the comma-separated form is still in your shell history from step 1)
and restart RAG — the old token continues to be accepted throughout.
