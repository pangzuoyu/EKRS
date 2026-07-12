"""Shared pytest fixtures for Phase 5 observability tests.

Issue 13: setup_logging handlers from prior tests would leak.
Issue 24: prometheus_client Counter/Histogram registration accumulates
across tests — Duplicate timeseries errors on second import.
"""
import pytest
from prometheus_client import REGISTRY


@pytest.fixture(autouse=True)
def _isolate_prometheus_registry():
    """Clear non-default Prometheus collectors after each test.

    Default collectors (process_, python_gc_) are preserved; user-defined
    ones (Counter/Histogram from ekrs_rag.observability.metrics) get pruned.
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