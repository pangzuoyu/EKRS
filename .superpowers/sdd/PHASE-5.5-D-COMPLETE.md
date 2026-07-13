# Phase 5.5 D: /metrics 主端口下线 → Sidecar Exporter — DONE

**Date:** 2026-07-13
**Range:** aa224fc..3f6bfa0
**Commits:** 9 (T1-T9)
**Tests:** 315 passed, 1 skipped, 0 failed

All spec §验收 checks pass:
- 主端口 /metrics 404
- Sidecar :9090 暴露 Prometheus text
- 3 test_metrics_exporter 测试 pass
- 文件清理: routes/metrics.py + test_metrics_endpoint.py
- .env.example: +METRICS_HOST/PORT/-METRICS_TOKEN
- 主端口 import metrics 引用已删
- coverage ≥ 78%
- docker-compose: prometheus sidecar service
- deployment/prometheus.yml: scrape rag:9090

Reviewer findings addressed (T4):
- fresh CollectorRegistry (avoids REGISTRY pollution)
- bind-conflict try/except (EADDRINUSE survival)
- try/finally around teardown (lifespan startup-failure cleanup)
- METRICS_HOST default 0.0.0.0 (docker-compose cross-container scrape)
- PROMETHEUS_MULTIPROC_DIR pre-startup RuntimeError (MmapedValue)
- PROMETHEUS_MULTIPROC_DIR wipe-between-restarts doc
- _sync_lifespan dropped → TestClient (reuse fix)

Deferred (Phase 5.5 F):
- audit.log rotation + /healthz filter (user deferred 2026-07-13)

Out of scope (unchanged):
- Phase 5.5 A/B/C/E: 各自 spec/plan
- /healthz 暴露 exporter alive: Phase 6 follow-up
- Alertmanager / PromQL 报警规则
- Iron Rules / audit schema / Phase 5 replay 行为