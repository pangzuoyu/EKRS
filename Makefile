.PHONY: dev test lint mock-notify install clean heavy-test golden-test test-e2e test-e2e-ci t5-acceptance gpu-up gpu-down gpu-acceptance

PYTHON ?= python3
PIP ?= pip

install:
	$(PIP) install -e ./shared
	cd rag && $(PIP) install -e ".[dev]"

dev:
	docker compose -f deployment/docker-compose.yml up --build

dev-down:
	docker compose -f deployment/docker-compose.yml down

test:
	cd rag && pytest tests/ -v --tb=short

test-cov:
	cd rag && pytest tests/ -v --tb=short --cov=ekrs_rag --cov-report=term-missing

lint:
	flake8 shared/ekrs_shared rag/ekrs_rag --max-line-length=120
	mypy shared/ekrs_shared rag/ekrs_rag --ignore-missing-imports

# Heavy tests: real bge-m3 model load. Requires Python 3.11.
# Excluded from default `make test` and PR CI; runs nightly.
heavy-test:
	cd rag && pytest tests/ -m heavy -v

# Phase 8 T8-5: chunker perf baseline (10k synthetic docs).
# Excluded from `make test` and PR CI; run on nightly heavy CI or
# locally for regression triage. See benchmarks/README.md.
bench-chunker:
	cd rag && PYTHONPATH=.. pytest ../benchmarks/test_chunker_10k.py -v -s -m heavy

# Golden set regression: 50 cases from ekrs-handbook.md §9.1
# (42 baseline + 5 chunk-level + 3 API-level from Phase 8 T8-4).
# Gate for behavior changes. See rag/tests/golden_set/test_golden_set.py
# (chunk-level via EvidenceBuilder + IntervalSolver) and
# rag/tests/golden_set/test_api_validation.py (API-level via TestClient).
golden-test:
	cd rag && pytest tests/golden_set/ -v

mock-notify:
	bash scripts/mock_parser_notify.sh

# Phase 8 T8-3b: end-to-end happy-path smoke. Requires `make dev` to
# be running (RAG at http://localhost:8000 with a valid PARSER_TOKEN).
# Generates a 6-block JSONL, POSTs /v1/ingestion/notify, polls status
# until terminal, checks audit.log for qdrant_write_failed, verifies
# the parser-side callback. Exits non-zero on any failure (see script
# header for exit-code contract).
smoke-ingestion:
	@bash scripts/smoke_ingestion.sh

# Phase 12-A: Playwright E2E suite (dev_ui_v2). Runs 6 existing T11-3
# specs under headless Chromium with MSW intercepting /v1/* + /healthz.
# Local first-time setup installs the Chromium browser (~250 MB); CI
# variant assumes the cache is warm.
test-e2e:
	cd dev_ui_v2 && npm ci && npx playwright install --with-deps chromium && npm run test:e2e
test-e2e-ci:
	cd dev_ui_v2 && npm ci --omit=optional && npx playwright test
# Pre-flight that the E2E environment can actually run the suite.
# Useful in restricted environments where `npx playwright install` is
# disallowed; see dev_ui_v2/scripts/check-ci-ready.sh.
test-e2e-ready:
	@bash dev_ui_v2/scripts/check-ci-ready.sh

# Phase 8 T8-3a: rebuild the locked-down reference image and capture
# its SHA256 into deployment/rag-image.baseline.json.
build-rag-baseline:
	@bash scripts/build_rag_baseline.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf *.egg-info shared/*.egg-info rag/*.egg-info .pytest_cache

# Run RAG service locally (without Docker)
run-local:
	cd rag && $(PYTHON) -m ekrs_rag.main

# Phase 13b T5: E2E acceptance suite (28-doc Phase12-v10-subset bench +
# equiv + failover). Requires real GPU infrastructure (BGE_M3_GPU_ENABLED=true
# in container) and ADMIN_KEY env var (for /v1/admin/gpu/invalidate).
# requires-gpu: true — runs on dedicated GPU runner only, NOT in PR gate.
# Exit codes: 0 = all acceptance lines pass; 1 = phase thresholds failed;
# 2 = GT missing or auth not configured.
t5-acceptance:
	cd rag && pytest tests/unit/test_phase13b_t5_acceptance.py tests/unit/test_admin_gpu_endpoints.py -v
	cd rag && pytest tests/integration/test_phase13b_t5_e2e.py -v -m heavy

# Phase 13b GPU 部署 — PoC 验收 (T5.1 smoke bench).
# UQ-2: precheck host 模型目录 (低成本失败预防)
# UQ-4: T5.1 容器内 exec 跑 (RAG_URL=http://localhost:8000 = 容器内 GPU 服务)
# UQ-5: gpu-up 自动停 CPU rag 防 Qdrant wipe 冲突, gpu-down 自动恢复
# UQ-6: BGE_M3_GPU_PROBE_INTERVAL_S=5 已写死在 compose override (T5.3 failover)
gpu-up:
	@test -d /home/pangzy/code_project/bge-m3 || \
		(echo "ERROR: /home/pangzy/code_project/bge-m3 missing — bind-mount would fail" && exit 1)
	cd deployment && docker compose stop rag 2>/dev/null || true
	cd deployment && docker compose --profile gpu up -d rag-gpu
	@echo "Waiting for rag-gpu healthz..."
	@for i in $$(seq 1 30); do \
		if curl -s http://localhost:8001/healthz | grep -q '"status":"ok"'; then \
			echo "rag-gpu healthy"; break; \
		fi; sleep 2; \
	done

gpu-down:
	# Phase 13c-C13 fix: `docker compose --profile gpu down` brings down
	# ALL services (rag, qdrant, redis) because `down` ignores profiles —
	# it removes every container in the project. gpu-up left the CPU rag
	# stopped (Qdrant write-conflict prevention); qdrant + redis stayed
	# running. We only want to stop+remove the GPU service, then restart
	# the CPU rag that gpu-up paused.
	cd deployment && docker compose stop rag-gpu
	cd deployment && docker compose rm -f rag-gpu
	@echo "Restart CPU rag service for normal ops..."
	cd deployment && docker compose up -d rag

# T5.1 smoke bench — 28 篇 ingest, peak mem, ingest p99.
# T5.2 (equiv) + T5.3 (failover) 单独 follow-up — 不阻塞 phase13b 合入.
# EKRS_RATE_LIMIT=6000 抬到 100 req/s 防 status poll 撞 429 (Phase 8 默认 60/min 太严).
# /healthz / /health / /metrics 已 exempt, 不受 EKRS_RATE_LIMIT 影响.
# T5_DRAIN_TIMEOUT_S=1: fresh container 上 tasks.db 为空, drain 等于空跑; 1s 跳过 (drain 在
# force=True 时 第一轮 28 个 poll_status 各最多 1s → ~28s 总; 实战比 5min 节省 4 分钟).
# host /home/pangzy/code_project/EKRS/scripts bind-mount 到容器内 /app/rag/scripts-host
# (docker-compose.override.yml rag-gpu 服务); 跑脚本前 PYTHONPATH=/app/rag/scripts-host 让
# _phase13b_common 跟 phase13b_* 能互相 import。
# 容器内 qdrant URL 是 docker 服务名 `qdrant:6333` (DNS), 不是 localhost。
# PARSER_TOKEN / ADMIN_KEY 默认 compose override 的 dev 值 (生产用 .env override)。
gpu-acceptance:
	docker compose -f deployment/docker-compose.yml exec -T rag-gpu \
		bash -c 'EKRS_RATE_LIMIT=6000 \
		         RAG_URL=http://localhost:8000 \
		         T5_DRAIN_TIMEOUT_S=1 \
		         PYTHONPATH=/app/rag/scripts-host \
		         python /app/rag/scripts-host/phase13b_poc_bench.py \
		             --phase full \
		             --qdrant-url http://qdrant:6333 \
		             --token $${PARSER_TOKEN:-change-me-to-a-secure-random-string-32chars} \
		             --admin-key $${ADMIN_KEY:-dev-admin-key-32chars-aaaaaaaaaa}'
