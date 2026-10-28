#!/usr/bin/env bash
# env.d unit: Playwright's headless shell, for the test suites' browser fixtures only
# (pytest_workspace_env.browsers picks it). It is a separate build (about 110 MB) from
# Fortress, kept for tests because it is lighter: a chat or shell browser suite ran
# about a third faster in about a third less memory on it than on Fortress, with every
# test passing on both. It lives in its own directory, outside Playwright's default
# cache, so nothing reaches it by default: a bare launch keeps using Fortress, whose
# stealth the browser service and agents' own browsing rely on. The fixtures launch it
# through the stable link and fall back to Fortress when it is missing; a failed
# install still fails the unit, so env-converge records it. Ordered after
# 1000-playwright-fortress.sh, whose `playwright install-deps` puts in the system
# libs this build needs too.
#
# env.d contract: idempotent with a fast satisfied-check -- NO marker files of our
# own. The installed build follows the locked Playwright, so a Playwright upgrade
# brings the matching build on the next converge.
set -euo pipefail

readonly REPO_ROOT="${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}"

_log() {
    printf '[env.d/playwright-test-headless-shell] %s\n' "$*"
}

# The same opt-out as the rest of the browser stack (see 1000-playwright-fortress.sh).
if [ "${DWT_SKIP_BROWSER_UNIT:-}" = "1" ]; then
    _log "DWT_SKIP_BROWSER_UNIT=1 -- skipping the test headless shell install"
    exit 0
fi

readonly _TEST_BROWSERS_DIR="/opt/playwright-test-browsers"
readonly _TEST_HEADLESS_SHELL_LINK="${_TEST_BROWSERS_DIR}/headless-shell"

# Where the locked Playwright installs the shell under the tests' own browsers
# directory (system/scripts/playwright_browser_paths.py); empty when it cannot be resolved.
_resolve_test_headless_shell_path() {
    (cd "$REPO_ROOT" && uv run python system/scripts/playwright_browser_paths.py "chromium-headless-shell=$_TEST_BROWSERS_DIR") || true
}

_install_test_headless_shell() {
    local shell_path="$1"
    if [ -z "$shell_path" ]; then
        _log "could not resolve the headless shell's path; tests use Fortress"
        return 1
    fi
    # Playwright writes this marker only once the build is fully extracted, so an install
    # interrupted mid-extract (its executable may already exist) is redone. The link comes
    # down first so a failed install leaves the tests on Fortress, not on a partial or
    # previous build.
    if [ ! -f "$(dirname "$(dirname "$shell_path")")/INSTALLATION_COMPLETE" ]; then
        if ! rm -f "$_TEST_HEADLESS_SHELL_LINK"; then
            _log "could not remove $_TEST_HEADLESS_SHELL_LINK before reinstalling; tests may launch an incomplete build"
            return 1
        fi
        _log "installing Playwright's headless shell into $_TEST_BROWSERS_DIR"
        if ! (cd "$REPO_ROOT" && PLAYWRIGHT_BROWSERS_PATH="$_TEST_BROWSERS_DIR" uv run python -m playwright install --only-shell chromium); then
            _log "install FAILED; tests use Fortress until the next converge"
            return 1
        fi
    fi
    if [ "$(readlink "$_TEST_HEADLESS_SHELL_LINK" 2>/dev/null)" = "$shell_path" ]; then
        _log "$_TEST_HEADLESS_SHELL_LINK already points at $shell_path, satisfied"
        return 0
    fi
    if ! ln -sfn "$shell_path" "$_TEST_HEADLESS_SHELL_LINK"; then
        _log "could not link $_TEST_HEADLESS_SHELL_LINK -> $shell_path; tests use Fortress"
        return 1
    fi
    _log "$_TEST_HEADLESS_SHELL_LINK -> $shell_path"
}

main() {
    if ! _install_test_headless_shell "$(_resolve_test_headless_shell_path)"; then
        _log "unit failed; tests use Fortress until the next converge retries"
        return 1
    fi
    _log "unit satisfied"
}

main "$@"
