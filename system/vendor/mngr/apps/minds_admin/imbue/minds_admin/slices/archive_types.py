"""Frozen records of the workspace archive (``minds-admin archives``).

A workspace the gen-2 migration cannot take (its version is below the
cutover floor, or it carries no ``minds-v*`` release tag at all) is archived
for its owner before it is retired: one zip of everything it holds, streamed
from its running VM into the tier's workspace-storage bucket, described by a
manifest beside it. Every archive is a new object under the workspace's
prefix; nothing is ever overwritten.
"""

import json
import posixpath
import re
from datetime import datetime
from enum import auto
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.minds_admin.slices.cutover_types import MINIMUM_VERSION_TAG
from imbue.minds_admin.slices.cutover_types import version_tag_error_or_none

# The bucket layout: ``<env key prefix>archives/<host_id>/<stamp>/`` holds one
# archive's zip and manifest. Deliberately outside ``<host_id>/`` (the product's
# release deletes that prefix) and outside ``cutover/`` (scheduled for wholesale
# deletion once the migration is over).
ARCHIVES_KEY_SEGMENT: Final[str] = "archives"
ARCHIVE_ZIP_FILENAME: Final[str] = "workspace.zip"
ARCHIVE_MANIFEST_FILENAME: Final[str] = "manifest.json"
ARCHIVE_STAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"

# The zip's two top-level folders: the workspace's persistent volume (its
# ``home/`` tree, or on the legacy layout ``host_dir/`` and ``agents/``) and
# the workspace container's own writable layer (the home dotfiles, Claude's
# config and transcripts, ``/var/log/mngr`` and the supervisor logs on layouts
# that keep those outside the volume).
ARCHIVE_VOLUME_PREFIX: Final[str] = "volume"
ARCHIVE_CONTAINER_LAYER_PREFIX: Final[str] = "container-layer"

# The regenerable caches left out of the zip: host_backup's default excludes
# (``system/services/host_backup/src/host_backup/config.py`` in the template),
# so an archive holds the same set of files a restic snapshot would.
DEFAULT_ARCHIVE_EXCLUDES: Final[tuple[str, ...]] = (
    "**/.venv",
    "**/node_modules",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/.ruff_cache",
    "**/target",
    "**/dist",
    "**/build",
    "**/.next",
    "**/.cache",
    "**/.cargo/registry",
    "**/.cargo/git",
    "**/.rustup/toolchains",
    "**/.rustup/downloads",
)

# S3 presigned URLs (SigV4) are valid for at most seven days; links are minted
# on demand (``minds-admin archives links``) rather than kept.
MAX_PRESIGNED_LINK_SECONDS: Final[int] = 7 * 24 * 3600
DEFAULT_PRESIGNED_LINK_SECONDS: Final[int] = MAX_PRESIGNED_LINK_SECONDS

# The markers the box-side upload script prints once the stream has landed.
ARCHIVE_SHA256_MARKER: Final[str] = "MNGR_ARCHIVE_SHA256"
ARCHIVE_BYTES_MARKER: Final[str] = "MNGR_ARCHIVE_BYTES"
# The marker the VM-side zip streamer prints on stderr (``archive_stream_zip.py``).
ARCHIVE_SUMMARY_MARKER: Final[str] = "MNGR_ARCHIVE_SUMMARY"

MANIFEST_SCHEMA_VERSION: Final[int] = 1

_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class ArchiveError(MindError):
    """Raised when a workspace cannot be archived, or an archive cannot be found or linked."""


class RetireVerdict(UpperCaseStrEnum):
    """Whether a gen-1 workspace is one the migration cannot take (and so is archived and retired)."""

    # Its live version is below the floor, or names no release tag at all.
    RETIRE = auto()
    # Its live version is at or above the floor: the migrate takes it.
    MIGRATE = auto()
    # A stopped workspace baked below the floor (or from a branch): only a live
    # probe after a start tells whether it self-updated past the floor.
    UNKNOWN_UNTIL_STARTED = auto()
    # Not a workspace (an unleased pool row) or one mid-transition.
    SKIP = auto()


class ArchiveCandidate(FrozenModel):
    """One gen-1 pool row with its retire verdict."""

    host_db_id: str = Field(description="pool_hosts row id")
    host_id: str = Field(description="mngr host id")
    host_name: str = Field(description="The row's host name")
    leased_to_user: str | None = Field(description="Owning user's 16-hex prefix, when leased")
    status: str = Field(description="The row's lifecycle status")
    baked_version: str | None = Field(description="attributes.repo_branch_or_tag (the bake's version)")
    live_version: str | None = Field(description="The version probe's output on the running container, when probed")
    verdict: RetireVerdict = Field(description="Retire, migrate, unknown until started, or skip")
    reason: str = Field(description="Why the verdict is what it is")


class ArchiveStreamSummary(FrozenModel):
    """What the VM-side zip streamer reported: the entries it wrote and what it skipped."""

    entry_count: int = Field(description="Zip entries written (files, directories, symlinks, the README)")
    entry_bytes: int = Field(description="Uncompressed bytes of the regular files written")
    skipped_count: int = Field(description="Entries skipped (special files, unreadable files)")
    skipped_paths: tuple[str, ...] = Field(description="The first skipped entries with their reasons")


class WorkspaceArchiveSource(FrozenModel):
    """What the archive read off the running workspace before streaming it."""

    container_id: str = Field(description="The workspace container's id")
    container_name: str = Field(description="The workspace container's name")
    volume_subvolume_path: str = Field(description="The VM path of the workspace's btrfs subvolume")
    container_layer_path: str = Field(description="The VM path of the container's overlay upper (writable) layer")
    home_layout: str = Field(
        description="The container's home layout as the probe printed it (home / legacy / unknown)"
    )
    version_probe: str = Field(description="The version probe's output ('' when it named no version)")


class WorkspaceArchiveManifest(FrozenModel):
    """One archive's record, stored beside its zip in the bucket and in the operator's state dir."""

    schema_version: int = Field(default=MANIFEST_SCHEMA_VERSION, description="The manifest format version")
    env_name: str = Field(description="The activated env the archive was taken from")
    host_db_id: str = Field(description="pool_hosts row id")
    host_id: str = Field(description="mngr host id")
    agent_id: str | None = Field(description="The workspace's services agent id (its workspace id)")
    host_name: str = Field(description="The row's host name")
    owner_user_id_prefix: str | None = Field(description="Owning user's 16-hex prefix")
    owner_email: str | None = Field(description="Owning account's email, when resolved")
    created_at: datetime = Field(description="When the archive was taken")
    workspace_status_before_archive: str = Field(
        description=(
            "The row's status as the create command found it: 'stopped' when --start-stopped admin-started the "
            "workspace for the archive, 'leased' when it was already running"
        )
    )
    box_generation: int = Field(description="The slice-fleet generation the workspace ran on")
    version_probe: str = Field(description="The version probe's output on the container ('' when none)")
    baked_version: str | None = Field(description="attributes.repo_branch_or_tag (the bake's version)")
    home_layout: str = Field(description="The container's home layout (home / legacy / unknown)")
    volume_subvolume_path: str = Field(description="The VM path the volume half was read from (a snapshot of it)")
    container_layer_path: str = Field(description="The VM path the container-layer half was read from")
    bucket: str = Field(description="The tier bucket holding the archive")
    zip_key: str = Field(description="The zip's object key")
    manifest_key: str = Field(description="This manifest's object key")
    zip_size_bytes: int = Field(description="The zip's size in bytes, as the upload measured it")
    zip_sha256: str = Field(description="The zip's sha256, as the upload measured it")
    excludes: tuple[str, ...] = Field(description="The exclude patterns the zip was taken with")
    summary: ArchiveStreamSummary = Field(description="What the zip streamer reported")


@pure
def render_manifest_json(manifest: WorkspaceArchiveManifest) -> str:
    """The manifest as it is stored, beside the zip in the bucket and in the operator's state dir."""
    return manifest.model_dump_json(indent=2)


@pure
def archive_object_prefix(storage_key_prefix: str, host_id: str, created_at: datetime) -> str:
    """``<env prefix>archives/<host_id>/<stamp>``: the prefix one archive's objects live under."""
    return f"{storage_key_prefix}{ARCHIVES_KEY_SEGMENT}/{host_id}/{created_at.strftime(ARCHIVE_STAMP_FORMAT)}"


@pure
def archives_listing_prefix(storage_key_prefix: str, host_id: str | None) -> str:
    """The prefix under which every archive (of one workspace, or of the env) is listed."""
    base = f"{storage_key_prefix}{ARCHIVES_KEY_SEGMENT}/"
    return base if host_id is None else f"{base}{host_id}/"


@pure
def archive_zip_key(object_prefix: str) -> str:
    return posixpath.join(object_prefix, ARCHIVE_ZIP_FILENAME)


@pure
def archive_manifest_key(object_prefix: str) -> str:
    return posixpath.join(object_prefix, ARCHIVE_MANIFEST_FILENAME)


@pure
def classify_retire_verdict(
    status: str, baked_version: str | None, live_version: str | None
) -> tuple[RetireVerdict, str]:
    """The retire verdict for one gen-1 row and the reason for it.

    A live probe (the running container's version) is authoritative; without
    one, the bake's version is only a lower bound (a workspace can update
    itself past the floor but never below it), so a bake at or above the
    floor is a migrate verdict and anything else waits for a start.
    """
    if status in ("available", "released", "baking"):
        return RetireVerdict.SKIP, f"unleased ({status}) pool row"
    if status not in ("leased", "stopped"):
        return RetireVerdict.SKIP, f"{status} row: settle its transition first"
    if live_version is not None:
        live_error = version_tag_error_or_none(live_version)
        if live_error is None:
            return RetireVerdict.MIGRATE, f"live version {live_version} is at or above {MINIMUM_VERSION_TAG}"
        return RetireVerdict.RETIRE, live_error
    baked_error = version_tag_error_or_none(baked_version or "")
    if baked_error is None:
        return RetireVerdict.MIGRATE, f"baked at {baked_version}, at or above {MINIMUM_VERSION_TAG}"
    if status == "leased":
        return RetireVerdict.UNKNOWN_UNTIL_STARTED, f"baked version: {baked_error}; probe the running container"
    return RetireVerdict.UNKNOWN_UNTIL_STARTED, f"baked version: {baked_error}; start it to read its live version"


@pure
def parse_archive_upload_output(stdout: str) -> tuple[str, int]:
    """The ``(sha256, byte count)`` the box-side upload printed behind its markers."""
    sha256: str | None = None
    byte_count: int | None = None
    for line in stdout.splitlines():
        marker, _separator, value = line.strip().partition(" ")
        if marker == ARCHIVE_SHA256_MARKER:
            sha256 = value.strip()
        elif marker == ARCHIVE_BYTES_MARKER:
            byte_count = int(value.strip()) if value.strip().isdigit() else None
        else:
            continue
    if sha256 is None or not _SHA256_RE.match(sha256):
        raise ArchiveError(f"the upload printed no sha256 marker: {stdout[-500:]!r}")
    if byte_count is None or byte_count <= 0:
        raise ArchiveError(f"the upload printed no byte-count marker (or an empty upload): {stdout[-500:]!r}")
    return sha256, byte_count


@pure
def parse_archive_stream_summary(stderr: str) -> ArchiveStreamSummary:
    """The zip streamer's summary line, found among whatever else the pipeline wrote to stderr."""
    for line in reversed(stderr.splitlines()):
        marker, _separator, payload = line.strip().partition(" ")
        if marker != ARCHIVE_SUMMARY_MARKER:
            continue
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ArchiveError(f"the zip streamer's summary is not JSON: {payload[:200]!r}") from exc
        return ArchiveStreamSummary.model_validate(raw)
    raise ArchiveError(f"the zip streamer printed no summary line: {stderr[-500:]!r}")
