.PHONY: dev test lint mock-notify install clean

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

mock-notify:
	bash scripts/mock_parser_notify.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf *.egg-info shared/*.egg-info rag/*.egg-info .pytest_cache

# Run RAG service locally (without Docker)
run-local:
	cd rag && $(PYTHON) -m ekrs_rag.main
