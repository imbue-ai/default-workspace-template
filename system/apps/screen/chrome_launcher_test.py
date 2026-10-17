import threading
from pathlib import Path

import pytest

from screen.chrome_args import STRIPPED_FROM_PLAYWRIGHT
from screen.chrome_args import launch_args
from screen.chrome_launcher import ChromiumLaunchRequest
from screen.chrome_launcher import SINGLETON_NAMES
from screen.chrome_launcher import SubprocessChromiumLauncher
from screen.chrome_launcher import clear_stale_singleton
from screen.chrome_launcher import extension_paths
from screen.chrome_launcher import read_devtools_port
from screen.chrome_launcher import should_disable_sandbox
from screen.errors import ChromiumStartError


def _args(is_sandbox_disabled: bool = True) -> list[str]:
    return launch_args(
        user_data_dir="/tmp/profile",
        remote_debugging_port=0,
        window_size=(1280, 800),
        extensions=("/opt/ext/a",),
        is_sandbox_disabled=is_sandbox_disabled,
    )


def test_launch_args_keep_stealth_suppress_the_bad_flag_infobar_and_declare_english() -> None:
    arguments = _args()
    assert "--test-type" in arguments
    assert "--no-sandbox" in arguments
    assert "--disable-blink-features=AutomationControlled" in arguments
    assert "--lang=en-US" in arguments
    assert "--accept-lang=en-US,en" in arguments
    assert "--user-data-dir=/tmp/profile" in arguments
    assert "--remote-debugging-port=0" in arguments
    assert "--window-size=1280,800" in arguments
    assert "--load-extension=/opt/ext/a" in arguments
    # The window manager places Chromium's windows: nothing pins their position.
    assert not any(argument.startswith("--window-position") for argument in arguments)
    assert "--no-sandbox" not in _args(is_sandbox_disabled=False)


def test_launch_args_never_emit_a_playwright_anti_stealth_switch() -> None:
    assert not set(_args()) & set(STRIPPED_FROM_PLAYWRIGHT)


def test_the_devtools_port_file_is_cleared_with_the_other_singletons_and_read_back(tmp_path: Path) -> None:
    for name in SINGLETON_NAMES:
        (tmp_path / name).write_text("stale")
    clear_stale_singleton(tmp_path)
    assert not any((tmp_path / name).exists() for name in SINGLETON_NAMES)
    assert read_devtools_port(tmp_path) is None
    (tmp_path / "DevToolsActivePort").write_text("41234\n/devtools/browser/abc\n")
    assert read_devtools_port(tmp_path) == 41234


def test_extension_paths_lists_every_unpacked_extension_of_both_roots_in_order(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    installed = tmp_path / "installed"
    for root, names in ((bundled, ("popups",)), (installed, ("ublock", "cookies", "not-an-extension"))):
        for name in names:
            (root / name).mkdir(parents=True)
            if name != "not-an-extension":
                (root / name / "manifest.json").write_text("{}")
    assert extension_paths(bundled, installed) == (
        str(bundled / "popups"),
        str(installed / "cookies"),
        str(installed / "ublock"),
    )
    assert extension_paths(tmp_path / "missing", tmp_path / "also-missing") == ()


def test_the_sandbox_is_off_as_root_or_when_forced() -> None:
    assert should_disable_sandbox(is_forced=False, geteuid=lambda: 0) is True
    assert should_disable_sandbox(is_forced=False, geteuid=lambda: 1000) is False
    assert should_disable_sandbox(is_forced=True, geteuid=lambda: 1000) is True


def test_a_sandboxed_launch_that_fails_is_retried_once_without_the_sandbox(tmp_path: Path) -> None:
    # A stand-in Chromium that records its argv and exits at once, so both attempts fail and both are seen.
    record = tmp_path / "argv.log"
    executable = tmp_path / "chromium"
    executable.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{record}"\nexit 3\n')
    executable.chmod(0o755)
    launcher = SubprocessChromiumLauncher(pause=threading.Event())
    request = ChromiumLaunchRequest(
        executable=executable,
        profile_dir=tmp_path / "profile",
        start_url="about:blank",
        extensions=(),
        environment={"PATH": "/usr/bin:/bin"},
        is_sandbox_disabled=False,
    )
    with pytest.raises(ChromiumStartError, match="exited at once"):
        launcher.launch(request)
    attempts = record.read_text().splitlines()
    assert len(attempts) == 2
    assert "--no-sandbox" not in attempts[0]
    assert "--no-sandbox" in attempts[1]
    # With the sandbox already off there is nothing to retry with.
    record.unlink()
    with pytest.raises(ChromiumStartError):
        launcher.launch(request.model_copy(update={"is_sandbox_disabled": True}))
    assert len(record.read_text().splitlines()) == 1


def test_a_missing_executable_is_refused_before_anything_is_spawned(tmp_path: Path) -> None:
    launcher = SubprocessChromiumLauncher()
    request = ChromiumLaunchRequest(
        executable=tmp_path / "nowhere",
        profile_dir=tmp_path / "profile",
        start_url="about:blank",
        environment={},
        is_sandbox_disabled=True,
    )
    with pytest.raises(ChromiumStartError, match="not found"):
        launcher.launch(request)
