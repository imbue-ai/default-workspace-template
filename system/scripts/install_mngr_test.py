"""Tests for the mngr tool install.

The install itself is a ``uv tool install`` against the network, so what is exercised here
is everything that decides *what* it runs: the argument vector, the refusal that keeps a
plugin-less install from happening at all, and the environment the install runs under.
None of it shells out: the refusal returns before any subprocess, and the pin is an
environment the install computes from the caller's rather than a mutation of the process's
own.
"""

from __future__ import annotations

from pathlib import Path

import install_mngr
import pytest
import tool_env

_MANIFEST_WITHOUT_MNGR = """
[[plugins]]
path = "system/vendor/mngr/libs/mngr_claude"
tools = ["chat"]
"""


def _repo(tmp_path: Path, manifest: str) -> Path:
    path = tmp_path / install_mngr.MANIFEST_PATH
    path.parent.mkdir(parents=True)
    path.write_text(manifest)
    return tmp_path


def test_the_base_package_and_every_plugin_go_in_one_command(tmp_path: Path) -> None:
    """Two commands is the bug: installing the base alone rebuilds the environment from it
    and drops every extra, so anything that stops in between strands a plugin-less mngr."""
    command = install_mngr.build_install_command(
        tmp_path,
        ["system/vendor/mngr/libs/mngr_claude", "system/vendor/mngr/libs/mngr_wait"],
    )

    assert command == [
        "uv",
        "tool",
        "install",
        "-e",
        str(tmp_path / install_mngr.MNGR_SOURCE_DIR),
        "--with-editable",
        str(tmp_path / "system/vendor/mngr/libs/mngr_claude"),
        "--with-editable",
        str(tmp_path / "system/vendor/mngr/libs/mngr_wait"),
        "--reinstall",
    ]


def test_an_empty_plugin_list_refuses_rather_than_installing_the_base_alone(
    tmp_path: Path,
) -> None:
    """The shell form could not see this: the substitution that produced the list swallowed
    the lister's exit status, so `set -e` passed and the install proceeded with nothing."""
    with pytest.raises(install_mngr.NoPluginsListed):
        install_mngr.build_install_command(tmp_path, [])


def test_a_manifest_that_assigns_mngr_nothing_exits_nonzero_without_installing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _MANIFEST_WITHOUT_MNGR)

    assert install_mngr.main(["--repo-root", str(repo)]) == 1

    assert "no plugins" in capsys.readouterr().err


def test_the_install_is_pinned_to_the_tool_directory_the_build_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent runs this with HOME=/home/user while the mngr being repaired is the one
    under the pinned home. Unpinned, uv reports success into a directory nothing runs from
    and leaves the broken copy untouched."""
    pinned_home = tmp_path / "root"
    monkeypatch.setenv("TOOL_ENV_HOME", str(pinned_home))

    env = install_mngr.install_environment({})

    assert env["UV_TOOL_DIR"] == str(tool_env.tools_dir(pinned_home))
    assert env["UV_TOOL_BIN_DIR"] == str(tool_env.bin_dir(pinned_home))


def test_a_caller_that_already_pinned_the_tool_directory_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_workspace.sh pins before calling, and its pin is the one that must hold."""
    monkeypatch.setenv("TOOL_ENV_HOME", str(tmp_path / "ignored"))

    env = install_mngr.install_environment({"UV_TOOL_DIR": "/already/chosen"})

    assert env["UV_TOOL_DIR"] == "/already/chosen"
