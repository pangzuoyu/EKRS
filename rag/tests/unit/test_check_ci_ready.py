"""Tests for dev_ui_v2/scripts/check-ci-ready.sh pre-flight script.

Phase 12-A: regression gate for the E2E-in-CI integration. The script is
invoked as a shell subprocess with controlled PATH / HOME / DEV_UI_V2_ROOT
so each failure mode is exercised without affecting the developer's
local environment. Five tests cover the four pre-flight checks plus the
happy path; they assert exit codes and stderr / stdout content so a
regression in any single check is caught.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "dev_ui_v2" / "scripts" / "check-ci-ready.sh"

# Capture absolute bash path BEFORE any test mutates PATH; the script's
# shebang uses `env bash` which would otherwise fail to resolve bash under
# an aggressively pruned PATH.
BASH_PATH = shutil.which("bash")
if BASH_PATH is None:  # pragma: no cover — only triggers on bash-less systems
    pytest.skip("bash not found on this system", allow_module_level=True)

# Standard system tools (dirname, sed, sort, head, ls, etc.) live here;
# tests put their fake `node`/`npx` binary dir AHEAD of these so fakes
# shadow real tools, but standard utilities remain reachable.
SYSTEM_PATHS = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"


def _make_fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    """Create executable `<tmp_path>/<name>` with given shebang/body text.

    Returns the directory the new binary lives in so the test can put it
    on PATH in isolation from any real /usr/bin tools.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    target = bin_dir / name
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the bash script with the given env, capturing output.

    Runs via the absolute bash path captured at import time so PATH
    mutation in tests does not break the `#!/usr/bin/env bash` lookup.
    """
    assert BASH_PATH is not None  # for type checker; see module-level skip
    return subprocess.run(
        [BASH_PATH, str(SCRIPT_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_node_too_old_exits_nonzero_with_stderr_message(tmp_path: Path) -> None:
    """Fake node reports v18.0.0 → version-check fails and stderr mentions 'node'.

    This is the realistic CI failure mode: nvm not sourced, the bundled
    Node is older than the pin in dev_ui_v2/.nvmrc. We exercise the
    version branch of check_node rather than the not-found branch,
    because the *response* the user sees in CI is the same: an exit-1
    with a clear, actionable stderr line containing 'node'.
    """
    _make_fake_bin(tmp_path, "node", '#!/bin/sh\necho "v18.0.0"\n')
    # npx must also be faked so the script reaches the node-version branch;
    # otherwise it short-circuits at `command -v npx` first.
    _make_fake_bin(
        tmp_path,
        "npx",
        '#!/bin/sh\n[ "$1" = "playwright" ] && echo "Version 1.46.1" && exit 0\nexit 1\n',
    )
    env: dict[str, str] = {
        "PATH": f"{tmp_path}/bin:{SYSTEM_PATHS}",
        "HOME": str(tmp_path),
    }
    result = _run_script(env)
    assert result.returncode != 0
    assert "node" in result.stderr.lower(), (
        f"expected stderr to mention 'node', got: {result.stderr!r}"
    )


def test_node_present_but_npx_missing_exits_nonzero(tmp_path: Path) -> None:
    """node on PATH but npx is not → exits non-zero mentioning npx/playwright."""
    _make_fake_bin(
        tmp_path,
        "node",
        '#!/bin/sh\necho "v20.20.2"\n',
    )
    env: dict[str, str] = {
        # PATH has our fake node; npx not present in fakes or system paths.
        "PATH": f"{tmp_path}/bin:/nonexistent-bin-only:{SYSTEM_PATHS}",
        "HOME": str(tmp_path),
    }
    result = _run_script(env)
    assert result.returncode != 0
    assert any(s in result.stderr.lower() for s in ("npx", "playwright")), (
        f"expected stderr to mention 'npx' or 'playwright', got: {result.stderr!r}"
    )


def test_node_and_npx_present_but_browser_cache_missing(tmp_path: Path) -> None:
    """node + npx OK, but $HOME/.cache/ms-playwright has no chromium-* entry."""
    _make_fake_bin(tmp_path, "node", '#!/bin/sh\necho "v20.20.2"\n')
    _make_fake_bin(
        tmp_path,
        "npx",
        '#!/bin/sh\n[ "$1" = "playwright" ] && echo "Version 1.46.1" && exit 0\nexit 1\n',
    )
    # Leave tmp_path with NO .cache/ms-playwright dir — chromium check must fail.
    env: dict[str, str] = {
        "PATH": f"{tmp_path}/bin:{SYSTEM_PATHS}",
        "HOME": str(tmp_path),
    }
    result = _run_script(env)
    assert result.returncode != 0
    assert any(
        s in result.stderr.lower()
        for s in ("chromium", "browser", "playwright", "cache")
    ), f"expected stderr to mention browser/chromium, got: {result.stderr!r}"


def test_node_npx_browser_ok_but_mock_service_worker_missing(tmp_path: Path) -> None:
    """First 3 checks pass; mockServiceWorker.js absent → script fails loudly."""
    _make_fake_bin(tmp_path, "node", '#!/bin/sh\necho "v20.20.2"\n')
    _make_fake_bin(
        tmp_path,
        "npx",
        '#!/bin/sh\n[ "$1" = "playwright" ] && echo "Version 1.46.1" && exit 0\nexit 1\n',
    )

    # Pretend a Playwright chromium has been installed.
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1097").mkdir(parents=True)

    # Point the script at a fake dev_ui_v2 root that has public/ but no worker file.
    fake_root = tmp_path / "fake_dev_ui_v2"
    (fake_root / "public").mkdir(parents=True)

    env: dict[str, str] = {
        "PATH": f"{tmp_path}/bin:{SYSTEM_PATHS}",
        "HOME": str(tmp_path),
        "DEV_UI_V2_ROOT_OVERRIDE": str(fake_root),
    }
    result = _run_script(env)
    assert result.returncode != 0
    assert "mockserviceworker" in result.stderr.lower() or "msw" in result.stderr.lower(), (
        f"expected stderr to mention mockServiceWorker/msw, got: {result.stderr!r}"
    )


def test_happy_path_exits_zero_with_ready(tmp_path: Path) -> None:
    """All 4 pre-flight checks pass → exit 0 and stdout says 'ready'."""
    _make_fake_bin(tmp_path, "node", '#!/bin/sh\necho "v20.20.2"\n')
    _make_fake_bin(
        tmp_path,
        "npx",
        '#!/bin/sh\n[ "$1" = "playwright" ] && echo "Version 1.46.1" && exit 0\nexit 1\n',
    )
    (tmp_path / ".cache" / "ms-playwright" / "chromium-1097").mkdir(parents=True)

    fake_root = tmp_path / "fake_dev_ui_v2"
    (fake_root / "public").mkdir(parents=True)
    (fake_root / "public" / "mockServiceWorker.js").write_text("// MSW worker stub")

    env: dict[str, str] = {
        "PATH": f"{tmp_path}/bin:{SYSTEM_PATHS}",
        "HOME": str(tmp_path),
        "DEV_UI_V2_ROOT_OVERRIDE": str(fake_root),
    }
    result = _run_script(env)
    assert result.returncode == 0, (
        f"expected exit 0 (happy path), got {result.returncode}; stderr={result.stderr!r}"
    )
    assert "ready" in result.stdout.lower(), (
        f"expected stdout to mention 'ready', got: {result.stdout!r}"
    )
