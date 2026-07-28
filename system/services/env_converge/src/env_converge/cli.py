"""CLI entry points: `uv run env-converge run | capture | upgrade | status`."""

import json
import sys

import click
from loguru import logger

from env_converge.converge import (
    DEFAULT_OVERLAY_DIR,
    DEFAULT_WORKSPACE_DIR,
    capture_all,
    read_pinned_snapshot_timestamp,
    run_fast_phase,
    run_slow_phase,
)
from env_converge.record import (
    default_record_dir,
    is_rootfs_stamped,
    read_apt_state,
    read_base_identity,
    read_cargo_state,
    read_npm_state,
    read_uv_tool_state,
)
from env_converge.upgrade import run_upgrade


@click.group()
def main() -> None:
    """Environment record + convergence for default-workspace-template hosts."""


@main.command()
@click.option(
    "--phase",
    type=click.Choice(["fast", "slow", "all"]),
    default="all",
    help="fast = overlay symlinks only (pre-services); slow = units + record replay",
)
def run(phase: str) -> None:
    """Converge this rootfs: overlay symlinks, env.d units, record replay."""
    workspace_dir = DEFAULT_WORKSPACE_DIR
    overlay_dir = DEFAULT_OVERLAY_DIR
    if phase in ("fast", "all"):
        overlay_results = run_fast_phase(workspace_dir, overlay_dir)
        logger.info("Applied {} overlay entries", len(overlay_results))
    if phase in ("slow", "all"):
        result = run_slow_phase(default_record_dir(), workspace_dir, overlay_dir)
        logger.info(
            "Converged: {} units, {} installs, {} unavailable",
            len(result.unit_results),
            len(result.installed_apt_packages)
            + len(result.installed_npm_packages)
            + len(result.installed_uv_tools)
            + len(result.installed_cargo_crates),
            len(result.unavailable_packages),
        )
        if result.unavailable_packages:
            sys.exit(3)


@main.command()
def capture() -> None:
    """Re-capture actual installed state into the record."""
    workspace_dir = DEFAULT_WORKSPACE_DIR
    capture_all(
        default_record_dir(),
        read_pinned_snapshot_timestamp(workspace_dir),
        workspace_dir,
    )
    logger.info("Captured environment state into {}", default_record_dir())


@main.command()
def upgrade() -> None:
    """Advance to the repo's committed snapshot timestamp (apt full-upgrade + re-converge)."""
    deltas = run_upgrade(
        default_record_dir(), DEFAULT_WORKSPACE_DIR, DEFAULT_OVERLAY_DIR
    )
    click.echo(json.dumps({"changed_count": len(deltas), "deltas": deltas}, indent=2))


@main.command()
def status() -> None:
    """Print the record vs reality summary as JSON."""
    record_dir = default_record_dir()
    base = read_base_identity(record_dir)
    apt_state = read_apt_state(record_dir)
    npm_state = read_npm_state(record_dir)
    uv_state = read_uv_tool_state(record_dir)
    cargo_state = read_cargo_state(record_dir)
    committed_timestamp = read_pinned_snapshot_timestamp(DEFAULT_WORKSPACE_DIR)
    summary = {
        "record_dir": str(record_dir),
        "is_rootfs_stamped": is_rootfs_stamped(),
        "recorded_snapshot_timestamp": base.snapshot_timestamp
        if base is not None
        else None,
        "committed_snapshot_timestamp": committed_timestamp,
        "is_upgrade_pending": base is not None
        and base.snapshot_timestamp != committed_timestamp,
        "recorded_manual_apt_count": len(apt_state.manual_packages)
        if apt_state is not None
        else None,
        "recorded_npm_global_count": len(npm_state.version_by_package)
        if npm_state is not None
        else None,
        "recorded_uv_tool_count": len(uv_state.version_by_tool)
        if uv_state is not None
        else None,
        "recorded_cargo_crate_count": len(cargo_state.version_by_crate)
        if cargo_state is not None
        else None,
        "recorded_rust_default_toolchain": cargo_state.default_toolchain
        if cargo_state is not None
        else None,
    }
    click.echo(json.dumps(summary, indent=2))
