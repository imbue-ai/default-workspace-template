"""Age-based sweep of stale CI-tier slices left on the standing CI boxes.

A crashed release run can leave slices on a CI box that no surviving env will
ever tear down (the per-run env's DB -- and with it the rows `minds-admin env
destroy` would have walked -- is destroyed with the env). Left alone they eat
box slots forever. This sweep reads each box's real slice resources over SSH
(through the box generation's slice client, so gen-1 and gen-2 boxes alike)
and destroys every slice whose stamped owner belongs to the ``ci`` tier and
whose on-box age exceeds the staleness threshold -- old enough that no live
(serialized) release run can still be using it. After the VM sweep it also
reclaims every ``ci``-owned data disk whose VM is no longer on the box (leaked
by an earlier failed carve or teardown), whatever the disk's age; a disk whose
VM is still on the box is held by it and never touched. Non-CI owners are never
touched; finding one on a CI box is tier contamination and is reported loudly
instead.

Run from the bake-stage prologue (so a wedged prior run cannot cause spurious
capacity failures) and from the release teardown job as the crash backstop.
See specs/remote-workspaces-in-ci.md.
"""

from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.primitives import CI_TIER
from imbue.mngr_imbue_cloud.slices.bare_metal import CI_SLICE_MAX_AGE_SECONDS
from imbue.mngr_imbue_cloud.slices.bare_metal import compute_tier_orphan_disk_names
from imbue.mngr_imbue_cloud.slices.bare_metal import partition_slice_names_by_tier_and_age
from imbue.mngr_vps.primitives import VpsInstanceId

# The CLI default, in the hours the ``--max-age-hours`` flag speaks; the
# threshold itself lives beside the orphan reaper's guard in ``bare_metal.py``.
DEFAULT_CI_SLICE_MAX_AGE_HOURS: Final[float] = CI_SLICE_MAX_AGE_SECONDS / 3600.0


class CiSliceSweepBoxReport(FrozenModel):
    """What the sweep did on one box."""

    server_id: str = Field(description="The bare_metal_servers row id of the swept box")
    public_address: str = Field(description="The box address the sweep reached")
    swept_instances: tuple[str, ...] = Field(description="Stale CI slice VMs destroyed, sorted")
    swept_disks: tuple[str, ...] = Field(
        description="CI slice data disks whose VM was already gone (leaked by an earlier failure), destroyed, sorted"
    )
    kept_ci_slices: tuple[str, ...] = Field(description="CI-owned slice VMs younger than the threshold, kept, sorted")
    foreign_slices: tuple[str, ...] = Field(
        description="Slice resources on the box owned by a non-ci tier -- tier contamination, never touched, sorted"
    )
    failed: tuple[str, ...] = Field(description="Resources whose destroy failed (retried on the next sweep), sorted")


class CiSliceSweepReport(FrozenModel):
    """The summary ``minds-admin server sweep-ci-slices`` emits."""

    max_age_hours: float = Field(description="Staleness threshold the sweep applied")
    boxes: tuple[CiSliceSweepBoxReport, ...] = Field(description="Per-box outcomes, in fleet-table order")
    unreachable_boxes: tuple[str, ...] = Field(description="Server ids whose box could not be read, sorted")


def sweep_ci_slices_on_box(
    client: SliceVmClientInterface,
    *,
    server_id: str,
    public_address: str,
    max_age_seconds: float,
) -> CiSliceSweepBoxReport:
    """Destroy every stale CI-owned slice VM (then every orphaned CI disk) on one box.

    Raises ``MngrError`` (from the client) when the box cannot be read at all;
    individual destroy failures are collected into the report instead, so one
    wedged resource never hides the rest of the sweep.
    """
    observations = client.list_instance_observations()
    age_seconds_by_name = {observation.instance_name: observation.age_seconds for observation in observations}
    stale_instances, young_instances, foreign_instances = partition_slice_names_by_tier_and_age(
        age_seconds_by_name, CI_TIER, max_age_seconds
    )
    failed: set[str] = set()
    for instance_name in sorted(stale_instances):
        logger.info("CI slice sweep: destroying stale slice VM {} on {}", instance_name, public_address)
        try:
            client.destroy_instance(VpsInstanceId(instance_name))
        except (MngrError, OSError) as exc:
            logger.warning("CI slice sweep: failed to destroy VM {} on {}: {}", instance_name, public_address, exc)
            failed.add(instance_name)

    # Disks second: a VM destroy removes its own disk, so what is left is a disk
    # whose VM is gone -- leaked by an earlier failure -- unless its VM is still on
    # the box (young, foreign, or a destroy that just failed), in which case it is
    # held and never touched.
    held_instance_names = {observation.instance_name for observation in observations} - (stale_instances - failed)
    orphan_disks, foreign_disks = compute_tier_orphan_disk_names(
        client.list_disk_names(), CI_TIER, held_instance_names
    )
    swept_disks: set[str] = set()
    for disk_name in sorted(orphan_disks):
        logger.info("CI slice sweep: destroying orphaned slice disk {} on {}", disk_name, public_address)
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
        kept_ci_slices=tuple(sorted(young_instances)),
        foreign_slices=tuple(sorted(foreign)),
        failed=tuple(sorted(failed)),
    )
