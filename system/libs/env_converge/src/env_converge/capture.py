"""Capture actual installed-package state from the systems' own databases.

The capture philosophy: for anything with a real package database, captured
state IS the manifest -- dpkg for apt, npm's global tree for npm, uv's tool
list for uv. No wrapper scripts, no intent files, no agent cooperation
required: the apt Post-Invoke hook and boot-time probes read what is actually
installed.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from imbue.imbue_common.pure import pure
from loguru import logger

from env_converge.data_types import (
    AptState,
    BaseIdentity,
    CargoState,
    NpmGlobalState,
    UvToolState,
)
from env_converge.record import EnvConvergeError

_COMMAND_TIMEOUT_SECONDS = 60.0


class CaptureCommandError(EnvConvergeError, RuntimeError):
    """Raised when a capture probe command fails."""

    def __init__(self, command: str, detail: str) -> None:
        super().__init__(f"Capture probe failed ({command}): {detail}")


def _run_capture_command(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CaptureCommandError(" ".join(command), str(e)) from e
    if result.returncode != 0:
        raise CaptureCommandError(" ".join(command), result.stderr.strip()[:500])
    return result.stdout


@pure
def parse_dpkg_versions(dpkg_output: str) -> dict[str, str]:
    """Parse `dpkg-query -W -f '${Package}\\t${Version}\\n'` output."""
    version_by_package: dict[str, str] = {}
    for line in dpkg_output.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] and parts[1]:
            version_by_package[parts[0]] = parts[1]
    return version_by_package


@pure
def parse_manual_packages(apt_mark_output: str) -> tuple[str, ...]:
    """Parse `apt-mark showmanual` output into the sorted manual install-set."""
    return tuple(
        sorted(line.strip() for line in apt_mark_output.splitlines() if line.strip())
    )


@pure
def parse_npm_global_versions(npm_ls_json: str) -> dict[str, str]:
    """Parse `npm ls -g --json --depth=0` output into name -> version."""
    parsed = json.loads(npm_ls_json)
    dependencies = parsed.get("dependencies", {})
    version_by_package: dict[str, str] = {}
    for name, info in dependencies.items():
        version = info.get("version") if isinstance(info, dict) else None
        if isinstance(version, str) and version:
            version_by_package[name] = version
    return version_by_package


@pure
def parse_uv_tool_versions(uv_tool_list_output: str) -> dict[str, str]:
    """Parse `uv tool list` output (tool lines are 'name vX.Y.Z'; entry points are indented)."""
    version_by_tool: dict[str, str] = {}
    for line in uv_tool_list_output.splitlines():
        if line.startswith((" ", "-")) or not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("v"):
            version_by_tool[parts[0]] = parts[1].removeprefix("v")
    return version_by_tool


def capture_apt_state() -> AptState:
    dpkg_output = _run_capture_command(
        ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"]
    )
    manual_output = _run_capture_command(["apt-mark", "showmanual"])
    return AptState(
        manual_packages=parse_manual_packages(manual_output),
        version_by_package=parse_dpkg_versions(dpkg_output),
        recorded_at=datetime.now(timezone.utc),
    )


def capture_npm_state() -> NpmGlobalState:
    # npm exits nonzero on peer-dep warnings while still printing valid JSON,
    # so parse whatever it produced and only fail on unparseable output.
    try:
        output = _run_capture_command(["npm", "ls", "-g", "--json", "--depth=0"])
    except CaptureCommandError:
        logger.debug("npm ls exited nonzero; retrying without check")
        result = subprocess.run(
            ["npm", "ls", "-g", "--json", "--depth=0"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        output = result.stdout
    return NpmGlobalState(
        version_by_package=parse_npm_global_versions(output or "{}"),
        recorded_at=datetime.now(timezone.utc),
    )


def capture_uv_tool_state() -> UvToolState:
    output = _run_capture_command(["uv", "tool", "list"])
    return UvToolState(
        version_by_tool=parse_uv_tool_versions(output),
        recorded_at=datetime.now(timezone.utc),
    )


# `cargo install --list` top-level lines: `name vX.Y.Z:` for registry crates,
# `name vX.Y.Z (/path or git url):` for path/git installs; installed binaries
# follow on indented lines.
_CARGO_INSTALL_LINE = re.compile(r"^(?P<name>\S+) v(?P<version>\S+):$")


@pure
def parse_cargo_install_list(cargo_install_list_output: str) -> dict[str, str]:
    """Parse `cargo install --list` output into crate -> version.

    Registry crates only: path/git installs (their lines carry a source suffix
    before the colon) cannot be replayed from crates.io and are skipped.
    """
    version_by_crate: dict[str, str] = {}
    for line in cargo_install_list_output.splitlines():
        if line.startswith((" ", "\t")):
            continue
        match = _CARGO_INSTALL_LINE.match(line.strip())
        if match is not None:
            version_by_crate[match.group("name")] = match.group("version")
    return version_by_crate


@pure
def parse_rustup_toolchain_list(
    rustup_list_output: str,
) -> tuple[tuple[str, ...], str | None]:
    """Parse `rustup toolchain list` output into (toolchains, default).

    Lines are `<name>` optionally followed by a parenthesized marker -- rustup
    emits `(default)` historically and `(active, default)` since 1.28.
    """
    toolchains: list[str] = []
    default: str | None = None
    for line in rustup_list_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        name = stripped.split()[0]
        toolchains.append(name)
        if "default" in stripped[len(name) :]:
            default = name
    return tuple(toolchains), default


def resolve_cargo_binary(home_dir: Path = Path("/home/user")) -> str | None:
    """Locate cargo: PATH first, then the conventional ~/.cargo/bin.

    The explicit fallback matters because rustup wires PATH via ~/.bashrc
    (`. ~/.cargo/env`), which service processes never source -- and after a
    backup restore, ~/.cargo/bin exists before any shell profile ran.
    """
    found = shutil.which("cargo")
    if found is not None:
        return found
    candidate = home_dir / ".cargo" / "bin" / "cargo"
    if candidate.is_file():
        return str(candidate)
    return None


def resolve_rustup_binary(home_dir: Path = Path("/home/user")) -> str | None:
    """Locate rustup, with the same ~/.cargo/bin fallback as cargo."""
    found = shutil.which("rustup")
    if found is not None:
        return found
    candidate = home_dir / ".cargo" / "bin" / "rustup"
    if candidate.is_file():
        return str(candidate)
    return None


def capture_cargo_state() -> CargoState:
    """Capture cargo/rustup state; rust being absent yields an empty state.

    Unlike npm/uv (always in the base image), rust is agent-installed, so a
    missing cargo is normal -- captured as empty rather than raising, which
    also makes a deliberate rust removal stick under capture-first ordering.
    """
    cargo = resolve_cargo_binary()
    version_by_crate: dict[str, str] = {}
    if cargo is not None:
        version_by_crate = parse_cargo_install_list(
            _run_capture_command([cargo, "install", "--list"])
        )
    rustup = resolve_rustup_binary()
    toolchains: tuple[str, ...] = ()
    default_toolchain: str | None = None
    if rustup is not None:
        toolchains, default_toolchain = parse_rustup_toolchain_list(
            _run_capture_command([rustup, "toolchain", "list"])
        )
    return CargoState(
        version_by_crate=version_by_crate,
        toolchains=toolchains,
        default_toolchain=default_toolchain,
        recorded_at=datetime.now(timezone.utc),
    )


def capture_base_identity(
    snapshot_timestamp: str, workspace_dir: Path | None
) -> BaseIdentity:
    architecture = _run_capture_command(["dpkg", "--print-architecture"]).strip()
    template_commit: str | None = None
    if workspace_dir is not None and (workspace_dir / ".git").exists():
        try:
            template_commit = _run_capture_command(
                ["git", "-C", str(workspace_dir), "rev-parse", "HEAD"]
            ).strip()
        except CaptureCommandError as e:
            logger.debug("Skipping template commit capture: {}", e)
    return BaseIdentity(
        snapshot_timestamp=snapshot_timestamp,
        architecture=architecture,
        template_commit=template_commit,
        recorded_at=datetime.now(timezone.utc),
    )
