"""Frozen records of the gen-1 -> gen-2 cutover (``minds-admin cutover``).

Every stage of the cutover writes one of these to the operator's state dir
(``~/.minds-<env>/cutover/``) so a re-run resumes exactly where the last one
stopped. The whole ``cutover`` command group is one-time tooling, deleted in
phase 6 of blueprint/slice-fleet-cutover.
"""

import re
from enum import auto
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DATA_DISK_BASE_GIB

# The oldest workspace version the cutover restores: older default-workspace-template
# tags predate the shape the gen-2 restore replays (the env-converge record, the
# minds_start_services_agent.sh entry the autostart runs).
MINIMUM_VERSION_TAG: Final[str] = "minds-v0.3.10"

# The shape of a default-workspace-template release tag (the same shape the
# workspace's own update-self skill parses).
_VERSION_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^minds-v(\d+)\.(\d+)\.(\d+)(?:-(?P<pre>.+))?$")

# The first release line that runs on gen-2 boxes: 0.6.x+ releases bake onto
# gen-2 and 0.5.x-and-older onto gen-1, so the release channels cohort each
# client version onto its generation through the exact-tag lease match.
FIRST_GEN2_RELEASE_TAG: Final[str] = "minds-v0.6.0"


class CutoverError(MindError):
    """Raised when a cutover stage cannot proceed for a workspace or a box."""


class CutoverStage(UpperCaseStrEnum):
    """How far one workspace has progressed through its migration (its state file's ``stage``)."""

    # Keys, inspect, and version read off the live workspace.
    HARVESTED = auto()
    # The product's gen-1 stop finished (verified artifact uploaded) and the
    # artifact pointers are saved to the state file for rollback.
    STOPPED = auto()
    # The row is parked (no placement, no manifest): user starts answer 409.
    PARKED = auto()
    # Restored on the gen-2 target box and re-leased at the new coordinates.
    RESTORED = auto()
    # Rolled back to gen-1 through the product's own restore.
    ROLLED_BACK = auto()
    FAILED = auto()


class CutoverBoxStage(UpperCaseStrEnum):
    """How far one box has progressed (its state file's ``stage``).

    ``FINISHED`` is not a persisted box stage: it is the migrate/rollback
    report's per-invocation terminal marker (their reports are shaped around
    the target box, which has no stage of its own).
    """

    REPAVED = auto()
    FINISHED = auto()


class RowVerdict(UpperCaseStrEnum):
    """What the preflight decided about one gen-1 pool row."""

    # A workspace the migrate can move (leased, or stopped -- the migrate
    # admin-starts a stopped one first).
    CANDIDATE = auto()
    # An unleased pool row: destroy it (``minds-admin pool destroy``) before
    # the box can be repaved.
    DESTROY = auto()
    # A row the migrate cannot take yet; the remedy names the operator action.
    REFUSED = auto()


class RowClassification(FrozenModel):
    """The preflight's verdict for one pool row, with the remedy for a refusal."""

    verdict: RowVerdict = Field(description="Whether the row is migrated, destroyed, or blocks the tier")
    remedy: str | None = Field(default=None, description="What the operator must do about a refused row")


class VersionTag(FrozenModel):
    """A parsed ``minds-v<major>.<minor>.<patch>[-<pre>]`` release tag."""

    major: int = Field(description="Major version")
    minor: int = Field(description="Minor version")
    patch: int = Field(description="Patch version")
    prerelease: str | None = Field(default=None, description="Prerelease suffix, when present")

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


class HarvestedKeys(FrozenModel):
    """The SSH trust material harvested from a running gen-1 workspace, replayed at restore."""

    vm_host_private_key: SecretStr = Field(description="The slice VM's live sshd host private key (PEM)")
    vm_host_public_key: str = Field(description="The slice VM's live sshd host public key (OpenSSH line)")
    vm_authorized_keys: str = Field(description="The VM root's authorized_keys file")
    container_host_private_key: SecretStr = Field(description="The container's sshd host private key (PEM)")
    container_host_public_key: str = Field(description="The container's sshd host public key (OpenSSH line)")
    container_authorized_keys: str = Field(description="The container root's authorized_keys file")


class ReplayedContainerFile(FrozenModel):
    """One file ``docker cp``'d into the recreated container's rootfs before it first starts."""

    container_path: str = Field(description="Absolute path inside the container")
    content: str = Field(description="The file's text")
    mode: str = Field(description="Octal mode string the staged copy is written with (docker cp keeps it)")


class TemplateReplayInputs(FrozenModel):
    """What the restore reads out of one default-workspace-template version to replay a container."""

    installer_commands: tuple[str, ...] = Field(
        description="The pool_host template's outer autostart installer block(s), run as root over VM SSH"
    )
    container_home_path: str = Field(
        description=(
            "The container home path symlinked onto the host volume's home subdirectory "
            "(the slice provider's volume_home_path setting)"
        )
    )


class SavedProductArtifact(FrozenModel):
    """The product gen-1 stop artifact a migrate saved for its transplant and for rollback.

    ``manifest_json`` is the row's ``artifact_manifest`` JSONB verbatim except
    for its ``key_prefix``, which points at the migrate's rollback copy of the
    three objects (the product deletes the previous generation's objects on
    the workspace's next stop, so the originals do not survive gen-2 use).
    """

    generation: int = Field(description="The artifact generation the product stop recorded")
    key_prefix: str = Field(description="Object key prefix of the rollback copy (DISK/DATADISK/META)")
    age_recipient: str = Field(description="The age recipient the objects are encrypted to")
    datadisk_sha256: str = Field(description="Ciphertext sha256 of the data-disk object")
    wrapped_dek: str = Field(description="The KEK-wrapped age identity, as the row recorded it")
    manifest_json: dict[str, Any] = Field(description="The artifact manifest, rewritten to the rollback copy")


class CutoverWorkspaceState(FrozenModel):
    """One workspace's migration record (``workspaces/<host_db_id>.json`` in the state dir)."""

    host_db_id: str = Field(description="pool_hosts row id")
    host_id: str = Field(description="mngr host id (host-<32hex>)")
    agent_id: str | None = Field(description="The workspace's services agent id")
    host_name: str = Field(description="The row's host name (the container name's suffix at bake)")
    leased_to_user: str | None = Field(description="Owning user's 16-hex prefix, when leased")
    origin_server_id: str = Field(description="The gen-1 box the workspace was migrated off")
    origin_public_address: str = Field(description="The origin box's public address")
    origin_vm_ssh_port: int = Field(description="The VM-root forwarded port on the origin box")
    origin_container_ssh_port: int = Field(description="The container forwarded port on the origin box")
    target_server_id: str = Field(description="The gen-2 box the workspace migrates onto")
    target_vm_ssh_port: int | None = Field(
        default=None, description="The VM-root port reserved on the target box (set once reserved)"
    )
    target_container_ssh_port: int | None = Field(
        default=None, description="The container port reserved on the target box (set once reserved)"
    )
    is_host_key_rotated: bool = Field(
        description="Whether the VM served an adopted (rotated) host key rather than the row's bake key"
    )
    slice_instance_name: str = Field(description="The gen-1 lima instance name (also the gen-2 instance name)")
    slice_disk_name: str = Field(description="The gen-1 lima data-disk name")
    version_tag: str = Field(description="The workspace's default-workspace-template version (git describe)")
    gen1_data_disk_virtual_gib: int = Field(description="The gen-1 data disk's qcow2 virtual size in GiB")
    gen1_data_disk_format: str = Field(description="The gen-1 data disk's image format as qemu-img reports it")
    migrated_data_disk_gib: int = Field(description="The gen-2 data disk size (the row's disk_gb, gen-1 + 16)")
    memory_units: int = Field(description="The machine's size in units (the default 8 for every gen-1 row)")
    saved_artifact: SavedProductArtifact | None = Field(
        default=None, description="The product stop artifact, saved once the stop finished"
    )
    is_origin_vm_kept: bool = Field(
        default=False,
        description="Whether --keep-origin-vm left the halted origin VM in place (finalize it by hand)",
    )
    stage: CutoverStage = Field(description="How far the workspace has progressed")
    last_error: str | None = Field(default=None, description="The last failure, when the stage is FAILED")


class CutoverBoxState(FrozenModel):
    """One box's cutover record (``boxes/<server_id>.json`` in the state dir)."""

    server_id: str = Field(description="bare_metal_servers row id")
    stage: CutoverBoxStage = Field(description="How far the box has progressed")
    storage_partition_bytes: int | None = Field(
        default=None, description="The repaved box's measured storage partition (df size), once repaved"
    )


class WorkspacePreflight(FrozenModel):
    """The preflight's findings for one pool row."""

    host_db_id: str = Field(description="pool_hosts row id")
    host_id: str = Field(description="mngr host id")
    host_name: str = Field(description="The row's host name")
    leased_to_user: str | None = Field(description="Owning user's 16-hex prefix, when leased")
    status: str = Field(description="The row's lifecycle status")
    verdict: RowVerdict = Field(description="Candidate, destroy, or refused")
    remedy: str | None = Field(default=None, description="The operator action for a refused row")
    version_tag: str | None = Field(default=None, description="git describe inside the container")
    baked_version: str | None = Field(default=None, description="attributes.repo_branch_or_tag (the bake's version)")
    is_version_ok: bool = Field(default=False, description="Whether the version is at or above the floor")
    is_host_key_rotated: bool = Field(default=False, description="Whether the VM serves an adopted host key")
    data_disk_format: str | None = Field(default=None, description="qemu-img format of the gen-1 data disk")
    data_disk_virtual_gib: int | None = Field(default=None, description="qemu-img virtual size in GiB")
    disk_gb: int = Field(description="The row's disk_gb (the migrated size 039 stamped)")
    health_warnings: tuple[str, ...] = Field(default=(), description="Health probe findings (warnings only)")
    error: str | None = Field(default=None, description="A probe failure that blocks the row")


class BoxPreflight(FrozenModel):
    """The preflight's findings for one gen-1 box."""

    server_id: str = Field(description="bare_metal_servers row id")
    public_address: str = Field(description="The box's public address")
    status: str = Field(description="The box's lifecycle status")
    workspaces: tuple[WorkspacePreflight, ...] = Field(description="Every pool row on the box")
    error: str | None = Field(default=None, description="A box-level failure (unreachable)")


class PreflightReport(FrozenModel):
    """The tier's gen-1 inventory: every gen-1 box and row, and per-row migrate eligibility.

    Fit against a target box is not computed here -- the migrate checks its
    target's budgets per workspace (the reserve script is the authoritative
    guard) -- so the report is an inventory, not a tier-wide gate.
    """

    env_name: str = Field(description="The activated env")
    boxes: tuple[BoxPreflight, ...] = Field(description="Per-box findings")
    unplaced_workspaces: tuple[WorkspacePreflight, ...] = Field(
        default=(), description="Gen-1 rows on no box (finalized stops; the migrate admin-starts them first)"
    )
    distinct_version_tags: tuple[str, ...] = Field(
        description="Every candidate version (the image tars the migrations need)"
    )
    refused_count: int = Field(description="Rows the migrate cannot take yet")
    error_count: int = Field(description="Rows and boxes whose probes failed")

    @property
    def is_clean(self) -> bool:
        return self.refused_count == 0 and self.error_count == 0


class WorkspaceOutcome(FrozenModel):
    """One workspace's result from a mutating stage (drain / restore)."""

    host_db_id: str = Field(description="pool_hosts row id")
    host_id: str = Field(description="mngr host id")
    stage: CutoverStage = Field(description="The stage the workspace reached")
    detail: str | None = Field(default=None, description="A note or the failure text")


class BoxOutcome(FrozenModel):
    """One box's result from a mutating stage."""

    server_id: str = Field(description="bare_metal_servers row id")
    stage: CutoverBoxStage | None = Field(description="The stage the box reached; None when it failed before any")
    workspaces: tuple[WorkspaceOutcome, ...] = Field(default=(), description="Per-workspace results on the box")
    detail: str | None = Field(default=None, description="A note or the failure text")


class StageReport(FrozenModel):
    """The report a mutating stage writes to the state dir and prints."""

    stage_name: str = Field(description="drain / repave / restore")
    env_name: str = Field(description="The activated env")
    is_dry_run: bool = Field(description="Whether anything was touched")
    boxes: tuple[BoxOutcome, ...] = Field(description="Per-box results")

    @property
    def failed_count(self) -> int:
        box_failures = sum(1 for box in self.boxes if box.stage is None)
        workspace_failures = sum(
            1 for box in self.boxes for workspace in box.workspaces if workspace.stage == CutoverStage.FAILED
        )
        return box_failures + workspace_failures


@pure
def classify_pool_row(status: str, bare_metal_server_id: str | None) -> RowClassification:
    """The verdict for one gen-1 row: migratable, destroyable, or refused with a remedy.

    A ``stopped`` row is a candidate in every shape (retained VM or finalized):
    the migrate admin-starts it and takes it as a running workspace. The
    ``bare_metal_server_id`` still distinguishes reporting (a finalized stop
    sits on no box), but no longer changes the verdict.
    """
    if status == "leased":
        return RowClassification(verdict=RowVerdict.CANDIDATE)
    elif status == "stopped":
        remedy = None if bare_metal_server_id is not None else "stopped on no box; the migrate admin-starts it first"
        return RowClassification(verdict=RowVerdict.CANDIDATE, remedy=remedy)
    elif status in ("available", "released"):
        return RowClassification(verdict=RowVerdict.DESTROY)
    elif status in ("stopping", "starting"):
        return RowClassification(verdict=RowVerdict.REFUSED, remedy="a transition is in flight: wait for it to settle")
    elif status == "crashed":
        return RowClassification(
            verdict=RowVerdict.REFUSED,
            remedy="abandoned row: release it (`minds-admin workspaces release <host_db_id>`)",
        )
    elif status in ("removing", "unreachable", "baking"):
        return RowClassification(
            verdict=RowVerdict.REFUSED,
            remedy=f"{status} row: finish or destroy it (`minds-admin pool destroy <host_db_id>`)",
        )
    else:
        return RowClassification(verdict=RowVerdict.REFUSED, remedy=f"unknown status {status!r}")


@pure
def classify_unplaced_gen1_row(status: str) -> RowClassification:
    """The verdict for a gen-1 row on no box: a finalized stop is migratable, anything else is broken.

    Only ``stopped`` legitimately has no placement (the retention finalize
    freed its slot); any other status on no box is a row wedged mid-transition
    and must be settled or released before it can be migrated.
    """
    classification = classify_pool_row(status, None)
    if classification.verdict == RowVerdict.REFUSED or status == "stopped":
        return classification
    return RowClassification(
        verdict=RowVerdict.REFUSED,
        remedy=f"{status} row with no box placement: wait for it to settle or release it",
    )


@pure
def parse_version_tag(describe_output: str) -> VersionTag | None:
    """Parse ``git describe --match 'minds-v*' --abbrev=0`` output; None when it is not a release tag."""
    match = _VERSION_TAG_RE.match(describe_output.strip())
    if match is None:
        return None
    return VersionTag(
        major=int(match.group(1)), minor=int(match.group(2)), patch=int(match.group(3)), prerelease=match.group("pre")
    )


@pure
def is_version_at_or_above_floor(version: VersionTag) -> bool:
    floor = parse_version_tag(MINIMUM_VERSION_TAG)
    if floor is None:
        raise CutoverError(f"the version floor {MINIMUM_VERSION_TAG!r} is not a release tag")
    return version.as_tuple() >= floor.as_tuple()


@pure
def version_tag_error_or_none(describe_text: str) -> str | None:
    """Why a workspace's ``git describe`` output blocks the cutover, or None when it names a version at or above the floor."""
    version = parse_version_tag(describe_text)
    if version is None:
        return f"git describe output {describe_text!r} is not a minds-v* release tag"
    if not is_version_at_or_above_floor(version):
        return f"version {describe_text} is below the cutover floor"
    return None


@pure
def bake_tag_generation_error_or_none(box_generation: int, repo_branch_or_tag: str | None) -> str | None:
    """Why baking this ref on this box would cross the release/generation pairing, or None when allowed.

    The pairing is what keeps the pools disjoint per client cohort: a 0.5.x
    row on a gen-2 box would hand old clients a gen-2 workspace, and a 0.6.x
    row on a gen-1 box would hand new clients a gen-1 one. Non-release refs
    (dev branches) are exempt -- they are current code, baked at the
    operator's own risk -- and so is the cutover's image-seed bake (its row
    never leases; the caller bypasses this guard explicitly).
    """
    version = parse_version_tag(repo_branch_or_tag or "")
    if version is None:
        return None
    first_gen2 = parse_version_tag(FIRST_GEN2_RELEASE_TAG)
    if first_gen2 is None:
        raise CutoverError(f"the gen-2 release boundary {FIRST_GEN2_RELEASE_TAG!r} is not a release tag")
    is_gen2_tag = version.as_tuple() >= first_gen2.as_tuple()
    if box_generation >= FIRST_QEMU_BOX_GENERATION and not is_gen2_tag:
        return (
            f"tag {repo_branch_or_tag} predates {FIRST_GEN2_RELEASE_TAG} and pairs with gen-1 boxes; "
            f"baking it on a generation-{box_generation} box would hand old clients gen-2 workspaces"
        )
    if box_generation < FIRST_QEMU_BOX_GENERATION and is_gen2_tag:
        return (
            f"tag {repo_branch_or_tag} is a gen-2 release ({FIRST_GEN2_RELEASE_TAG}+); "
            f"baking it on a generation-{box_generation} box would hand new clients gen-1 workspaces"
        )
    return None


@pure
def gen1_data_disk_size_error_or_none(data_disk_virtual_gib: int, row_disk_gb: int) -> str | None:
    """Why a gen-1 data disk cannot be transplanted into the row's stamped gen-2 size, or None when it can.

    Migration 039 stamped every gen-1 row's ``disk_gb`` as its data disk's
    virtual size plus the gen-2 base; the transplant creates the gen-2 disk at
    that size, so the measured disk must still agree with it.
    """
    if data_disk_virtual_gib == row_disk_gb - DATA_DISK_BASE_GIB:
        return None
    return f"data disk virtual size {data_disk_virtual_gib} GiB != row disk_gb {row_disk_gb} - {DATA_DISK_BASE_GIB}"
