# OpenWolf

@.wolf/OPENWOLF.md

This project uses OpenWolf for context management. Read and follow .wolf/OPENWOLF.md every session. Check .wolf/cerebrum.md before generating code. Check .wolf/anatomy.md before reading files.


# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Engineering Knowledge Recovery System (EKRS) — extracts structured engineering constraints (temperature, pressure, material limits) from unstructured documents (PDF/Word/DWG), computes parameter feasible ranges via a deterministic solver, and provides scope-aware conflict detection. Full specification: `ekrs.md`.

## Current State

Phase 1 implementation complete (as of 2026-04-10):
- shared/ekrs_shared/ — Pydantic models, normalizer (affine temp conversion), audit, utils
- rag/ekrs_rag/ingestion/ — IR parser, chunker (semantic, scope-aware), pipeline
- rag/ekrs_rag/retrieval/ — Qdrant client (Phase 1: dummy vectors)
- rag/ekrs_rag/api/routes/ — Ingestion notify/status endpoints, metrics
- rag/tests/ — 90 unit/integration tests, all passing

Phase 2 (solver core) not yet implemented.

## Architecture

Monorepo with three deployable units:

```
shared/ekrs_shared/   → Pydantic models, normalizer, audit base (installed as editable dep)
rag/ekrs_rag/         → FastAPI service: ingestion, retrieval (Qdrant), constraint solving
dev_ui/               → Streamlit debug UI (dev only)
deployment/           → docker-compose, k8s manifests
```

Key data flow: Parser (external) → `POST /v1/ingestion/notify` → RAG reads JSONL, vectorizes into Qdrant → callback to Parser. Queries: `POST /v1/constraints` → semantic retrieval → NumericHint extraction → interval solver → structured result.

## Seven Iron Rules (must never be violated)

| ID | Rule | Enforcement |
|----|------|-------------|
| R1 | Every numeric_hint must have source_span, block_id, context_window | Validate on ingestion |
| R2 | Solver is a pure function — no I/O, no state, no side effects | Unit test determinism |
| R3 | Three-gate pipeline: recall → extract → solve; any failure blocks the result | Golden set tests |
| R4 | Context priority: User > Explicit_Doc > Inferred_Doc > Default | Show source in output |
| R5 | Only entity-overlap scoring for KG — no graph DB, no multi-hop | No graph DB dependency |
| R6 | strict=true forbids inference; missing context returns 400 | API test |
| R7 | Every hint carries scope_path; queries can filter by scope | Multi-branch tests |

## Commands (once code exists)

```bash
make dev          # Start docker-compose + uvicorn + streamlit
make test         # pytest rag/tests/
make golden       # Golden set CI gate (must 100% pass)
make lint         # flake8 + mypy on shared/ and rag/
make mock-notify  # Simulate parser notification for testing

docs/solutions/  # documented solutions to past problems (bugs, best practices, workflow patterns), organized by category with YAML frontmatter (module, tags, problem_type)

Run a single test:
```bash
cd rag && pytest tests/unit/test_solver.py -k "test_name" -v
```

## Key Dependencies

- **Python 3.11+**, FastAPI 0.115, Pydantic 2.8, Qdrant client 1.11
- **portion** (interval arithmetic library — critical for solver)
- **bge-m3** ONNX for embeddings (dense 1024d + sparse)
- **Redis** for distributed locks and replay cache
- **aiosqlite** for task state

## Code Conventions

- All logs: structured JSON via `python-json-logger` (see spec §12 for required fields)
- Audit log (`audit.log`): permanent, never disabled, records every solve with evidence
- Debug log: only when `EKRS_DEBUG=true`, rotatable, max 100MB x 5 backups
- `shared/` is installed as editable dep (`pip install -e ../shared`) from both `rag/` and `dev_ui/`

## Environment Variables

Minimal set defined in `.env.example` (spec §18). Key ones:
- `PARSER_TOKEN` — shared secret for parser↔RAG auth (≥32 chars)
- `SHARED_STORAGE_PATH` — where parser writes JSONL, RAG reads
- `EKRS_DEBUG` — enables debug UI at `/dev-ui` and verbose logging
- `QDRANT_HOST`, `QDRANT_GRPC_PORT`, `REDIS_URL`

## Development Phases (from spec §6)

1. Foundation: DB, versioning, heartbeat, callback server
2. Deterministic solver core: hint extractor, evidence builder, interval solver, context manager
3. Scope-aware retrieval & multi-branch output
4. System integration: idempotent callbacks, distributed locks, reconciliation
5. Observability: Prometheus metrics, audit log, CI gate, Replay mode

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
