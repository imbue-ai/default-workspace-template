"""The browser env.d unit's install of Playwright's headless shell for the test suites."""

from __future__ import annotations

import os
import stat
import subprocess
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
