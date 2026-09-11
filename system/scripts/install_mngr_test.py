"""Tests for the mngr tool install.

The real install is a ``uv tool install`` against the network, so ``uv`` is a stub on
PATH: the program runs whole, and what the stub writes down is what it was told to do --
the argument vector, the tool directories in its environment, and its working directory.
The pieces that decide those are also checked on their own, where a failure names itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import install_mngr
import pytest
import tool_env

_MANIFEST_WITHOUT_MNGR = """
[[plugins]]
path = "system/vendor/mngr/libs/mngr_claude"
tools = ["chat"]
"""

# Two plugins for the mngr tool and one for an app's, so the tool the paths are looked up
# under is something the install can get wrong.
_MANIFEST = """
[[plugins]]
path = "system/vendor/mngr/libs/mngr_claude"
tools = ["mngr", "chat"]

[[plugins]]
path = "system/vendor/mngr/libs/mngr_wait"
tools = ["mngr"]

[[plugins]]
path = "system/vendor/mngr/libs/mngr_only_an_app_wants"
tools = ["chat"]
"""


def _repo(tmp_path: Path, manifest: str) -> Path:
    path = tmp_path / install_mngr.MANIFEST_PATH
    path.parent.mkdir(parents=True)
    path.write_text(manifest)
    return tmp_path


class _StubUv:
    """A ``uv`` on PATH that records one invocation instead of installing anything."""

    def __init__(self, bin_dir: Path, record: Path) -> None:
        self._record = record
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "uv"
        script.write_text(
            "#!/bin/sh\n"
            f'exec > "{record}"\n'
            'printf "%s\\n" "$PWD" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" "$@"\n'
        )
        script.chmod(0o755)

    @property
    def working_directory(self) -> Path:
        return Path(self._record.read_text().splitlines()[0])

    @property
    def tool_directories(self) -> list[str]:
        """``UV_TOOL_DIR`` and ``UV_TOOL_BIN_DIR`` as the install handed them over."""
        return self._record.read_text().splitlines()[1:3]

    @property
    def arguments(self) -> list[str]:
        """The argument vector, less the ``uv`` the shell consumed as $0."""
        return self._record.read_text().splitlines()[3:]


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

    env = install_mngr.install_environment(
        {"UV_TOOL_DIR": "/already/chosen", "UV_TOOL_BIN_DIR": "/already/chosen/bin"}
    )

    assert env["UV_TOOL_DIR"] == "/already/chosen"
    assert env["UV_TOOL_BIN_DIR"] == "/already/chosen/bin"


def test_the_install_runs_the_command_it_built_under_the_pin_it_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The program end to end, which is where the pin either reaches uv or does not.

    The build pins the tool directories in its own shell before calling, so an install
    that computed the pin and then failed to hand it over would still look right there.
    The other caller is a person following AGENTS.md, who runs this with HOME=/home/user
    while the mngr being repaired is the one under the pinned home -- and would get a
    success message and an untouched broken tool.
    """
    repo = _repo(tmp_path, _MANIFEST)
    pinned_home = tmp_path / "root"
    uv = _StubUv(tmp_path / "stub-bin", tmp_path / "uv-invocation")
    monkeypatch.setenv("TOOL_ENV_HOME", str(pinned_home))
    monkeypatch.setenv(
        "PATH", f"{tmp_path / 'stub-bin'}{os.pathsep}{os.environ['PATH']}"
    )

    command = install_mngr.install_mngr(repo, os.environ)

    assert uv.arguments == command[1:]
    assert command == install_mngr.build_install_command(
        repo,
        ["system/vendor/mngr/libs/mngr_claude", "system/vendor/mngr/libs/mngr_wait"],
    )
    assert uv.tool_directories == [
        str(tool_env.tools_dir(pinned_home)),
        str(tool_env.bin_dir(pinned_home)),
    ]
    assert uv.working_directory.resolve() == repo.resolve()
