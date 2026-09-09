"""Backup capabilities: what snapshot primitives this environment provides.

Capabilities are environment-derived facts, not configuration: the service
detects them in memory at startup by probing the container's filesystem, and
they are deliberately excluded from `data/system/backup.toml` (which holds only
user settings) and from the minds "backup service up to date" comparison
surface. They are logged and included in the backup events stream for
observability; there is no persisted capabilities file and no override
mechanism.

Detection decision tree (everything is probeable from inside the container):
  - If the trigger dir (`/mngr-snapshot/`) exists as a directory, we are
    inside a vps-docker agent container with the snapshot-trigger volume
    mounted -> `outer_trigger`. Snapshots cover the whole unified volume, so
    restic reads the `home/` subtree inside each snapshot (`read_subpath`).
  - Else if the backup root (`/home/user`, resolved through the provider's
    home symlink) is on a btrfs filesystem (lima), we can take snapshots
    directly via `sudo btrfs subvolume snapshot` -> `btrfs_local`.
  - Else (plain docker / any unrecognized provider) -> `direct` (no
    snapshot; restic reads the backup root live).
"""

import os
import subprocess
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

# Well-known in-container paths probed by detection. The outer helper resolves
# the real outer-side subvolume path at request time, so the inner service
# never needs outer-side knowledge beyond these constants.
DEFAULT_TRIGGER_DIR: Final[Path] = Path("/mngr-snapshot")

# The tree backups cover: the whole persistent home (workspace, worktrees,
# mngr state at ~/.mngr, dotfiles) -- NOT just the mngr data dir.
DEFAULT_BACKUP_ROOT: Final[Path] = Path("/home/user")

# Where the agent's mngr host dir lives, used to work out how much of the backup
# root is actually on persistent storage. Read from the environment the service
# inherits; absent when the service is run outside a provisioned agent host.
_HOST_DIR_ENV_VAR: Final[str] = "MNGR_HOST_DIR"

_FINDMNT_TIMEOUT_SECONDS: Final[float] = 15.0


class SnapshotMethod(UpperCaseStrEnum):
    """How the service obtains a consistent view of the host_dir before restic reads it."""

    BTRFS_LOCAL = auto()
    OUTER_TRIGGER = auto()
    DIRECT = auto()


class BackupCapabilities(FrozenModel):
    """The snapshot primitives available in this environment, detected at startup."""

    method: SnapshotMethod = Field(description="Snapshot mechanism for this provider")
    btrfs_mount_path: Path | None = Field(
        default=None,
        description=(
            "Outer-side btrfs mount root, e.g. /mngr-btrfs. Used by "
            "outer_trigger to construct the snapshot_path; ignored for direct. "
            "For btrfs_local, set to the in-VM btrfs mount path."
        ),
    )
    host_subvolume_path: Path | None = Field(
        default=None,
        description=(
            "Absolute path of the host's btrfs subvolume on the (outer or in-VM) "
            "btrfs filesystem. The outer helper resolves the real path at request "
            "time for outer_trigger, so the placeholder value is never used there."
        ),
    )
    snapshot_current_path: Path | None = Field(
        default=None,
        description=(
            "Where the live snapshot slot is created on the btrfs filesystem "
            "(the outer's perspective for outer_trigger, the in-VM view for "
            "btrfs_local). For outer_trigger only the PARENT directory is used "
            "(snapshots get unique per-tick names under it)."
        ),
    )
    snapshot_read_path: Path | None = Field(
        default=None,
        description=(
            "Path the in-container restic actually reads from. For outer_trigger "
            "this is /mngr-snapshots/current (the bind mount of the outer's "
            "snapshot dir; only the PARENT directory is used); for btrfs_local it "
            "equals snapshot_current_path; for direct it is the host_dir itself."
        ),
    )
    trigger_dir: Path | None = Field(
        default=None,
        description=(
            "Inner-container dir where request.json / result.json live for "
            "outer_trigger (e.g. /mngr-snapshot). Present only for outer_trigger."
        ),
    )
    read_subpath: str | None = Field(
        default=None,
        description=(
            "Subdirectory inside each snapshot that corresponds to the backup "
            "root. outer_trigger snapshots cover the whole unified volume "
            "(provider bookkeeping beside home/), so restic reads <snapshot>/home; "
            "None when the snapshot root IS the backup root."
        ),
    )
    is_backup_root_fully_persisted: bool = Field(
        default=True,
        description=(
            "Whether the whole backup root sits on the same persistent storage as "
            "the mngr host dir. False means only the host dir is persisted and "
            "everything else under the backup root -- the user's workspace above "
            "all -- is in no backup and cannot be restored. Defaults to True so "
            "an environment this cannot be probed in is never falsely alarmed."
        ),
    )
    outer_helper_timeout_seconds: float = Field(
        default=120.0,
        description="Hard cap on how long to wait for the outer helper's result.json",
    )
    max_local_snapshots: int = Field(
        default=5,
        ge=1,
        description=(
            "outer_trigger only: how many on-host btrfs snapshots to retain. "
            "Each tick creates a new timestamped snapshot and deletes the "
            "oldest beyond this count. Ignored by btrfs_local and direct."
        ),
    )


def _read_filesystem_id(path: Path) -> int | None:
    """The id of the filesystem a path is on, or None when it cannot be read."""
    try:
        return path.stat().st_dev
    except OSError as e:
        logger.debug("Could not stat {} while probing backup coverage: {}", path, e)
        return None


def are_backup_root_and_host_dir_on_one_filesystem(
    backup_root_filesystem_id: int | None,
    host_dir_filesystem_id: int | None,
) -> bool:
    """Whether the whole backup root is persisted, or only the mngr host dir within it.

    The mngr host dir is always on persistent storage -- that is what makes an
    agent's state survive its container. So if the backup root is on a *different*
    filesystem, the rest of the tree (the user's workspace above all) is not
    persisted, and no snapshot of that storage can contain it.

    Unknown reads as fully persisted: a probe that failed is not evidence of a
    gap, and a false alarm here would teach people to ignore a real one.
    """
    if backup_root_filesystem_id is None or host_dir_filesystem_id is None:
        return True
    return backup_root_filesystem_id == host_dir_filesystem_id


def _probe_backup_root_coverage(backup_root: Path) -> bool:
    host_dir_value = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir_value:
        return True
    return are_backup_root_and_host_dir_on_one_filesystem(
        backup_root_filesystem_id=_read_filesystem_id(backup_root),
        host_dir_filesystem_id=_read_filesystem_id(Path(host_dir_value)),
    )


def detect_backup_capabilities(
    *,
    trigger_dir: Path = DEFAULT_TRIGGER_DIR,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
) -> BackupCapabilities:
    """Probe the container's filesystem to choose the right snapshot mechanism."""
    is_fully_persisted = _probe_backup_root_coverage(backup_root)
    if trigger_dir.is_dir():
        # vps-docker: snapshots dir is bind-mounted at /mngr-snapshots; the
        # outer helper resolves <btrfs-mount>/<host_id_hex>/snapshots/<name>
        # at request time, so the inner service doesn't need to know the
        # outer-side path -- it just reads what appears at /mngr-snapshots/.
        return BackupCapabilities(
            method=SnapshotMethod.OUTER_TRIGGER,
            btrfs_mount_path=Path("/mngr-btrfs"),
            host_subvolume_path=Path("/mngr-btrfs/<host_id_hex>"),
            snapshot_current_path=Path("/mngr-btrfs/snapshots/current"),
            snapshot_read_path=Path("/mngr-snapshots/current"),
            trigger_dir=trigger_dir,
            read_subpath="home",
            is_backup_root_fully_persisted=is_fully_persisted,
        )
    # The provider reaches the persistent home via a symlink (lima: onto the
    # btrfs data disk), so probe the resolved path -- a symlink itself is
    # never a mountpoint.
    resolved_root = backup_root.resolve()
    fstype = _findmnt_fstype(resolved_root)
    if fstype == "btrfs":
        # lima attaches a btrfs additional disk and symlinks the home tree to
        # its mount point, so the btrfs filesystem *is* the backup root. The
        # snapshot must live on that same btrfs (you cannot snapshot a
        # subvolume onto another filesystem), so derive every path from it.
        return BackupCapabilities(
            method=SnapshotMethod.BTRFS_LOCAL,
            btrfs_mount_path=resolved_root,
            host_subvolume_path=resolved_root,
            snapshot_current_path=resolved_root / "snapshots" / "current",
            snapshot_read_path=resolved_root / "snapshots" / "current",
            is_backup_root_fully_persisted=is_fully_persisted,
        )
    # direct takes no snapshot: restic reads the backup root live, so whatever
    # is under it is covered and the persistence probe does not apply. The field
    # keeps its True default rather than reporting a gap that is not one.
    return BackupCapabilities(
        method=SnapshotMethod.DIRECT,
        snapshot_read_path=backup_root,
    )


def _findmnt_fstype(path: Path) -> str:
    """Return the filesystem type for `path` via `findmnt`; empty string on any failure."""
    try:
        result = subprocess.run(
            # -T (--target) walks up to the containing mount, so a path INSIDE
            # a filesystem (not itself a mountpoint) still reports its fstype.
            ["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_FINDMNT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        # An unexpected findmnt failure downgrades detection to `direct` (no
        # snapshots), so surface why rather than falling back silently.
        logger.warning("findmnt failed for {}: {}", path, e)
        return ""
    if result.returncode != 0:
        # Same rationale as above: a nonzero exit silently downgrades a
        # snapshot-capable host to `direct`, so say why.
        logger.warning(
            "findmnt exited {} for {}: {}",
            result.returncode,
            path,
            result.stderr.strip(),
        )
        return ""
    return result.stdout.strip()
