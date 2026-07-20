"""Shared pytest fixtures for Phase 5 observability tests.

Issue 13: setup_logging handlers from prior tests would leak.
Issue 24: prometheus_client Counter/Histogram registration accumulates
across tests — Duplicate timeseries errors on second import.
"""
import pytest
from prometheus_client import REGISTRY


@pytest.fixture(autouse=True)
def _redirect_shared_storage_path(tmp_path, monkeypatch):
    """T1: redirect SHARED_STORAGE_PATH to a per-test tmpdir.

    The integration-fix lifespan check (main.py startup) refuses to boot
    if SHARED_STORAGE_PATH does not exist on disk. Production defaults to
    /parsed_lib, but that path is not mounted in the test environment and
    must not be created system-wide. Redirecting to a per-test tmp_path
    keeps tests hermetic and lets every existing TestClient-based test
    exercise lifespan without modification.
    """
    from ekrs_rag.core.config import settings as _settings

    monkeypatch.setattr(_settings, "SHARED_STORAGE_PATH", tmp_path)
    yield


@pytest.fixture(autouse=True, scope="session")
def _isolate_prometheus_registry():
    """Clear non-default Prometheus collectors at end of session.

    Default collectors (process_, python_gc_) are preserved; user-defined
    ones (Counter/Histogram from ekrs_rag.observability.metrics) are pruned.

    Scope is session-level (was per-test): per-test pruning emptied the
    registry between tests in the same file, causing later tests to
    fail with "missing metric" assertions. Session scope keeps collectors
    alive across tests; the original isolation purpose (preventing
    Duplicate timeseries errors on `importlib.reload()`) is preserved
    because reloads don't happen within a session anyway.
    """
    yield
    # Remove only our metric families by name
    to_remove = []
    for collector in list(REGISTRY._collector_to_names.keys()):
        names = REGISTRY._collector_to_names.get(collector, set())
        if any(n.startswith("rag_") for n in names):
            to_remove.append(collector)
    for c in to_remove:
        try:
            REGISTRY.unregister(c)
        except KeyError:
            pass

    # Phase 5.5 D: also clean up multiproc-mode collectors. MultiProcessCollector
    # (registered via lifespan when PROMETHEUS_MULTIPROC_DIR is set) prefixes
    # its child collectors with 'prometheus_multiproc_'. Without this, repeated
    # sessions accumulate duplicates.
    multiproc_to_remove = []
    for collector in list(REGISTRY._collector_to_names.keys()):
        names = REGISTRY._collector_to_names.get(collector, set())
        if any(n.startswith("prometheus_multiproc_") for n in names):
            multiproc_to_remove.append(collector)
    for c in multiproc_to_remove:
        try:
            REGISTRY.unregister(c)
        except KeyError:
            pass
