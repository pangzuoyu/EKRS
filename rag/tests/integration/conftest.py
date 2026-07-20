"""Pytest fixtures shared across integration tests.

The `_redirect_shared_storage_path` autouse fixture exists ONLY here
(integration scope) because only TestClient-based integration tests
trigger the FastAPI lifespan startup check that requires
SHARED_STORAGE_PATH to exist on disk. Pure unit tests under
`tests/unit/` do not run the lifespan and must not have this redirect
applied — moving it here keeps unit-test isolation hermetic.
"""
import pytest


@pytest.fixture(autouse=True)
def _redirect_shared_storage_path(tmp_path, monkeypatch):
    """Redirect SHARED_STORAGE_PATH to a per-test tmpdir for integration tests.

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