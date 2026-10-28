"""The browser env.d units: the Playwright path lookup they share, the Fortress unit's links from
Playwright's default paths, and the test headless shell's install."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import playwright_browser_paths
import pytest

_FORTRESS_UNIT_PATH = Path(__file__).parent / "env.d" / "1000-playwright-fortress.sh"
_TEST_SHELL_UNIT_PATH = (
    Path(__file__).parent / "env.d" / "1010-playwright-test-headless-shell.sh"
)


def _run_unit_function(
    unit_path: Path,
    name: str,
    variables: dict[str, Path],
    env: dict[str, str] | None = None,
    arguments: tuple[Path, ...] = (),
    after: str = "",
) -> subprocess.CompletedProcess[str]:
    """Runs the unit's shell function ``name`` with ``arguments`` under ``set -euo pipefail``,
    called outside ``||``, with ``variables`` set and ``after`` run once it returns."""
    unit = unit_path.read_text()
    start = unit.index(f"\n{name}() {{\n") + 1
    function = unit[start : unit.index("\n}\n", start) + len("\n}\n")]
    assignments = "".join(
        f"{variable}={shlex.quote(str(value))}\n"
        for variable, value in variables.items()
    )
    script = (
        "set -euo pipefail\n"
        "_log() { printf '%s\\n' \"$*\"; }\n"
        + assignments
        + function
        + " ".join([name, *(shlex.quote(str(argument)) for argument in arguments)])
        + "\n"
        + after
    )
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )


def _put_on_path(bin_dir: Path, name: str, body: str) -> dict[str, str]:
    """Writes an executable bash script ``name`` into ``bin_dir``; returns an environment that finds it first."""
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/usr/bin/env bash\n{body}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _write_empty_executable(path: Path) -> None:
    """Creates ``path`` (and its parent directories) as an empty executable file."""
    path.parent.mkdir(parents=True)
    path.write_text("")
    path.chmod(0o755)


def _run_install_test_headless_shell(
    repo_root: Path, browsers_dir: Path, link: Path, shell: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Runs the unit's ``_install_test_headless_shell`` with the shell resolved to ``shell``."""
    return _run_unit_function(
        _TEST_SHELL_UNIT_PATH,
        "_install_test_headless_shell",
        {
            "REPO_ROOT": repo_root,
            "_TEST_BROWSERS_DIR": browsers_dir,
            "_TEST_HEADLESS_SHELL_LINK": link,
        },
        env=env,
        arguments=(shell,),
    )


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
    _write_empty_executable(shell)
    if is_install_complete:
        marker.write_text("")
    calls = tmp_path / "uv-calls"
    # A `uv` that records how it was run and, as a finished `playwright install` does, writes the marker.
    env = _put_on_path(
        tmp_path / "bin",
        "uv",
        f'printf "%s %s\\n" "$PLAYWRIGHT_BROWSERS_PATH" "$*" >> {str(calls)!r}\ntouch {str(marker)!r}\n',
    )

    result = _run_install_test_headless_shell(tmp_path, browsers_dir, link, shell, env)

    assert result.returncode == 0, result.stdout + result.stderr
    expected_calls = (
        []
        if is_install_complete
        else [f"{browsers_dir} run python -m playwright install --only-shell chromium"]
    )
    assert (calls.read_text().splitlines() if calls.exists() else []) == expected_calls
    assert marker.exists()
    assert link.resolve() == shell.resolve()


def test_a_failed_install_leaves_no_link_to_the_incomplete_shell(
    tmp_path: Path,
) -> None:
    browsers_dir = tmp_path / "playwright-test-browsers"
    shell = (
        browsers_dir / "chromium_headless_shell-1" / "chrome-linux" / "headless_shell"
    )
    link = browsers_dir / "headless-shell"
    # An earlier install got as far as the executable and the link, but never wrote its marker.
    _write_empty_executable(shell)
    link.symlink_to(shell)
    env = _put_on_path(tmp_path / "bin", "uv", "exit 1\n")

    result = _run_install_test_headless_shell(tmp_path, browsers_dir, link, shell, env)

    assert result.returncode != 0
    assert "install FAILED" in result.stdout
    assert not link.is_symlink()


def test_a_unit_that_cannot_resolve_the_shell_still_succeeds(tmp_path: Path) -> None:
    # The fixtures fall back to Fortress, so the converge must not report the unit failed.
    env = {
        **_put_on_path(tmp_path / "bin", "uv", "exit 1\n"),
        "ENV_CONVERGE_WORKSPACE_DIR": str(tmp_path),
    }
    env.pop("DWT_SKIP_BROWSER_UNIT", None)

    result = subprocess.run(
        ["bash", str(_TEST_SHELL_UNIT_PATH)], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "will retry next converge" in result.stdout


def test_the_playwright_paths_resolve_from_the_locked_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    browsers_dir = tmp_path / "playwright-test-browsers"
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    exit_code = playwright_browser_paths.main(
        [
            "chromium",
            "no-such-browser",
            "chromium-headless-shell",
            f"chromium-headless-shell={browsers_dir}",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    # A name that cannot be resolved keeps its line, so the units can read the rest by position.
    default_chromium, unknown, default_shell, test_shell = captured.out.splitlines()
    assert unknown == ""
    assert "could not resolve no-such-browser" in captured.err
    assert Path(default_chromium) != Path(default_shell)
    # The executable sits two levels under its browser directory, which sits in the browsers directory.
    default_cache = Path(default_shell).parents[2]
    assert Path(default_shell).parents[1].name.startswith("chromium_headless_shell-")
    assert Path(test_shell) == browsers_dir / Path(default_shell).relative_to(
        default_cache
    )


@pytest.mark.parametrize("is_shell_path_blocked", [False, True])
def test_both_playwright_defaults_point_at_fortress_and_a_failed_link_only_warns(
    tmp_path: Path, is_shell_path_blocked: bool
) -> None:
    install_dir = tmp_path / "fortress"
    fortress = install_dir / "tilion-fortress" / "tilion"
    _write_empty_executable(fortress)
    default_chromium = tmp_path / "cache" / "chromium-1" / "chrome-linux" / "chrome"
    # A regular file where a parent directory should be makes the link impossible.
    shell_root = tmp_path / ("blocker" if is_shell_path_blocked else "cache")
    if is_shell_path_blocked:
        shell_root.write_text("")
    default_shell = (
        shell_root / "chromium_headless_shell-1" / "chrome-linux" / "headless_shell"
    )

    result = _run_unit_function(
        _FORTRESS_UNIT_PATH,
        "_point_playwright_defaults_at_fortress",
        {
            "_FORTRESS_INSTALL_DIR": install_dir,
            "_PW_DEFAULT_CHROMIUM": default_chromium,
            "_PW_DEFAULT_HEADLESS_SHELL": default_shell,
        },
        after="echo unit-continued\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "unit-continued"
    assert default_chromium.readlink() == fortress
    if is_shell_path_blocked:
        assert "WARNING could not link chromium-headless-shell" in result.stdout
    else:
        assert default_shell.readlink() == fortress
