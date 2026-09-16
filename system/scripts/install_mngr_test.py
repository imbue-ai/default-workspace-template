"""Tests for the mngr tool install.

The real install is a ``uv tool install`` against the network, so the program is handed a
recorder in place of ``subprocess.run``: it runs whole, and what the recorder keeps is
what uv would have been told to do -- the argument vector, the tool directories in its
environment, and its working directory. The pieces that decide those are also checked on
their own, where a failure names itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import install_mngr
import list_mngr_plugins
import pytest
import tool_env

_MANIFEST_WITHOUT_MNGR = """
[[plugins]]
package = "imbue-mngr-claude"
subdirectory = "libs/mngr_claude"
tools = ["chat"]
"""

# Two plugins for the mngr tool and one for an app's, so the tool the packages are looked
# up under is something the install can get wrong.
_MANIFEST = """
[[plugins]]
package = "imbue-mngr-claude"
subdirectory = "libs/mngr_claude"
tools = ["mngr", "chat"]

[[plugins]]
package = "imbue-mngr-wait"
subdirectory = "libs/mngr_wait"
tools = ["mngr"]

[[plugins]]
package = "imbue-mngr-only-an-app-wants"
subdirectory = "libs/mngr_only_an_app_wants"
tools = ["chat"]
"""

_REV = "0123456789abcdef0123456789abcdef01234567"
_PIN = list_mngr_plugins.GitPin(git_url="https://github.com/imbue-ai/mngr", rev=_REV)
_PYPROJECT = (
    "[tool.uv.sources]\n"
    f'imbue-mngr = {{ git = "{_PIN.git_url}", rev = "{_REV}", subdirectory = "libs/mngr" }}\n'
)


def _requirement(package: str, subdirectory: str) -> str:
    return _PIN.requirement(package, subdirectory)


def _repo(tmp_path: Path, manifest: str) -> Path:
    path = tmp_path / install_mngr.MANIFEST_PATH
    path.parent.mkdir(parents=True)
    path.write_text(manifest)
    (tmp_path / install_mngr.PYPROJECT_PATH).write_text(_PYPROJECT)
    return tmp_path


class _RecordingUv:
    """A ``Run`` that keeps one invocation instead of installing anything."""

    def __init__(self) -> None:
        self.command: list[str] | None = None
        self.working_directory: Path | None = None
        self.environment: Mapping[str, str] | None = None
        self.checked: bool | None = None

    def __call__(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path,
        env: Mapping[str, str],
        check: bool,
    ) -> object:
        self.command = list(command)
        self.working_directory = cwd
        self.environment = env
        self.checked = check
        return None

    @property
    def tool_directories(self) -> list[str | None]:
        """``UV_TOOL_DIR`` and ``UV_TOOL_BIN_DIR`` as the install handed them over."""
        assert self.environment is not None
        return [
            self.environment.get(name) for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR")
        ]


def test_the_base_package_and_every_plugin_go_in_one_command(tmp_path: Path) -> None:
    """Two commands is the bug: installing the base alone rebuilds the environment from it
    and drops every extra, so anything that stops in between strands a plugin-less mngr."""
    command = install_mngr.build_install_command(
        _PIN,
        list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, _PIN, "mngr"),
    )

    assert command == [
        "uv",
        "tool",
        "install",
        _requirement("imbue-mngr", "libs/mngr"),
        "--with",
        _requirement("imbue-mngr-claude", "libs/mngr_claude"),
        "--with",
        _requirement("imbue-mngr-wait", "libs/mngr_wait"),
        "--reinstall",
    ]


def test_a_local_tree_installs_the_base_and_every_plugin_editable_from_it(tmp_path: Path) -> None:
    """A checkout at system/vendor/mngr is the dev and CI override of the pin."""
    tree = list_mngr_plugins.LocalTree(root=tmp_path / "system" / "vendor" / "mngr")

    command = install_mngr.build_install_command(
        tree,
        list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, tree, "mngr"),
    )

    assert command == [
        "uv",
        "tool",
        "install",
        "-e",
        str(tree.root / "libs" / "mngr"),
        "--with-editable",
        str(tree.root / "libs" / "mngr_claude"),
        "--with-editable",
        str(tree.root / "libs" / "mngr_wait"),
        "--reinstall",
    ]


def test_an_empty_plugin_list_refuses_rather_than_installing_the_base_alone(
    tmp_path: Path,
) -> None:
    """The shell form could not see this: the substitution that produced the list swallowed
    the lister's exit status, so `set -e` passed and the install proceeded with nothing."""
    with pytest.raises(install_mngr.NoPluginsListed):
        install_mngr.build_install_command(_PIN, [])


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
    """The program end to end, which is where the pin either reaches the install or not.

    The build pins the tool directories in its own shell before calling, so an install
    that computed the pin and then failed to hand it over would still look right there.
    The other caller is a person following AGENTS.md, who runs this with HOME=/home/user
    while the mngr being repaired is the one under the pinned home -- and would get a
    success message and an untouched broken tool.

    The environment handed over is empty rather than the process's own: an already-set
    ``UV_TOOL_DIR`` wins over the computed pin (by design, covered above), and
    ``_tool_env.sh``'s ``tool_env_pin`` exports one into every shell that sources it.
    """
    repo = _repo(tmp_path, _MANIFEST)
    pinned_home = tmp_path / "root"
    uv = _RecordingUv()
    monkeypatch.setenv("TOOL_ENV_HOME", str(pinned_home))

    command = install_mngr.install_mngr(repo, {}, uv)

    assert uv.command == command
    assert command == install_mngr.build_install_command(
        _PIN,
        list_mngr_plugins.plugin_arguments_for_tool(_MANIFEST, _PIN, "mngr"),
    )
    assert uv.tool_directories == [
        str(tool_env.tools_dir(pinned_home)),
        str(tool_env.bin_dir(pinned_home)),
    ]
    assert uv.working_directory is not None
    assert uv.working_directory.resolve() == repo.resolve()
    assert uv.checked is True
