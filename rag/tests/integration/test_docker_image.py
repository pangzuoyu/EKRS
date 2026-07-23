"""Heavy integration test: build the RAG Docker image and verify the
bge-m3 model is vendored correctly.

Phase 8 T8-3a acceptance: ``docker compose up`` produces a RAG image
with bge-m3 ONNX baked in. This test exercises the build locally so a
regression in rag/Dockerfile (e.g., a stray .dockerignore, a COPY that
drops the SHA manifest, a model file path typo) is caught during
nightly heavy runs, not at the next deploy.

Marked `@pytest.mark.heavy`. The test runs when:
- docker is installed AND a daemon is reachable (skip otherwise), AND
- one of:
  * the default `docker.io` registry is reachable, OR
  * the operator has exported ``EKRS_DOCKER_PYTHON_MIRROR`` pointing
    at a working mirror (e.g., ``docker.m.daocloud.io/library/python``
    for China-network dev machines).

On any other machine (no docker, no registry access) the test is a
no-op skip, which is the right behavior — the assertion passes in CI
where both conditions are met.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "rag" / "Dockerfile"

# Translation of common China-network mirrors to a full
# ``docker.m.daocloud.io/library/python:3.11-slim`` reference for the
# ``PYTHON_BASE_IMAGE`` build-arg override. Operators on a restricted
# network set ``EKRS_DOCKER_PYTHON_MIRROR`` to one of these values (or
# any fully qualified ``registry/repository/name:tag``) and the heavy
# test will pick the right base image automatically.
_DEFAULT_BASE = "python:3.11-slim"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


pytestmark = pytest.mark.heavy


def _resolved_base_image() -> str:
    """Pick the PYTHON_BASE_IMAGE build arg value, falling back to the
    canonical docker.io reference."""
    return os.environ.get("EKRS_DOCKER_PYTHON_MIRROR", _DEFAULT_BASE)


def _build_cmd() -> list[str]:
    """Build command. Build context is the repo root (matches
    deployment/docker-compose.yml), so we run from REPO_ROOT.

    PIP_INDEX_URL is overridable for restricted-network dev machines
    (default is canonical pypi.org). Operators on China-network
    hosts export EKRS_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
    and the heavy test picks it up automatically.
    """
    base = _resolved_base_image()
    pip_index = os.environ.get(
        "EKRS_PIP_INDEX_URL", "https://pypi.org/simple"
    )
    return [
        "docker", "build",
        "--build-arg", f"PYTHON_BASE_IMAGE={base}",
        "--build-arg", f"PIP_INDEX_URL={pip_index}",
        "-t", "ekrs-rag:t8-3a-test",
        "-f", str(DOCKERFILE),
        str(REPO_ROOT),
    ]


@pytest.fixture(scope="module")
def _skip_if_no_docker() -> None:
    if not _docker_available():
        pytest.skip("docker not installed on this machine")


def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    """Run a subprocess, raise on non-zero, return stdout."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.stdout


def _run_skipping_on_registry_failure(cmd: list[str]) -> str:
    """Like _run but skip-on-network-failure so the heavy test is
    a no-op on dev machines without Docker Hub access."""
    try:
        return _run(cmd)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "") + "\n" + (exc.output or "")
        network_markers = (
            "registry-1.docker.io",
            "DeadlineExceeded",
            "i/o timeout",
            "no such host",
            "connection refused",
        )
        if any(m.lower() in stderr.lower() for m in network_markers):
            pytest.skip(
                f"docker registry unreachable on this host; "
                f"set EKRS_DOCKER_PYTHON_MIRROR to a reachable mirror "
                f"or run in CI: {stderr.splitlines()[0][:200]!r}"
            )
        raise


def test_docker_file_exists() -> None:
    """Sanity gate — fail fast if rag/Dockerfile has been moved or
    renamed (the test would otherwise fail mysteriously in the build
    step)."""
    assert DOCKERFILE.exists(), f"Dockerfile missing at {DOCKERFILE}"


def test_docker_image_builds_with_model_vendored(_skip_if_no_docker: None) -> None:
    """docker build succeeds and bge-m3.sha256 in the image matches the
    repo manifest byte-for-byte.

    The image is built with a transient tag ``ekrs-rag:t8-3a-test`` so
    we never collide with `make dev`'s `ekrs-rag` tag if a developer
    has one cached.
    """
    _run_skipping_on_registry_failure(_build_cmd())

    # Compare SHA manifests.
    image_sha = _run(
        ["docker", "run", "--rm", "ekrs-rag:t8-3a-test",
         "cat", "/opt/ekrs/models/bge-m3/bge-m3.sha256"]
    )
    repo_sha = (REPO_ROOT / "rag" / "models" / "bge-m3" / "bge-m3.sha256").read_text()
    assert image_sha == repo_sha, (
        "bge-m3.sha256 in built image differs from repo manifest"
    )


def test_docker_image_contains_all_vendored_files(_skip_if_no_docker: None) -> None:
    """Each file listed in bge-m3.sha256 must exist in /opt/ekrs/models/bge-m3
    inside the image. Catches the "COPY truncated by .dockerignore"
    failure mode."""
    _run_skipping_on_registry_failure(_build_cmd())
    listing = _run(
        ["docker", "run", "--rm", "ekrs-rag:t8-3a-test",
         "ls", "/opt/ekrs/models/bge-m3"]
    )
    expected = {
        "model.onnx",
        "model.onnx_data",
        "sparse_linear.pt",
        "bge-m3.sha256",
        "config.json",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "Constant_7_attr__value",
    }
    actual = set(listing.split())
    missing = expected - actual
    assert not missing, f"image is missing vendored model files: {sorted(missing)}"


def test_docker_image_env_var_set(_skip_if_no_docker: None) -> None:
    """EMBEDDING_MODEL_DIR inside the running image must be /opt/ekrs/models/bge-m3."""
    _run_skipping_on_registry_failure(_build_cmd())
    env = _run(
        ["docker", "run", "--rm", "ekrs-rag:t8-3a-test",
         "sh", "-c", "printenv EMBEDDING_MODEL_DIR"]
    ).strip()
    assert env == "/opt/ekrs/models/bge-m3"
