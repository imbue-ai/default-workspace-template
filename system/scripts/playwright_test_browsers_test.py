"""The browser env.d unit's Playwright path lookup and its install of the headless shell for the test suites."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_UNIT_PATH = Path(__file__).parent / "env.d" / "1000-playwright-fortress.sh"


def _function(name: str) -> str:
    """The definition of the unit's shell function ``name``, through its closing brace."""
    unit = _UNIT_PATH.read_text()
    start = unit.index(f"\n{name}() {{\n") + 1
    return unit[start : unit.index("\n}\n", start) + len("\n}\n")]


def _put_on_path(bin_dir: Path, name: str, body: str) -> dict[str, str]:
    """Writes an executable bash script ``name`` into ``bin_dir``; returns an environment that finds it first."""
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/usr/bin/env bash\n{body}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


@pytest.mark.parametrize("is_install_complete", [True, False])
def test_the_shell_is_installed_until_playwright_marks_it_complete(
    tmp_path: Path, is_install_complete: bool
) -> None:
    browsers_dir = tmp_path / "playwright-test-browsers"
    browser_dir = browsers_dir / "chromium_headless_shell-1"
    shell = browser_dir / "chrome-linux" / "headless_shell"
    marker = browser_dir / "INSTALLATION_COMPLETE"
    link = browsers_dir / "headless-shell"
    # The executable exists either way: an install interrupted mid-extract leaves it without the marker.
    shell.parent.mkdir(parents=True)
    shell.write_text("")
    shell.chmod(0o755)
    if is_install_complete:
        marker.write_text("")
    calls = tmp_path / "uv-calls"
    # A `uv` that records how it was run and, as a finished `playwright install` does, writes the marker.
    env = _put_on_path(
        tmp_path / "bin",
        "uv",
        f'printf "%s %s\\n" "$PLAYWRIGHT_BROWSERS_PATH" "$*" >> {str(calls)!r}\ntouch {str(marker)!r}\n',
    )
    script = (
        "set -euo pipefail\n"
        "_log() { printf '%s\\n' \"$*\"; }\n"
        f"REPO_ROOT={str(tmp_path)!r}\n"
        f"_TEST_BROWSERS_DIR={str(browsers_dir)!r}\n"
        f"_TEST_HEADLESS_SHELL_LINK={str(link)!r}\n"
        f"_PW_TEST_HEADLESS_SHELL={str(shell)!r}\n"
        + _function("_install_test_headless_shell")
        + "_install_test_headless_shell\n"
    )

    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected_calls = (
        []
        if is_install_complete
        else [f"{browsers_dir} run python -m playwright install --only-shell chromium"]
    )
    assert (calls.read_text().splitlines() if calls.exists() else []) == expected_calls
    assert marker.exists()
    assert link.resolve() == shell.resolve()


def test_the_playwright_paths_resolve_from_the_locked_playwright(
    tmp_path: Path,
) -> None:
    browsers_dir = tmp_path / "playwright-test-browsers"
    # `uv run python ...` runs this test's interpreter, which has the locked Playwright.
    env = _put_on_path(
        tmp_path / "bin", "uv", f'shift 2\nexec {sys.executable!r} "$@"\n'
    )
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    script = (
        "set -euo pipefail\n"
        f"REPO_ROOT={str(Path(__file__).parents[2])!r}\n"
        f"_TEST_BROWSERS_DIR={str(browsers_dir)!r}\n"
        + _function("_resolve_playwright_paths")
        + "_resolve_playwright_paths\n"
        'printf "%s\\n" "$_PW_DEFAULT_CHROMIUM" "$_PW_DEFAULT_HEADLESS_SHELL" "$_PW_TEST_HEADLESS_SHELL"\n'
    )

    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    default_chromium, default_shell, test_shell = (
        Path(line) for line in result.stdout.splitlines()
    )
    assert default_chromium != default_shell
    # The executable sits two levels under its browser directory, which sits in the browsers directory.
    default_cache = default_shell.parents[2]
    assert default_shell.parents[1].name.startswith("chromium_headless_shell-")
    assert test_shell == browsers_dir / default_shell.relative_to(default_cache)


@pytest.mark.parametrize("is_shell_path_blocked", [False, True])
def test_both_playwright_defaults_point_at_fortress_and_a_failed_link_only_warns(
    tmp_path: Path, is_shell_path_blocked: bool
) -> None:
    install_dir = tmp_path / "fortress"
    fortress = install_dir / "tilion-fortress" / "tilion"
    fortress.parent.mkdir(parents=True)
    fortress.write_text("")
    fortress.chmod(0o755)
    default_chromium = tmp_path / "cache" / "chromium-1" / "chrome-linux" / "chrome"
    # A regular file where a parent directory should be makes the link impossible.
    shell_root = tmp_path / ("blocker" if is_shell_path_blocked else "cache")
    if is_shell_path_blocked:
        shell_root.write_text("")
    default_shell = (
        shell_root / "chromium_headless_shell-1" / "chrome-linux" / "headless_shell"
    )
    # Called the way main() calls it: outside `||`, so `set -e` is in force.
    script = (
        "set -euo pipefail\n"
        "_log() { printf '%s\\n' \"$*\"; }\n"
        f"_FORTRESS_INSTALL_DIR={str(install_dir)!r}\n"
        f"_PW_DEFAULT_CHROMIUM={str(default_chromium)!r}\n"
        f"_PW_DEFAULT_HEADLESS_SHELL={str(default_shell)!r}\n"
        + _function("_point_playwright_defaults_at_fortress")
        + "_point_playwright_defaults_at_fortress\n"
        "echo unit-continued\n"
    )

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "unit-continued"
    assert default_chromium.readlink() == fortress
    if is_shell_path_blocked:
        assert "WARNING could not link chromium-headless-shell" in result.stdout
    else:
        assert default_shell.readlink() == fortress
