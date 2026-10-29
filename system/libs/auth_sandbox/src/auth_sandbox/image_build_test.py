import re
from pathlib import Path

import pytest

from auth_sandbox.errors import ImageBuildError
from auth_sandbox.image_build import (
    BOOT_SCRIPT_SOURCE,
    COMMAND_NAMES,
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
    assert commands[3] == 'test "$(latchkey --version)" = 3.19.3'
    assert "import auth_sandbox.cli, imbue.mngr_latchkey.core" in commands[4]
    install = commands[5]
    assert (
        f"install -m 0755 /home/user/workspace/{BOOT_SCRIPT_SOURCE} /opt/auth-sandbox/boot.sh"
        in install
    )
    assert (
        f"install -m 0644 /home/user/workspace/{SUPERVISORD_CONF_SOURCE} /opt/auth-sandbox/supervisord.conf"
        in install
    )
    assert "bash -n /opt/auth-sandbox/boot.sh" in install
    for name in COMMAND_NAMES:
        assert (
            f"ln -sfn /home/user/workspace/.venv/bin/{name} /usr/local/bin/{name}"
            in install
        )
    assert commands[6].endswith(
        f"auth-sandbox-render-config --browser-executable {FORTRESS_LAUNCHER_PATH} /opt/auth-sandbox/config.json"
    )
    assert "latchkey ensure-browser --source existing-config" in commands[7]
    assert "LATCHKEY_ENCRYPTION_KEY=" in commands[7]
    assert len(commands) == 8


def test_build_steps_install_files_this_library_ships() -> None:
    for source in (BOOT_SCRIPT_SOURCE, SUPERVISORD_CONF_SOURCE):
        assert (_REPO_ROOT / source).is_file(), source


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
