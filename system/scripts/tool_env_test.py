"""Tests for the pinned uv tool location and the shadowing-install cleanup.

The pin is shell (it exports into the calling shell, which a child process cannot do), so
it is driven through bash; everything else is ``tool_env.py``, called directly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import tool_env

_TOOL_ENV_SH = Path(__file__).with_name("_tool_env.sh")


def _run_shell(snippet: str, *, home: Path, tool_home: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", f'. "{_TOOL_ENV_SH}"\n{snippet}'],
        env={**os.environ, "HOME": str(home), "TOOL_ENV_HOME": str(tool_home)},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _install_mngr_tool(home: Path, *, shebang_prefix: str = "#!") -> tuple[Path, Path]:
    """A uv-tool-shaped mngr environment under ``home``, plus its console script.

    Shaped the way uv lays one out, because the cleanup reads the script's shebang to
    decide which environment it belongs to and the receipt to confirm it is a tool at all.
    ``shebang_prefix`` spells that marker, so a test can hand it the spaced form as well.
    """
    env_dir = tool_env.tools_dir(home) / tool_env.MNGR_TOOL_NAME
    (env_dir / "bin").mkdir(parents=True)
    (env_dir / tool_env.RECEIPT).write_text("")
    script = tool_env.bin_dir(home) / tool_env.MNGR_EXECUTABLE
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"{shebang_prefix}{env_dir}/bin/python\n")
    return env_dir, script


def test_the_pin_installs_into_the_directory_it_puts_on_path() -> None:
    """The divergence this exists to close: uv's tool directories follow $HOME, so
    unpinned they name the runtime home while PATH names the image's -- and what gets
    installed is then not what gets run."""
    tool_dir, bin_dir, first_on_path = (
        _run_shell(
            'tool_env_pin\nprintf "%s\\n%s\\n%s\\n" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" "${PATH%%:*}"',
            home=Path("/home/user"),
            tool_home=Path("/root"),
        )
        .strip()
        .splitlines()
    )

    assert tool_dir == "/root/.local/share/uv/tools"
    assert bin_dir == "/root/.local/bin"
    assert first_on_path == bin_dir


def test_the_shell_pin_and_the_module_agree_on_where_tools_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin is shell and the sweep is Python, so they have to name the same directory;
    otherwise the build installs into one place and cleans around another."""
    monkeypatch.setenv("TOOL_ENV_HOME", "/somewhere")
    pinned = _run_shell(
        'tool_env_pin\nprintf "%s\\n" "$UV_TOOL_DIR"',
        home=Path("/home/user"),
        tool_home=Path("/somewhere"),
    ).strip()

    assert pinned == str(tool_env.tools_dir(tool_env.tool_home()))


@pytest.mark.parametrize(
    "spelling",
    ["plain", "trailing-slash", "symlinked-home", "space-after-shebang-marker"],
)
def test_an_install_under_another_home_goes_with_its_console_script(
    tmp_path: Path, spelling: str
) -> None:
    """uv bakes an absolute path into the shebang at install time, and ``$HOME`` may be
    spelled another way by the time this runs. Comparing those as strings would remove the
    environment and leave the script -- a `mngr` on PATH with a dead interpreter, worse
    than the stale but working copy it replaced."""
    runtime_home = tmp_path / "home" / "user"
    pinned_home = tmp_path / "root"
    prefix = "#! " if spelling == "space-after-shebang-marker" else "#!"
    shadow_env, shadow_script = _install_mngr_tool(runtime_home, shebang_prefix=prefix)
    pinned_env, pinned_script = _install_mngr_tool(pinned_home)

    swept = runtime_home
    if spelling == "trailing-slash":
        swept = Path(f"{runtime_home}/")
    elif spelling == "symlinked-home":
        swept = tmp_path / "home-link"
        swept.symlink_to(runtime_home)

    removed = tool_env.remove_shadowing_mngr_installs(
        tool_env.tools_dir(pinned_home), [swept]
    )

    assert not shadow_env.exists()
    assert not shadow_script.exists()
    # Resolved, because what is reported is spelled the way the caller named the home.
    assert {path.resolve() for path in removed} == {
        shadow_env.resolve(),
        shadow_script.resolve(),
    }
    assert pinned_env.is_dir()
    assert pinned_script.is_file()


def test_the_pinned_install_is_not_removed_when_reached_by_another_path(
    tmp_path: Path,
) -> None:
    """The build runs with $HOME already the pinned home. Reached as "/root/", or through a
    symlink, a string comparison would call it a shadow of itself -- and the cleanup would
    delete the very environment it protects."""
    pinned_home = tmp_path / "root"
    pinned_env, pinned_script = _install_mngr_tool(pinned_home)
    linked_home = tmp_path / "root-link"
    linked_home.symlink_to(pinned_home)

    removed = tool_env.remove_shadowing_mngr_installs(
        tool_env.tools_dir(pinned_home), [linked_home, Path(f"{pinned_home}/")]
    )

    assert removed == []
    assert pinned_env.is_dir()
    assert pinned_script.is_file()


def test_a_console_script_already_resolving_to_the_pinned_install_is_left_alone(
    tmp_path: Path,
) -> None:
    """A shim under the runtime home pointing at the pinned environment is how a login
    shell is *meant* to reach mngr; only the one pointing into what was removed goes."""
    runtime_home = tmp_path / "home" / "user"
    pinned_home = tmp_path / "root"
    shadow_env, shim = _install_mngr_tool(runtime_home)
    pinned_env, _ = _install_mngr_tool(pinned_home)
    shim.write_text(f"#!{pinned_env}/bin/python\n")

    tool_env.remove_shadowing_mngr_installs(
        tool_env.tools_dir(pinned_home), [runtime_home]
    )

    assert not shadow_env.exists()
    assert shim.is_file()


def test_nothing_is_removed_when_the_install_being_kept_is_missing(
    tmp_path: Path,
) -> None:
    """A build whose install did not land has no confirmed copy to fall back on, so taking
    the shadow would leave the workspace with no mngr at all."""
    runtime_home = tmp_path / "home" / "user"
    shadow_env, shadow_script = _install_mngr_tool(runtime_home)

    removed = tool_env.remove_shadowing_mngr_installs(
        tool_env.tools_dir(tmp_path / "root"), [runtime_home]
    )

    assert removed == []
    assert shadow_env.is_dir()
    assert shadow_script.is_file()


def test_a_home_with_no_install_is_a_no_op(tmp_path: Path) -> None:
    pinned_home = tmp_path / "root"
    _install_mngr_tool(pinned_home)

    removed = tool_env.remove_shadowing_mngr_installs(
        tool_env.tools_dir(pinned_home), [tmp_path / "home" / "user"]
    )

    assert removed == []


def test_the_build_entry_point_sweeps_the_home_it_runs_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What build_workspace.sh invokes: the pinned home comes from TOOL_ENV_HOME and the
    home to sweep from $HOME, so a create (HOME=/home/user) cleans up after itself."""
    runtime_home = tmp_path / "home" / "user"
    pinned_home = tmp_path / "root"
    shadow_env, _ = _install_mngr_tool(runtime_home)
    pinned_env, _ = _install_mngr_tool(pinned_home)
    monkeypatch.setenv("TOOL_ENV_HOME", str(pinned_home))
    monkeypatch.setenv("HOME", str(runtime_home))

    assert tool_env.main(["drop-shadowing-mngr"]) == 0

    assert not shadow_env.exists()
    assert pinned_env.is_dir()
