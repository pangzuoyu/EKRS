#!/usr/bin/env bash
# Phase 12-A: Pre-flight check that dev_ui_v2 is ready to run Playwright E2E
# tests in CI. Verifies four prerequisites and exits 0 on success or non-zero
# with a stderr hint on any failure.
#
# Override targets for tests / restricted environments:
#   DEV_UI_V2_ROOT_OVERRIDE   absolute path to dev_ui_v2/ (defaults to parent
#                             of this script's directory)
#   PLAYWRIGHT_BROWSERS_PATH  Playwright cache root (defaults to
#                             $HOME/.cache/ms-playwright)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_UI_V2_ROOT="${DEV_UI_V2_ROOT_OVERRIDE:-$(dirname "$SCRIPT_DIR")}"
PLAYWRIGHT_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-${HOME:-}/.cache/ms-playwright}"

prefix="[check-ci-ready]"

check_node() {
  if ! command -v node >/dev/null 2>&1; then
    echo "$prefix FAIL: 'node' not found in PATH (install Node 20.20.0+ or run 'nvm use')" >&2
    return 1
  fi
  if ! command -v npx >/dev/null 2>&1; then
    echo "$prefix FAIL: 'npx' not found in PATH" >&2
    return 1
  fi
  local min_version cur_version
  min_version="20.20.0"
  cur_version="$(node --version | sed 's/^v//')"
  if [ "$(printf 'v%s\nv%s\n' "$min_version" "$cur_version" | sort -V | head -n1)" != "v$min_version" ]; then
    echo "$prefix FAIL: node >= $min_version required (got v$cur_version)" >&2
    return 1
  fi
  if ! npx playwright --version >/dev/null 2>&1; then
    echo "$prefix FAIL: 'playwright' not installed (run: cd dev_ui_v2 && npm ci)" >&2
    return 1
  fi
  return 0
}

check_browser_cache() {
  if ! compgen -G "$PLAYWRIGHT_CACHE/chromium-*" >/dev/null; then
    echo "$prefix FAIL: Playwright chromium browser cache missing at $PLAYWRIGHT_CACHE (run: npx playwright install --with-deps chromium)" >&2
    return 1
  fi
  return 0
}

check_msw_worker() {
  local msw_file="$DEV_UI_V2_ROOT/public/mockServiceWorker.js"
  if [ ! -s "$msw_file" ]; then
    echo "$prefix FAIL: MSW worker file missing: $msw_file (run: cd dev_ui_v2 && npx msw init public/)" >&2
    return 1
  fi
  return 0
}

main() {
  check_node || return 1
  check_browser_cache || return 1
  check_msw_worker || return 1
  echo "ready: check-ci-ready passed (node $(node --version), playwright OK, chromium OK, MSW worker OK)"
  return 0
}

main || exit 1
exit 0
