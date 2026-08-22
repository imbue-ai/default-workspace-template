"""Age-based sweep of stale CI-tier slices left on the standing CI boxes.

A crashed release run can leave slices on a CI box that no surviving env will
ever tear down (the per-run env's DB -- and with it the rows `minds-admin env
destroy` would have walked -- is destroyed with the env). Left alone they eat
box slots forever. This sweep reads each box's real lima resources over SSH and
destroys every slice whose stamped owner belongs to the ``ci`` tier and whose
on-box age exceeds the staleness threshold -- old enough that no live
(serialized) release run can still be using it. Non-CI owners are never
touched; finding one on a CI box is tier contamination and is reported loudly
instead.

Run from the bake-stage prologue (so a wedged prior run cannot cause spurious
capacity failures) and from the release teardown job as the crash backstop.
See specs/remote-workspaces-in-ci.md.
"""

from collections.abc import Mapping
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_imbue_cloud.primitives import CI_TIER
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_name_env_owner
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_vps.primitives import VpsInstanceId

# Matches the ci Modal-env sweep's staleness threshold: release runs are
# serialized and far shorter than this, so anything older is certainly leaked.
DEFAULT_CI_SLICE_MAX_AGE_HOURS: Final[float] = 4.0

_LIST_AGES_TIMEOUT_SECONDS: Final[float] = 60.0
_NOW_MARKER: Final[str] = "MNGR_SWEEP_NOW"


class CiSliceSweepBoxReport(FrozenModel):
    """What the sweep did on one box."""

    server_id: str = Field(description="The bare_metal_servers row id of the swept box")
    public_address: str = Field(description="The box address the sweep reached")
    swept_instances: tuple[str, ...] = Field(description="Stale CI slice VMs destroyed, sorted")
    swept_disks: tuple[str, ...] = Field(description="Stale CI slice data disks destroyed (beyond the VMs'), sorted")
    kept_ci_slices: tuple[str, ...] = Field(description="CI-owned slices younger than the threshold, kept, sorted")
    foreign_slices: tuple[str, ...] = Field(
        description="Slice resources on the box owned by a non-ci tier -- tier contamination, never touched, sorted"
    )
    failed: tuple[str, ...] = Field(description="Resources whose destroy failed (retried on the next sweep), sorted")


class CiSliceSweepReport(FrozenModel):
    """The summary ``minds-admin server sweep-ci-slices`` emits."""

    max_age_hours: float = Field(description="Staleness threshold the sweep applied")
    boxes: tuple[CiSliceSweepBoxReport, ...] = Field(description="Per-box outcomes, in fleet-table order")
    unreachable_boxes: tuple[str, ...] = Field(description="Server ids whose box could not be read, sorted")


@pure
def compute_stale_ci_slice_names(
    age_seconds_by_name: Mapping[str, float],
    max_age_seconds: float,
) -> tuple[set[str], set[str], set[str]]:
    """Split slice resource names into (stale-ci, young-ci, foreign-tier) sets.

    Names without a stamped owner (legacy slices, non-slice lima resources) are
    ignored entirely: they cannot be attributed, so an age-based sweep must not
    touch them.
    """
    stale_ci: set[str] = set()
    young_ci: set[str] = set()
    foreign: set[str] = set()
    for name, age_seconds in age_seconds_by_name.items():
        owner = slice_name_env_owner(name)
        if owner is None:
            continue
        if tier_for_env_name(owner) != CI_TIER:
            foreign.add(name)
        elif age_seconds > max_age_seconds:
            stale_ci.add(name)
        else:
            young_ci.add(name)
    return stale_ci, young_ci, foreign


@pure
def parse_slice_resource_ages(stat_output: str) -> tuple[dict[str, float], dict[str, float]]:
    """Parse the on-box age listing into (instance ages, disk ages) keyed by resource name.

    The listing is the output of :func:`_build_age_listing_command`: a
    ``MNGR_SWEEP_NOW <epoch>`` line followed by ``<mtime-epoch> <path>`` lines
    for every lima instance's ``lima.yaml`` and every ``_disks`` entry. Ages are
    computed against the box's own clock so runner clock skew cannot mislead
    the staleness check. Unparseable lines are skipped with a warning.
    """
    now_epoch: float | None = None
    instance_ages: dict[str, float] = {}
    disk_ages: dict[str, float] = {}
    for line in stat_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2:
            logger.warning("Skipping unparseable slice-age line: {!r}", stripped)
            continue
        first, rest = parts
        if first == _NOW_MARKER:
            try:
                now_epoch = float(rest)
            except ValueError:
                logger.warning("Skipping unparseable sweep NOW line: {!r}", stripped)
            continue
        try:
            mtime_epoch = float(first)
        except ValueError:
            logger.warning("Skipping unparseable slice-age line: {!r}", stripped)
            continue
        if now_epoch is None:
            logger.warning("Ignoring slice-age line before the NOW marker: {!r}", stripped)
            continue
        age_seconds = max(0.0, now_epoch - mtime_epoch)
        path_parts = rest.rstrip("/").split("/")
        if "_disks" in path_parts:
            disk_ages[path_parts[-1]] = age_seconds
        elif path_parts[-1] == "lima.yaml" and len(path_parts) >= 2:
            instance_ages[path_parts[-2]] = age_seconds
        else:
            logger.warning("Skipping slice-age line with unrecognized path shape: {!r}", stripped)
    return instance_ages, disk_ages


def _build_age_listing_command() -> str:
    # `stat` on globs that match nothing exits non-zero with empty stdout; the
    # trailing `true` keeps the composite command's exit clean either way. The
    # box's own `date +%s` anchors the age computation to its clock.
    return (
        f'echo "{_NOW_MARKER} $(date +%s)"; '
        'stat -c "%Y %n" "$HOME"/.lima/*/lima.yaml 2>/dev/null; '
        'stat -c "%Y %n" "$HOME"/.lima/_disks/* 2>/dev/null; '
        "true"
    )


def sweep_ci_slices_on_box(
    client: LimaSliceVpsClient,
    *,
    server_id: str,
    public_address: str,
    max_age_seconds: float,
) -> CiSliceSweepBoxReport:
    """Destroy every stale CI-owned slice VM (then orphan disk) on one box.

    Raises ``MngrError`` (from the client) when the box cannot be read at all;
    individual destroy failures are collected into the report instead, so one
    wedged resource never hides the rest of the sweep.
    """
    return_code, stdout, stderr = client.run_on_box(
        _build_age_listing_command(), timeout=_LIST_AGES_TIMEOUT_SECONDS, label="ci-sweep-ages"
    )
    if return_code != 0:
        raise MngrError(
            f"could not list slice resource ages on {public_address} (exit {return_code}): {stderr.strip()}"
        )
    # Only the instance ages matter here; disks are re-listed (and swept) after
    # the VM sweep below, so the first listing's disk ages are discarded.
    instance_ages, _disk_ages_before_vm_sweep = parse_slice_resource_ages(stdout)

    stale_instances, young_instances, foreign_instances = compute_stale_ci_slice_names(instance_ages, max_age_seconds)
    failed: set[str] = set()
    for instance_name in sorted(stale_instances):
        logger.info("CI slice sweep: destroying stale slice VM {} on {}", instance_name, public_address)
        try:
            client.destroy_instance(VpsInstanceId(instance_name))
        except (MngrError, OSError) as exc:
            logger.warning("CI slice sweep: failed to destroy VM {} on {}: {}", instance_name, public_address, exc)
            failed.add(instance_name)

    # Disks second, re-listed so disks just removed with their VM are gone; a disk
    # that outlived its VM (or whose VM destroy failed to remove it) is reaped here.
    disk_return_code, disk_stdout, disk_stderr = client.run_on_box(
        _build_age_listing_command(), timeout=_LIST_AGES_TIMEOUT_SECONDS, label="ci-sweep-disk-ages"
    )
    if disk_return_code != 0:
        # Raise rather than silently skipping the disk sweep: the CLI records the
        # box as not fully swept and exits non-zero, so the next sweep retries
        # (the VM destroys above are idempotent).
        raise MngrError(
            f"could not re-list slice disk ages on {public_address} (exit {disk_return_code}): {disk_stderr.strip()}"
        )
    _instances_after, disks_after = parse_slice_resource_ages(disk_stdout)
    stale_disks, young_disks, foreign_disks = compute_stale_ci_slice_names(disks_after, max_age_seconds)
    swept_disks: set[str] = set()
    for disk_name in sorted(stale_disks):
        logger.info("CI slice sweep: destroying stale slice disk {} on {}", disk_name, public_address)
        try:
            client.destroy_disk(disk_name)
            swept_disks.add(disk_name)
        except (MngrError, OSError) as exc:
            logger.warning("CI slice sweep: failed to destroy disk {} on {}: {}", disk_name, public_address, exc)
            failed.add(disk_name)

    foreign = foreign_instances | foreign_disks
    if foreign:
        logger.warning(
            "CI slice sweep: box {} carries non-ci-tier slice resources (tier contamination, NOT touched): {}",
            public_address,
            sorted(foreign),
        )
    return CiSliceSweepBoxReport(
        server_id=server_id,
        public_address=public_address,
        swept_instances=tuple(sorted(stale_instances - failed)),
        swept_disks=tuple(sorted(swept_disks)),
        kept_ci_slices=tuple(sorted(young_instances | young_disks)),
        foreign_slices=tuple(sorted(foreign)),
        failed=tuple(sorted(failed)),
    )
