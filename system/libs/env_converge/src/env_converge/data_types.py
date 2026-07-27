"""Frozen models for the environment record and convergence results."""

from datetime import datetime

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field


class BaseIdentity(FrozenModel):
    """Which base environment the record was captured against."""

    snapshot_timestamp: str = Field(
        description="apt archive snapshot timestamp (YYYYMMDDTHHMMSSZ) the environment is pinned to"
    )
    architecture: str = Field(description="dpkg architecture (e.g. amd64, arm64)")
    template_commit: str | None = Field(
        default=None,
        description="Workspace repo commit the environment was last converged from, when known",
    )
    recorded_at: datetime = Field(description="When this identity was recorded (UTC)")


class AptState(FrozenModel):
    """Captured apt package state (from dpkg's own database)."""

    manual_packages: tuple[str, ...] = Field(
        description="apt-mark showmanual set: the install-set converge replays (deps follow via apt)"
    )
    version_by_package: dict[str, str] = Field(
        description="Every installed package's version (forensic cross-check; not the replay input)"
    )
    recorded_at: datetime = Field(description="When this state was captured (UTC)")


class NpmGlobalState(FrozenModel):
    """Captured npm --global package state."""

    version_by_package: dict[str, str] = Field(
        description="Globally-installed npm packages and versions"
    )
    recorded_at: datetime = Field(description="When this state was captured (UTC)")


class UvToolState(FrozenModel):
    """Captured `uv tool` state."""

    version_by_tool: dict[str, str] = Field(
        description="uv-installed tools and versions"
    )
    recorded_at: datetime = Field(description="When this state was captured (UTC)")


class CargoState(FrozenModel):
    """Captured cargo/rustup state.

    Empty collections when rust is not installed (rust is not in the base
    image; agents add it ad hoc). Registry crates only: path/git installs
    cannot be replayed from crates.io and are deliberately not recorded.
    """

    version_by_crate: dict[str, str] = Field(
        description="cargo-installed registry crates and versions (from `cargo install --list`)"
    )
    toolchains: tuple[str, ...] = Field(
        description="rustup toolchains present (full names)"
    )
    default_toolchain: str | None = Field(
        default=None, description="rustup default toolchain, when one is configured"
    )
    recorded_at: datetime = Field(description="When this state was captured (UTC)")


class UnitRunResult(FrozenModel):
    """Outcome of one env.d unit script run."""

    unit_name: str = Field(
        description="Script file name (e.g. 1000-playwright-fortress.sh)"
    )
    exit_code: int = Field(description="The unit's exit code (0 = satisfied/installed)")
    duration_seconds: float = Field(description="Wall-clock run time")


class OverlayApplyResult(FrozenModel):
    """Outcome of applying one overlay-paths entry."""

    absolute_path: str = Field(
        description="The rootfs path that now symlinks into the overlay"
    )
    is_adopted: bool = Field(
        description="Whether pre-existing rootfs content was moved into the overlay"
    )


class ConvergeResult(FrozenModel):
    """Outcome of a converge pass (fast and/or slow phase)."""

    overlay_results: tuple[OverlayApplyResult, ...] = Field(
        description="Overlay entries applied"
    )
    unit_results: tuple[UnitRunResult, ...] = Field(
        description="env.d units run, in order"
    )
    installed_apt_packages: tuple[str, ...] = Field(
        description="Recorded apt packages installed this pass"
    )
    installed_npm_packages: tuple[str, ...] = Field(
        description="Recorded npm globals installed this pass"
    )
    installed_uv_tools: tuple[str, ...] = Field(
        description="Recorded uv tools installed this pass"
    )
    installed_cargo_crates: tuple[str, ...] = Field(
        description="Recorded cargo crates installed this pass"
    )
    unavailable_packages: tuple[str, ...] = Field(
        description="Recorded packages that could not be installed (package_unavailable events)"
    )
    is_fresh_rootfs: bool = Field(
        description="Whether this rootfs had no identity stamp (converge-first ordering)"
    )
