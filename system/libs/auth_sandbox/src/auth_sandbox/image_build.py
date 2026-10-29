"""The sandbox image's build steps (spec 5.1), as commands run inside a build sandbox made from the product image.

The steps are data over a small interface so they can be checked without Modal: ``system/scripts/
build_auth_sandbox_image.py`` runs them in a real Modal sandbox and snapshots it; the tests run them over a fake.
Nothing user-specific is installed: no key, no token, no certificate, no authorized key.
"""

import re
import shlex
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from pydantic import Field

from auth_sandbox.errors import ImageBuildError
from auth_sandbox.primitives import (
    BOOT_SCRIPT_PATH,
    FORTRESS_LAUNCHER_PATH,
    INSTALL_DIR,
    SUPERVISORD_CONF_PATH,
    TEMPLATE_CONFIG_PATH,
    WORKSPACE_ROOT,
)

# The commands owner-exec runs on the chrome's behalf, linked onto PATH from the workspace venv's console scripts.
COMMAND_NAMES: Final[tuple[str, ...]] = (
    "auth-sandbox-sign-in",
    "auth-sandbox-set-credentials",
    "auth-sandbox-import-browser-state",
    "auth-sandbox-export",
)
# Where this library keeps the boot script and the sandbox's supervisord config, relative to the workspace root.
SANDBOX_SOURCE_DIR: Final[Path] = Path("system/libs/auth_sandbox/sandbox")
BOOT_SCRIPT_SOURCE: Final[Path] = SANDBOX_SOURCE_DIR / "boot.sh"
SUPERVISORD_CONF_SOURCE: Final[Path] = SANDBOX_SOURCE_DIR / "supervisord.conf"
# The pin ``system/scripts/setup_system.sh`` installs, in the shape ``: "${LATCHKEY_VERSION:=3.19.3}"``.
_LATCHKEY_VERSION_PIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$\{LATCHKEY_VERSION:=([0-9][0-9.]*)\}"
)
_SYMLINK_LIST_PATH: Final[str] = "/tmp/auth-sandbox-symlinks"

# Bounds per step: the Screen unit downloads the stack and Fortress (hundreds of MB); everything else is quick.
_SEED_TIMEOUT_SECONDS: Final[int] = 300
_SCREEN_UNIT_TIMEOUT_SECONDS: Final[int] = 1800
_QUICK_STEP_TIMEOUT_SECONDS: Final[int] = 300
DEFAULT_SNAPSHOT_TIMEOUT_SECONDS: Final[int] = 900


class BuildSandboxInterface(MutableModel, ABC):
    """A sandbox the build runs its steps in and then snapshots."""

    @abstractmethod
    def run(self, command: str, timeout_seconds: int) -> int:
        """Run a shell command to completion and answer its exit code; its output is the implementation's to show."""

    @abstractmethod
    def snapshot(self, timeout_seconds: int) -> str:
        """Snapshot the filesystem and answer the image id."""


class BuildStep(FrozenModel):
    """One command of the build, with the reason it runs."""

    description: str = Field(description="What the step does, for the build log")
    command: str = Field(
        description="The shell command, run with bash -lc inside the sandbox"
    )
    timeout_seconds: int = Field(description="How long the step may take")


class ImageBuildSettings(FrozenModel):
    """What the steps are rendered from."""

    latchkey_version: str = Field(
        description="The latchkey CLI version the image must carry"
    )
    workspace_root: Path = Field(
        default=WORKSPACE_ROOT, description="Where the seed puts the template tree"
    )
    install_dir: Path = Field(
        default=INSTALL_DIR, description="Where the boot script and its files land"
    )
    fortress_launcher: Path = Field(
        default=FORTRESS_LAUNCHER_PATH, description="The browser latchkey launches"
    )
    snapshot_timeout_seconds: int = Field(
        default=DEFAULT_SNAPSHOT_TIMEOUT_SECONDS, description="The snapshot's bound"
    )


@pure
def pinned_latchkey_version(setup_system_text: str) -> str:
    """The latchkey version ``setup_system.sh`` pins, so the build checks the image carries exactly it."""
    match = _LATCHKEY_VERSION_PIN_PATTERN.search(setup_system_text)
    if match is None:
        raise ImageBuildError("setup_system.sh names no LATCHKEY_VERSION pin")
    return match.group(1)


@pure
def build_steps(settings: ImageBuildSettings) -> tuple[BuildStep, ...]:
    """The build, in order: the first-boot seed and the Screen unit as the CI image does them, then what the
    sign-in needs on top, each checked as it lands."""
    root = shlex.quote(str(settings.workspace_root))
    install_dir = shlex.quote(str(settings.install_dir))
    fortress = shlex.quote(str(settings.fortress_launcher))
    boot_script = shlex.quote(str(settings.install_dir / BOOT_SCRIPT_PATH.name))
    supervisord_conf = shlex.quote(
        str(settings.install_dir / SUPERVISORD_CONF_PATH.name)
    )
    config = shlex.quote(str(settings.install_dir / TEMPLATE_CONFIG_PATH.name))
    link_commands = " && ".join(
        f"ln -sfn {root}/.venv/bin/{name} /usr/local/bin/{name}"
        for name in COMMAND_NAMES
    )
    return (
        BuildStep(
            description="seed the workspace tree onto the sandbox, as a workspace's first boot does",
            command="default-workspace-template-seed",
            timeout_seconds=_SEED_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="put back the symlinks the build context upload flattened",
            command=(
                f"cd {root} && git ls-files -s | awk -F'\\t' '$1 ~ /^120000/ {{ print $2 }}' > {_SYMLINK_LIST_PATH}"
                f" && git checkout-index -f --stdin < {_SYMLINK_LIST_PATH} && rm {_SYMLINK_LIST_PATH}"
            ),
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="install the Screen's stack and Fortress through the env.d unit",
            command=(
                f"ENV_CONVERGE_WORKSPACE_DIR={root} bash {root}/system/scripts/env.d/2000-screen.sh"
                f" && test -x {fortress}"
            ),
            timeout_seconds=_SCREEN_UNIT_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description=f"check the image carries latchkey {settings.latchkey_version}",
            command=f'test "$(latchkey --version)" = {shlex.quote(settings.latchkey_version)}',
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="check the sign-in packages import from the workspace venv",
            command=f"cd {root} && .venv/bin/python -c 'import auth_sandbox.cli, imbue.mngr_latchkey.core'",
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="install the boot script, the sandbox's supervisord config and the commands",
            command=(
                f"install -d {install_dir}"
                f" && install -m 0755 {root}/{BOOT_SCRIPT_SOURCE} {boot_script}"
                f" && install -m 0644 {root}/{SUPERVISORD_CONF_SOURCE} {supervisord_conf}"
                f" && bash -n {boot_script}"
                f" && {link_commands}"
            ),
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="render the template config.json with the Fortress browser entry",
            command=f"cd {root} && .venv/bin/auth-sandbox-render-config --browser-executable {fortress} {config}",
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="check latchkey accepts the template's browser entry as it stands",
            command=(
                f'd="$(mktemp -d)" && cp {config} "$d/config.json"'
                ' && LATCHKEY_DIRECTORY="$d" LATCHKEY_ENCRYPTION_KEY="$(openssl rand -base64 32)"'
                " LATCHKEY_DISABLE_COUNTING=1 latchkey ensure-browser --source existing-config"
                ' && rm -rf "$d"'
            ),
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
    )


def run_build(sandbox: BuildSandboxInterface, settings: ImageBuildSettings) -> str:
    """Run every step, stopping at the first failure, then snapshot; answers the image id."""
    for step in build_steps(settings):
        returncode = sandbox.run(step.command, step.timeout_seconds)
        if returncode != 0:
            raise ImageBuildError(f"{step.description}: exited {returncode}")
    return sandbox.snapshot(settings.snapshot_timeout_seconds)
