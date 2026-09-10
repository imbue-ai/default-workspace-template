"""Tests for the pinned uv tool location and the shadowing-install cleanup."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_TOOL_ENV = Path(__file__).with_name("_tool_env.sh")
_MNGR_TOOL = "imbue-mngr"


def _run(snippet: str, *, home: Path, tool_home: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", f'. "{_TOOL_ENV}"\n{snippet}'],
        env={**os.environ, "HOME": str(home), "TOOL_ENV_HOME": str(tool_home)},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _install_mngr_tool(home: Path, *, shebang_prefix: str = "#!") -> tuple[Path, Path]:
    """A uv-tool-shaped mngr environment under ``home``, plus its console script.

    Shaped the way uv lays one out, because the cleanup reads the script's shebang to
    decide which environment it belongs to. ``shebang_prefix`` spells that marker, so a
    test can hand it the whitespace-separated form as well.
    """
    env_dir = home / ".local" / "share" / "uv" / "tools" / _MNGR_TOOL
    (env_dir / "bin").mkdir(parents=True)
    (env_dir / "uv-receipt.toml").write_text("")
    script = home / ".local" / "bin" / "mngr"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"{shebang_prefix}{env_dir}/bin/python\n")
    return env_dir, script


def test_the_pin_installs_into_the_directory_it_puts_on_path() -> None:
    """The divergence this exists to close: uv's tool directories follow $HOME, so
    unpinned they name the runtime home while PATH names the image's -- and what gets
    installed is then not what gets run."""
    tool_dir, bin_dir, first_on_path = (
        _run(
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


def test_an_install_under_another_home_is_removed_with_its_console_script(
    tmp_path: Path,
) -> None:
    runtime_home = tmp_path / "home" / "user"
    image_home = tmp_path / "root"
    shadow_env, shadow_script = _install_mngr_tool(runtime_home)
    pinned_env, pinned_script = _install_mngr_tool(image_home)

    _run("tool_env_drop_shadowing_mngr", home=runtime_home, tool_home=image_home)

    assert not shadow_env.exists()
    assert not shadow_script.exists()
    assert pinned_env.is_dir()
    assert pinned_script.is_file()


def test_the_pinned_install_is_not_removed_when_home_reaches_it_by_another_path(
    tmp_path: Path,
) -> None:
    """The image build runs with $HOME already the pinned home. Reached as "/root/", or
    through a symlink, a string comparison would call it a shadow of itself -- and the
    cleanup would delete the very environment it protects."""
    image_home = tmp_path / "root"
    pinned_env, pinned_script = _install_mngr_tool(image_home)
    linked_home = tmp_path / "root-link"
    linked_home.symlink_to(image_home)

    _run("tool_env_drop_shadowing_mngr", home=linked_home, tool_home=image_home)
    _run(f"HOME={image_home}/ tool_env_drop_shadowing_mngr", home=image_home, tool_home=image_home)

    assert pinned_env.is_dir()
    assert pinned_script.is_file()


def test_the_console_script_goes_even_when_home_is_spelled_differently_than_the_shebang(
    tmp_path: Path,
) -> None:
    """uv bakes an absolute path into the shebang at install time; `$HOME` now may be
    spelled another way (a trailing slash, a symlink). Matching those as strings would
    remove the environment and leave the script -- a `mngr` on PATH with a dead
    interpreter, which is worse than the stale but working copy it replaced."""
    runtime_home = tmp_path / "home" / "user"
    image_home = tmp_path / "root"
    shadow_env, shadow_script = _install_mngr_tool(runtime_home)
    _install_mngr_tool(image_home)

    _run(
        f'HOME="{runtime_home}/" tool_env_drop_shadowing_mngr',
        home=runtime_home,
        tool_home=image_home,
    )

    assert not shadow_env.exists()
    assert not shadow_script.exists()


def test_the_console_script_goes_when_its_shebang_has_a_space_after_the_marker(
    tmp_path: Path,
) -> None:
    """The update apply reads the same shebang with a ``strip()`` before splitting
    (update_environment.py::_tool_location), so the two must agree on a `#! /path`
    spelling; disagreeing here removes the environment and strands the script on PATH."""
    runtime_home = tmp_path / "home" / "user"
    image_home = tmp_path / "root"
    shadow_env, shadow_script = _install_mngr_tool(runtime_home, shebang_prefix="#! ")
    _install_mngr_tool(image_home)

    _run("tool_env_drop_shadowing_mngr", home=runtime_home, tool_home=image_home)

    assert not shadow_env.exists()
    assert not shadow_script.exists()


def test_a_console_script_already_resolving_to_the_pinned_install_is_left_alone(
    tmp_path: Path,
) -> None:
    """A shim under the runtime home that points at the pinned environment is how a
    login shell is *meant* to reach mngr; only the one pointing into what was just
    removed goes with it."""
    runtime_home = tmp_path / "home" / "user"
    image_home = tmp_path / "root"
    shadow_env, shim = _install_mngr_tool(runtime_home)
    pinned_env, _ = _install_mngr_tool(image_home)
    shim.write_text(f"#!{pinned_env}/bin/python\n")

    _run("tool_env_drop_shadowing_mngr", home=runtime_home, tool_home=image_home)

    assert not shadow_env.exists()
    assert shim.is_file()


def test_nothing_is_removed_when_the_pinned_install_is_missing(tmp_path: Path) -> None:
    """A build whose install did not land has no confirmed copy to fall back on, so
    taking the shadow would leave the workspace with no mngr at all."""
    runtime_home = tmp_path / "home" / "user"
    shadow_env, shadow_script = _install_mngr_tool(runtime_home)

    _run("tool_env_drop_shadowing_mngr", home=runtime_home, tool_home=tmp_path / "root")

    assert shadow_env.is_dir()
    assert shadow_script.is_file()


def test_a_home_with_no_install_is_a_no_op_that_does_not_abort_the_build(
    tmp_path: Path,
) -> None:
    """build_workspace.sh runs under `set -e`, so a non-zero exit here would take the
    whole build down on the common case of there being nothing to clean up."""
    image_home = tmp_path / "root"
    _install_mngr_tool(image_home)

    output = _run(
        "tool_env_drop_shadowing_mngr\necho done",
        home=tmp_path / "home" / "user",
        tool_home=image_home,
    )

    assert output.strip() == "done"
