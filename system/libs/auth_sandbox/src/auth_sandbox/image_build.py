"""The sandbox image's build steps (spec 5.1), as commands run inside a build sandbox made from the product image.

The steps are data over a small interface so they can be checked without Modal: ``auth_sandbox.
modal_image_build`` runs them in a real Modal sandbox and snapshots it; the tests run them over a fake.
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
    VENV_DIRNAME,
    WORKSPACE_ROOT,
)

# The commands owner-exec runs on the chrome's behalf, linked onto PATH from this library's environment.
COMMAND_NAMES: Final[tuple[str, ...]] = (
    "auth-sandbox-prepare",
    "auth-sandbox-sign-in",
    "auth-sandbox-set-credentials",
)
# The boot script runs it from this library's environment; owner-exec has no use for it, so it is not linked onto PATH.
CLAIM_WATCHER_COMMAND: Final[str] = "auth-sandbox-await-claim"
# This library's project, and where it keeps the boot script and the sandbox's supervisord config, relative to the
# workspace root.
LIBRARY_SOURCE_DIR: Final[Path] = Path("system/libs/auth_sandbox")
SANDBOX_SOURCE_DIR: Final[Path] = LIBRARY_SOURCE_DIR / "sandbox"
BOOT_SCRIPT_SOURCE: Final[Path] = SANDBOX_SOURCE_DIR / "boot.sh"
SUPERVISORD_CONF_SOURCE: Final[Path] = SANDBOX_SOURCE_DIR / "supervisord.conf"
# The pin ``system/scripts/setup_system.sh`` installs, in the shape ``: "${LATCHKEY_VERSION:=3.19.3}"``.
_LATCHKEY_VERSION_PIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$\{LATCHKEY_VERSION:=([0-9][0-9.]*)\}"
)
_SYMLINK_LIST_PATH: Final[str] = "/tmp/auth-sandbox-symlinks"
# Run by the library environment's python with the pinned version as its argument: the sign-in packages import, and
# the pinned latchkey meets the floor its imbue-mngr-latchkey enforces on every run (below it, every sign-in in the
# image would refuse to start while the build itself succeeded).
SIGN_IN_PACKAGES_CHECK: Final[str] = (
    "import sys, auth_sandbox.cli; from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION; "
    "parse = lambda version: tuple(int(part) for part in version.split('.')); "
    "sys.exit(0 if parse(LATCHKEY_MIN_VERSION) <= parse(sys.argv[1]) else 1)"
)
# The environment a latchkey invocation needs to run at all in the image: the CLI refuses to start
# without an encryption key (there is no system keychain in a sandbox), and the usage ping stays off.
# Every build-time invocation points at a throwaway directory, so nothing of it lands in the image.
_OFFLINE_LATCHKEY_ENVIRONMENT: Final[str] = (
    'LATCHKEY_DIRECTORY="$d" LATCHKEY_ENCRYPTION_KEY="$(openssl rand -base64 32)" LATCHKEY_DISABLE_COUNTING=1'
)

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
        default=INSTALL_DIR,
        description="Where the boot script, its files and the library's environment land",
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
    venv_dir = settings.install_dir / VENV_DIRNAME
    venv_bin = shlex.quote(str(venv_dir / "bin"))
    fortress = shlex.quote(str(settings.fortress_launcher))
    boot_script = shlex.quote(str(settings.install_dir / BOOT_SCRIPT_PATH.name))
    supervisord_conf = shlex.quote(
        str(settings.install_dir / SUPERVISORD_CONF_PATH.name)
    )
    config = shlex.quote(str(settings.install_dir / TEMPLATE_CONFIG_PATH.name))
    link_commands = " && ".join(
        f"ln -sfn {venv_bin}/{name} /usr/local/bin/{name}" for name in COMMAND_NAMES
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
            command=(
                f'd="$(mktemp -d)" && v="$({_OFFLINE_LATCHKEY_ENVIRONMENT} latchkey --version)" && rm -rf "$d"'
                f' && test "$v" = {shlex.quote(settings.latchkey_version)}'
            ),
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="install this library into its own environment from its lock, on the workspace venv's Python",
            command=(
                f"cd {root} && UV_PROJECT_ENVIRONMENT={shlex.quote(str(venv_dir))}"
                f" uv sync --locked --no-dev --no-editable --python .venv/bin/python --project {LIBRARY_SOURCE_DIR}"
            ),
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description=(
                f"check the sign-in packages import from the library's environment and accept latchkey"
                f" {settings.latchkey_version}"
            ),
            command=(
                f"{venv_bin}/python -c {shlex.quote(SIGN_IN_PACKAGES_CHECK)}"
                f" {shlex.quote(settings.latchkey_version)}"
            ),
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="install the boot script, the sandbox's supervisord config and the commands",
            command=(
                f"install -d {install_dir}"
                f" && install -m 0755 {root}/{BOOT_SCRIPT_SOURCE} {boot_script}"
                f" && install -m 0644 {root}/{SUPERVISORD_CONF_SOURCE} {supervisord_conf}"
                f" && bash -n {boot_script}"
                f" && test -x {venv_bin}/{CLAIM_WATCHER_COMMAND}"
                f" && {link_commands}"
            ),
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="render the template config.json with the Fortress browser entry",
            command=f"{venv_bin}/auth-sandbox-render-config --browser-executable {fortress} {config}",
            timeout_seconds=_QUICK_STEP_TIMEOUT_SECONDS,
        ),
        BuildStep(
            description="check latchkey accepts the template's browser entry as it stands",
            command=(
                f'd="$(mktemp -d)" && cp {config} "$d/config.json"'
                f" && {_OFFLINE_LATCHKEY_ENVIRONMENT} latchkey ensure-browser --source existing-config"
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
