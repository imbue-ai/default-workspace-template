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


def _run_shell(snippet: str, *, home: Path, tool_home: Path | None = None) -> str:
    """Run ``snippet`` with ``_tool_env.sh`` sourced. ``tool_home=None`` leaves
    ``TOOL_ENV_HOME`` unset, so the shell falls to its own default."""
    env = {**os.environ, "HOME": str(home)}
    if tool_home is None:
        env.pop("TOOL_ENV_HOME", None)
    else:
        env["TOOL_ENV_HOME"] = str(tool_home)
    result = subprocess.run(
        ["bash", "-c", f'. "{_TOOL_ENV_SH}"\n{snippet}'],
        env=env,
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
    script = tool_env.bin_dir(home) / "mngr"
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
    """The pin is shell and the sweep is Python, so they have to name the same directories;
    otherwise the build installs into one place and cleans around another. Both are checked:
    the tool directory is where an environment lands, and the bin directory is the one the
    pin puts on PATH -- and the one the update apply aims a last-resort install at. Neither
    side is overridden here, because what they hold separately -- and can therefore drift --
    is the default each falls back to when nothing sets TOOL_ENV_HOME, which is the
    production case."""
    monkeypatch.delenv("TOOL_ENV_HOME", raising=False)
    pinned_tools, pinned_bin = (
        _run_shell(
            'tool_env_pin\nprintf "%s\\n%s\\n" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"',
            home=Path("/home/user"),
        )
        .strip()
        .splitlines()
    )

    assert pinned_tools == str(tool_env.tools_dir(tool_env.tool_home()))
    assert pinned_bin == str(tool_env.bin_dir(tool_env.tool_home()))


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

    removed = tool_env.remove_shadowing_installs(
        tool_env.tools_dir(pinned_home),
        [swept],
        tool_env.MNGR_TOOL_NAME,
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

    removed = tool_env.remove_shadowing_installs(
        tool_env.tools_dir(pinned_home),
        [linked_home, Path(f"{pinned_home}/")],
        tool_env.MNGR_TOOL_NAME,
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

    tool_env.remove_shadowing_installs(
        tool_env.tools_dir(pinned_home),
        [runtime_home],
        tool_env.MNGR_TOOL_NAME,
    )

    assert not shadow_env.exists()
    assert shim.is_file()


def test_every_console_script_into_a_removed_environment_goes_with_it(
    tmp_path: Path,
) -> None:
    """A tool can install several console scripts (the browser app ships two). Removing
    its environment while leaving any of them would put a script with a dead interpreter
    first on a login shell's PATH; a script into another environment is not the sweep's."""
    runtime_home = tmp_path / "home" / "user"
    pinned_home = tmp_path / "root"
    shadow_env, first_script = _install_mngr_tool(runtime_home)
    _install_mngr_tool(pinned_home)
    second_script = first_script.with_name("mngr-second")
    second_script.write_text(f"#!{shadow_env}/bin/python\n")
    other_env = tool_env.tools_dir(runtime_home) / "other-tool"
    (other_env / "bin").mkdir(parents=True)
    other_script = first_script.with_name("other")
    other_script.write_text(f"#!{other_env}/bin/python\n")

    removed = tool_env.remove_shadowing_installs(
        tool_env.tools_dir(pinned_home), [runtime_home], tool_env.MNGR_TOOL_NAME
    )

    assert set(removed) == {shadow_env, first_script, second_script}
    assert other_script.is_file()


def test_a_console_script_uv_linked_into_the_bin_dir_goes_with_its_environment(
    tmp_path: Path,
) -> None:
    """uv links a tool's console scripts into the bin dir as symlinks to the copies in the
    environment's own bin/, so the sweep reads the shebang through the link and must
    remove the link itself before deleting what it points at."""
    runtime_home = tmp_path / "home" / "user"
    pinned_home = tmp_path / "root"
    shadow_env, planted_script = _install_mngr_tool(runtime_home)
    _install_mngr_tool(pinned_home)
    env_script = shadow_env / "bin" / planted_script.name
    planted_script.rename(env_script)
    planted_script.symlink_to(env_script)

    removed = tool_env.remove_shadowing_installs(
        tool_env.tools_dir(pinned_home), [runtime_home], tool_env.MNGR_TOOL_NAME
    )

    assert set(removed) == {shadow_env, planted_script}
    assert not planted_script.is_symlink()
    assert not shadow_env.exists()


def test_nothing_is_removed_when_the_install_being_kept_is_missing(
    tmp_path: Path,
) -> None:
    """A build whose install did not land has no confirmed copy to fall back on, so taking
    the shadow would leave the workspace with no mngr at all."""
    runtime_home = tmp_path / "home" / "user"
    shadow_env, shadow_script = _install_mngr_tool(runtime_home)

    removed = tool_env.remove_shadowing_installs(
        tool_env.tools_dir(tmp_path / "root"),
        [runtime_home],
        tool_env.MNGR_TOOL_NAME,
    )

    assert removed == []
    assert shadow_env.is_dir()
    assert shadow_script.is_file()


def test_a_home_with_no_install_is_a_no_op(tmp_path: Path) -> None:
    pinned_home = tmp_path / "root"
    _install_mngr_tool(pinned_home)

    removed = tool_env.remove_shadowing_installs(
        tool_env.tools_dir(pinned_home),
        [tmp_path / "home" / "user"],
        tool_env.MNGR_TOOL_NAME,
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


def _install_receipted_tool(
    home: Path, tool_name: str, requirement: str, scripts: tuple[str, ...]
) -> tuple[Path, list[Path]]:
    """A tool environment under ``home`` laid out as ``uv tool install`` leaves one: a
    receipt naming ``requirement`` (one TOML inline table), each console script in the
    environment's own bin/, and a symlink to it in the home's bin directory."""
    env_dir = tool_env.tools_dir(home) / tool_name
    (env_dir / "bin").mkdir(parents=True)
    (env_dir / tool_env.RECEIPT).write_text(
        f"[tool]\nrequirements = [{requirement}]\n"
    )
    links: list[Path] = []
    for script in scripts:
        env_script = env_dir / "bin" / script
        env_script.write_text(f"#!{env_dir}/bin/python\n")
        link = tool_env.bin_dir(home) / script
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(env_script)
        links.append(link)
    return env_dir, links


def test_every_app_tool_goes_from_every_home_and_nothing_else_does(
    tmp_path: Path,
) -> None:
    """Old builds installed each app as an editable uv tool beside the workspace venv. Those
    go, in both homes they were left in and with every console script into them (the
    browser app ships two), while the mngr tool and a tool installed from anywhere else
    stay: the receipt's editable source is the only thing that marks an app's tool."""
    checkout = tmp_path / "mngr-vol" / "workspace"
    apps = checkout / "system" / "apps"
    (apps / "browser").mkdir(parents=True)
    (apps / "system_interface").mkdir()
    # The build reached the checkout through a symlink, as /home/user reaches /mngr-vol.
    linked_checkout = tmp_path / "workspace"
    linked_checkout.symlink_to(checkout)
    pinned_home = tmp_path / "root"
    runtime_home = tmp_path / "home" / "user"
    browser_env, browser_links = _install_receipted_tool(
        pinned_home,
        "browser",
        f'{{ name = "browser", editable = "{linked_checkout}/system/apps/browser/" }}',
        ("browser-service", "agentic-browser-fleet"),
    )
    stale_shell_env, stale_shell_links = _install_receipted_tool(
        runtime_home,
        "system-interface",
        f'{{ name = "system-interface", editable = "{apps}/system_interface" }}',
        ("system-interface",),
    )
    mngr_env, mngr_links = _install_receipted_tool(
        pinned_home,
        tool_env.MNGR_TOOL_NAME,
        '{ name = "imbue-mngr", git = "https://github.com/imbue-ai/mngr?rev=abc" }',
        ("mngr",),
    )
    elsewhere = tmp_path / "scratch" / "helper"
    elsewhere.mkdir(parents=True)
    other_env, other_links = _install_receipted_tool(
        runtime_home,
        "helper",
        f'{{ name = "helper", editable = "{elsewhere}" }}',
        ("helper",),
    )

    removed = tool_env.remove_app_tool_installs([pinned_home, runtime_home], apps)

    assert set(removed) == {
        browser_env,
        *browser_links,
        stale_shell_env,
        *stale_shell_links,
    }
    assert not browser_env.exists() and not stale_shell_env.exists()
    assert not any(link.is_symlink() for link in [*browser_links, *stale_shell_links])
    assert mngr_env.is_dir() and all(link.is_symlink() for link in mngr_links)
    assert other_env.is_dir() and all(link.is_symlink() for link in other_links)


def test_an_environment_the_receipt_does_not_describe_is_left_alone(
    tmp_path: Path,
) -> None:
    """A receipt that will not parse, or one whose editable requirement is a different
    package (an app pulled in as a plugin of some other tool), proves nothing about the
    environment it sits in, and a deletion needs proof."""
    apps = tmp_path / "workspace" / "system" / "apps"
    (apps / "chat").mkdir(parents=True)
    home = tmp_path / "root"
    unreadable_env, _ = _install_receipted_tool(home, "chat", "", ("chat-app",))
    (unreadable_env / tool_env.RECEIPT).write_text("[tool\n")
    host_env, _ = _install_receipted_tool(
        home,
        "host-tool",
        f'{{ name = "chat", editable = "{apps}/chat" }}',
        ("host-tool",),
    )

    assert tool_env.remove_app_tool_installs([home], apps) == []
    assert unreadable_env.is_dir() and host_env.is_dir()


def test_the_build_entry_point_drops_the_app_tools_of_the_checkout_it_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What build_workspace.sh invokes: the pinned home from TOOL_ENV_HOME and the home it
    runs under from $HOME are both swept, so an image baked with app tools under /root
    loses them on its next create, and a create's own $HOME copy goes too."""
    repo_root = tmp_path / "workspace"
    apps = repo_root / "system" / "apps"
    (apps / "files").mkdir(parents=True)
    pinned_home = tmp_path / "root"
    runtime_home = tmp_path / "home" / "user"
    pinned_env, _ = _install_receipted_tool(
        pinned_home,
        "files-app",
        f'{{ name = "files-app", editable = "{apps}/files" }}',
        ("files-app",),
    )
    runtime_env, _ = _install_receipted_tool(
        runtime_home,
        "files-app",
        f'{{ name = "files-app", editable = "{apps}/files" }}',
        ("files-app",),
    )
    monkeypatch.setenv("TOOL_ENV_HOME", str(pinned_home))
    monkeypatch.setenv("HOME", str(runtime_home))

    assert tool_env.main(["drop-app-tools", "--repo-root", str(repo_root)]) == 0

    assert not pinned_env.exists()
    assert not runtime_env.exists()
