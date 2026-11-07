import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION

from auth_sandbox.errors import ImageBuildError
from auth_sandbox.image_build import (
    BOOT_SCRIPT_SOURCE,
    CLAIM_WATCHER_COMMAND,
    COMMAND_NAMES,
    LIBRARY_SOURCE_DIR,
    SIGN_IN_BROWSER_SOURCE,
    SIGN_IN_PACKAGES_CHECK,
    SUPERVISORD_CONF_SOURCE,
    ImageBuildSettings,
    build_steps,
    pinned_latchkey_version,
    run_build,
)
from auth_sandbox.mock_latchkey_test import FakeBuildSandbox
from auth_sandbox.primitives import FORTRESS_LAUNCHER_PATH

_REPO_ROOT = Path(__file__).resolve().parents[5]
_SETTINGS = ImageBuildSettings(latchkey_version="3.19.3")


def test_pinned_latchkey_version_reads_the_setup_scripts_pin() -> None:
    assert pinned_latchkey_version(': "${LATCHKEY_VERSION:=3.19.3}"\n') == "3.19.3"
    # The real pin moves with every latchkey bump, so only its shape is checked here.
    assert re.fullmatch(
        r"\d+\.\d+\.\d+",
        pinned_latchkey_version(
            (_REPO_ROOT / "system" / "scripts" / "setup_system.sh").read_text()
        ),
    )
    with pytest.raises(ImageBuildError):
        pinned_latchkey_version("nothing here")


def test_build_steps_seed_then_screen_then_the_sign_in_pieces_each_checked() -> None:
    commands = [step.command for step in build_steps(_SETTINGS)]
    assert commands[0] == "default-workspace-template-seed"
    assert "git checkout-index -f --stdin" in commands[1]
    assert commands[2].startswith(
        "ENV_CONVERGE_WORKSPACE_DIR=/home/user/workspace bash /home/user/workspace/system/scripts/env.d/2000-screen.sh"
    )
    assert f"test -x {FORTRESS_LAUNCHER_PATH}" in commands[2]
    # The version is read under a throwaway directory and key, inside the substitution that runs latchkey.
    assert commands[3] == (
        'd="$(mktemp -d)" && v="$(LATCHKEY_DIRECTORY="$d" LATCHKEY_ENCRYPTION_KEY="$(openssl rand -base64 32)"'
        ' LATCHKEY_DISABLE_COUNTING=1 latchkey --version)" && rm -rf "$d" && test "$v" = 3.19.3'
    )
    # The library comes from its own lock into its own environment, which no workspace venv carries.
    assert commands[4] == (
        "cd /home/user/workspace && UV_PROJECT_ENVIRONMENT=/opt/auth-sandbox/venv uv sync --locked --no-dev"
        f" --no-editable --python .venv/bin/python --project {LIBRARY_SOURCE_DIR}"
    )
    assert commands[5].startswith("/opt/auth-sandbox/venv/bin/python -c ")
    assert commands[5].endswith(" 3.19.3")
    install = commands[6]
    assert (
        f"install -m 0755 /home/user/workspace/{BOOT_SCRIPT_SOURCE} /opt/auth-sandbox/boot.sh"
        in install
    )
    assert (
        f"install -m 0644 /home/user/workspace/{SUPERVISORD_CONF_SOURCE} /opt/auth-sandbox/supervisord.conf"
        in install
    )
    assert (
        f"install -m 0755 /home/user/workspace/{SIGN_IN_BROWSER_SOURCE} /opt/auth-sandbox/sign-in-browser.sh"
        in install
    )
    assert "bash -n /opt/auth-sandbox/boot.sh" in install
    assert "bash -n /opt/auth-sandbox/sign-in-browser.sh" in install
    assert f"test -x /opt/auth-sandbox/venv/bin/{CLAIM_WATCHER_COMMAND}" in install
    for name in COMMAND_NAMES:
        assert (
            f"ln -sfn /opt/auth-sandbox/venv/bin/{name} /usr/local/bin/{name}"
            in install
        )
    assert commands[7] == (
        "/opt/auth-sandbox/venv/bin/auth-sandbox-render-config"
        " --browser-executable /opt/auth-sandbox/sign-in-browser.sh /opt/auth-sandbox/config.json"
    )
    assert "latchkey ensure-browser --source existing-config" in commands[8]
    assert "LATCHKEY_ENCRYPTION_KEY=" in commands[8]
    assert len(commands) == 9


def test_build_steps_install_files_this_library_ships() -> None:
    for source in (BOOT_SCRIPT_SOURCE, SIGN_IN_BROWSER_SOURCE, SUPERVISORD_CONF_SOURCE):
        assert (_REPO_ROOT / source).is_file(), source
    assert (_REPO_ROOT / LIBRARY_SOURCE_DIR / "uv.lock").is_file()


def test_the_linked_commands_and_the_claim_watcher_are_this_librarys_console_scripts() -> (
    None
):
    scripts = tomllib.loads(
        (_REPO_ROOT / LIBRARY_SOURCE_DIR / "pyproject.toml").read_text()
    )["project"]["scripts"]
    assert set(scripts) == {
        *COMMAND_NAMES,
        CLAIM_WATCHER_COMMAND,
        "auth-sandbox-render-config",
    }


def test_run_build_runs_every_step_in_order_then_snapshots() -> None:
    sandbox = FakeBuildSandbox(image_id="im-123")
    assert run_build(sandbox, _SETTINGS) == "im-123"
    assert sandbox.commands == [step.command for step in build_steps(_SETTINGS)]
    assert sandbox.snapshot_count == 1


def test_run_build_stops_at_the_first_failing_step_and_never_snapshots() -> None:
    sandbox = FakeBuildSandbox(failing_command_fragments=("latchkey --version",))
    with pytest.raises(
        ImageBuildError, match="check the image carries latchkey 3.19.3: exited 1"
    ):
        run_build(sandbox, _SETTINGS)
    assert len(sandbox.commands) == 4
    assert sandbox.snapshot_count == 0


def test_the_sign_in_packages_check_refuses_a_latchkey_below_the_floor_the_venv_enforces() -> (
    None
):
    def check(version: str) -> int:
        return subprocess.run(
            [sys.executable, "-c", SIGN_IN_PACKAGES_CHECK, version], check=False
        ).returncode

    assert check(LATCHKEY_MIN_VERSION) == 0
    assert check("0.0.1") == 1
