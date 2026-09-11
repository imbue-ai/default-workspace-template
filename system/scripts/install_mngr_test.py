"""Tests for the mngr tool install.

The install itself is a ``uv tool install`` against the network, so what is exercised here
is everything that decides *what* it runs: the argument vector, the refusal that keeps a
plugin-less install from happening at all, and the tool-directory pin. The refusal path
returns before any subprocess, so none of this shells out.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import install_mngr
import tool_env

_MANIFEST = """
[[plugins]]
path = "system/vendor/mngr/libs/mngr_claude"
tools = ["mngr", "chat"]

[[plugins]]
path = "system/vendor/mngr/libs/mngr_wait"
tools = ["mngr"]
"""

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


def test_the_install_is_pinned_to_the_tool_directory_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent runs this with HOME=/home/user while the mngr being repaired is the one on
    PATH under the pinned home. Unpinned, uv reports success into a directory nothing runs
    from and leaves the broken copy untouched."""
    pinned_home = tmp_path / "root"
    monkeypatch.setenv("TOOL_ENV_HOME", str(pinned_home))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)

    install_mngr.main(["--repo-root", str(_repo(tmp_path, _MANIFEST_WITHOUT_MNGR))])

    assert os.environ["UV_TOOL_DIR"] == str(tool_env.tools_dir(pinned_home))
    assert os.environ["UV_TOOL_BIN_DIR"] == str(tool_env.bin_dir(pinned_home))


def test_a_caller_that_already_pinned_the_tool_directory_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_workspace.sh pins before calling, and its pin is the one that must hold."""
    monkeypatch.setenv("TOOL_ENV_HOME", str(tmp_path / "ignored"))
    monkeypatch.setenv("UV_TOOL_DIR", "/already/chosen")

    install_mngr.main(["--repo-root", str(_repo(tmp_path, _MANIFEST_WITHOUT_MNGR))])

    assert os.environ["UV_TOOL_DIR"] == "/already/chosen"
