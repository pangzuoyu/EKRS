.PHONY: dev test lint mock-notify install clean heavy-test golden-test

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

# Golden set regression: 42 cases from ekrs-handbook.md §9.1
# (rag/tests/golden_set/test_golden_set.py). Gate for behavior changes.
golden-test:
	cd rag && pytest tests/golden_set/ -v

mock-notify:
	bash scripts/mock_parser_notify.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf *.egg-info shared/*.egg-info rag/*.egg-info .pytest_cache

# Run RAG service locally (without Docker)
run-local:
	cd rag && $(PYTHON) -m ekrs_rag.main
