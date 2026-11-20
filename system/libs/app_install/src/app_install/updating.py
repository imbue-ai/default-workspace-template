"""Updating an installed app from the repository it came from.

The new version is merged over the workspace's own changes to the app with ``git subtree merge
--squash``, in a worktree of the installer's own that lives under the workspace's state
directory, so that an update which needs a person (a conflict in the app's code, or tests that
fail) can be picked up again with ``--continue`` once an agent has resolved it there. The live
app is untouched until the result lands.
"""

import json
import shutil
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from loguru import logger
from pydantic import Field

from app_install.collisions import find_collisions
from app_install.derived import derive_files
from app_install.derived import program_names
from app_install.derived import write_derived_files
from app_install.errors import AppInstallError
from app_install.errors import CollisionError
from app_install.errors import GitCommandError
from app_install.errors import NoSnapshotError
from app_install.errors import NotInstalledAppError
from app_install.errors import UpdateInProgressError
from app_install.git import fetch_commit
from app_install.git import land_commit
from app_install.git import resolve_ref
from app_install.git import run_git
from app_install.interfaces import WorkspaceRuntimeInterface
from app_install.paths import APP_DATA_DIRECTORY
from app_install.paths import APPS_DIRECTORY
from app_install.paths import STATE_DIRECTORY
from app_install.paths import UPSTREAM_REF_PREFIX
from app_install.paths import manifest_path
from app_manifest.errors import AppManifestError
from app_manifest.manifest import AppManifest
from app_manifest.manifest import ManifestReadMode
from app_manifest.manifest import load_manifest
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import load_record
from app_manifest.records import write_record

# The files in an app's directory that are a function of other files there: on a conflict the upstream side is
# taken and the file is made again, and a difference in one alone does not make an installed app modified.
REGENERABLE_FILENAMES: Final[frozenset[str]] = frozenset({"uv.lock"})
_WORKTREES_DIRNAME: Final[str] = "worktrees"
_SNAPSHOTS_DIRNAME: Final[str] = "snapshots"
_NOBACKUP_MARKER: Final[str] = ".nobackup"
_SNAPSHOT_LABEL_LENGTH: Final[int] = 12
# A snapshot must leave the disk room to work in.
_SNAPSHOT_HEADROOM_FACTOR: Final[float] = 1.2


class UpdateResult(LowerCaseStrEnum):
    """How an update ended."""

    # The app is already at the version asked for.
    UP_TO_DATE = auto()
    # The new version is live.
    UPDATED = auto()
    # The new version conflicts with the workspace's changes to the app; the worktree waits for a resolution.
    CONFLICT = auto()
    # The merged app's own checks fail; the worktree waits for a fix.
    TESTS_FAILED = auto()


class PendingUpdate(FrozenModel):
    """An update that stopped for a person: what it was bringing in."""

    ref: str = Field(description="The tag or branch being installed")
    commit: str = Field(description="The upstream commit being installed")
    snapshot: str = Field(
        default="", description="Where the app's data was copied before the update started, repo-relative"
    )


class UpdateOutcome(FrozenModel):
    """What an update did."""

    result: UpdateResult = Field(description="How the update ended")
    record: AppRecord = Field(description="The app's record as it is now")
    ref: str = Field(description="The tag or branch that was asked for")
    previous_version: str = Field(description="The version the app was at")
    conflicted_paths: tuple[str, ...] = Field(default=(), description="The files an agent must resolve")
    worktree: str = Field(default="", description="Where a stopped update waits, repo-relative")
    snapshot: str = Field(default="", description="Where the app's data was copied before the update, repo-relative")
    detail: str = Field(default="", description="Why the app's checks failed")
    manifest: AppManifest | None = Field(default=None, description="The updated app's manifest")
    problems: tuple[str, ...] = Field(default=(), description="What could not be done outside the tree")


def update_worktree_path(repo_root: Path, name: str) -> Path:
    return repo_root / STATE_DIRECTORY / _WORKTREES_DIRNAME / name


def _pending_path(repo_root: Path, name: str) -> Path:
    return repo_root / STATE_DIRECTORY / _WORKTREES_DIRNAME / f"{name}.json"


def _directory_size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file() and not path.is_symlink())


def snapshot_app_data(repo_root: Path, record: AppRecord, runtime: WorkspaceRuntimeInterface) -> str:
    """Copy the app's stored data aside before a new version runs over it, and return where, repo-relative.

    Only the newest snapshot of an app is kept, and the snapshots stay out of the workspace's backup. An app
    with no stored data has nothing to copy. Raises NoSnapshotError when the data does not fit on the disk and
    the workspace has no recent backup to fall back on.
    """
    data_directory = repo_root / APP_DATA_DIRECTORY / str(record.name)
    if not data_directory.is_dir():
        return ""
    snapshots_directory = repo_root / STATE_DIRECTORY / _SNAPSHOTS_DIRNAME / str(record.name)
    snapshots_directory.mkdir(parents=True, exist_ok=True)
    needed = _directory_size(data_directory) * _SNAPSHOT_HEADROOM_FACTOR
    if needed > runtime.free_disk_bytes(snapshots_directory):
        if runtime.has_recent_backup():
            return ""
        raise NoSnapshotError(
            f"the data of {record.name} does not fit on the disk beside itself and the workspace has no recent "
            "backup, so there would be no way back from the update"
        )
    (snapshots_directory / _NOBACKUP_MARKER).touch()
    # Named for the version the data belongs to, so a second update from the same version replaces it.
    label = record.upstream_commit[:_SNAPSHOT_LABEL_LENGTH] or "unversioned"
    for previous in snapshots_directory.iterdir():
        if previous.is_dir():
            shutil.rmtree(previous)
    destination = snapshots_directory / label
    shutil.copytree(data_directory, destination, symlinks=True)
    return destination.relative_to(repo_root).as_posix()


def _conflicted_paths(worktree: Path) -> tuple[str, ...]:
    return tuple(line for line in run_git(worktree, ["diff", "--name-only", "--diff-filter=U"]).splitlines() if line)


def _remove_worktree(repo_root: Path, name: str) -> None:
    worktree = update_worktree_path(repo_root, name)
    if worktree.exists():
        try:
            run_git(repo_root, ["worktree", "remove", "--force", str(worktree)])
        except GitCommandError as e:
            logger.warning("Could not remove the update's worktree {}: {}", worktree, e)
            shutil.rmtree(worktree, ignore_errors=True)
    run_git(repo_root, ["worktree", "prune"])
    _pending_path(repo_root, name).unlink(missing_ok=True)


def _merge_new_version(
    repo_root: Path, record: AppRecord, pending: PendingUpdate, runtime: WorkspaceRuntimeInterface
) -> tuple[str, ...]:
    """Merge the new version into a fresh worktree; the files left in conflict, empty when it merged."""
    worktree = update_worktree_path(repo_root, str(record.name))
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_git(repo_root, ["worktree", "add", "--detach", str(worktree), "HEAD"])
    _pending_path(repo_root, str(record.name)).write_text(pending.model_dump_json(), encoding="utf-8")
    prefix = (APPS_DIRECTORY / record.package).as_posix()
    try:
        run_git(
            worktree,
            [
                "subtree",
                "merge",
                f"--prefix={prefix}",
                pending.commit,
                "--squash",
                "-m",
                f"app-install: update {record.name} to {pending.ref}",
            ],
        )
    except GitCommandError as merge_failure:
        conflicted = _conflicted_paths(worktree)
        if not conflicted:
            _remove_worktree(repo_root, str(record.name))
            raise merge_failure
        regenerable = [path for path in conflicted if Path(path).name in REGENERABLE_FILENAMES]
        if len(regenerable) != len(conflicted):
            return conflicted
        # Only files that are made from other files conflict: take the publisher's and make them again.
        run_git(worktree, ["checkout", "--theirs", "--", *regenerable])
        relock_failure = runtime.relock_app(worktree, record.package)
        if relock_failure is not None:
            logger.warning("Could not relock {} after its update: {}", record.name, relock_failure)
        run_git(worktree, ["add", "--", *regenerable])
        run_git(worktree, ["commit", "--quiet", "--no-edit"])
    return ()


def _finish(
    repo_root: Path,
    record: AppRecord,
    pending: PendingUpdate,
    runtime: WorkspaceRuntimeInterface,
) -> UpdateOutcome:
    """Register, check, and land the merged app that waits in the update's worktree."""
    name = str(record.name)
    snapshot = pending.snapshot
    worktree = update_worktree_path(repo_root, name)
    relative_worktree = worktree.relative_to(repo_root).as_posix()
    previous_version = record.upstream_version
    conflicted = _conflicted_paths(worktree)
    if conflicted:
        return UpdateOutcome(
            result=UpdateResult.CONFLICT,
            record=record,
            ref=pending.ref,
            previous_version=previous_version,
            conflicted_paths=conflicted,
            worktree=relative_worktree,
            snapshot=snapshot,
        )
    if run_git(worktree, ["status", "--porcelain", "--untracked-files=no"]):
        return UpdateOutcome(
            result=UpdateResult.CONFLICT,
            record=record,
            ref=pending.ref,
            previous_version=previous_version,
            worktree=relative_worktree,
            snapshot=snapshot,
            detail="the resolution in the worktree is not committed yet",
        )
    manifest = load_manifest(
        manifest_path(worktree, record.package), repo_root=worktree, mode=ManifestReadMode.TOLERANT
    )
    collisions = find_collisions(worktree, record.package, manifest, frozenset())
    if collisions:
        raise CollisionError(collisions)
    updated_record = record.model_copy_update(
        to_update(record.field_ref().upstream_commit, pending.commit),
        to_update(record.field_ref().upstream_tree, run_git(repo_root, ["rev-parse", f"{pending.commit}^{{tree}}"])),
        to_update(record.field_ref().upstream_version, manifest.version or ""),
    )
    write_record(worktree, updated_record)
    write_derived_files(worktree, record.package, derive_files(worktree, updated_record, manifest))
    run_git(worktree, ["add", "--all"])
    if run_git(worktree, ["status", "--porcelain"]):
        run_git(worktree, ["commit", "--quiet", "-m", f"app-install: register {name} at {pending.ref}"])

    check_failure = runtime.verify_app(worktree, record.package)
    if check_failure is not None:
        return UpdateOutcome(
            result=UpdateResult.TESTS_FAILED,
            record=record,
            ref=pending.ref,
            previous_version=previous_version,
            worktree=relative_worktree,
            snapshot=snapshot,
            detail=check_failure,
        )

    land_commit(repo_root, run_git(worktree, ["rev-parse", "HEAD"]))
    run_git(repo_root, ["update-ref", f"{UPSTREAM_REF_PREFIX}{name}", pending.commit])
    _remove_worktree(repo_root, name)

    problems: list[str] = []
    unresolved_environment = runtime.install_environment(manifest.environment)
    problems.extend(f"could not install {entry}" for entry in unresolved_environment)
    reload_failure = runtime.reload_programs() or runtime.restart_programs(program_names(manifest))
    if reload_failure is not None:
        problems.append(f"supervisord did not restart the app's programs: {reload_failure}")
    return UpdateOutcome(
        result=UpdateResult.UPDATED,
        record=updated_record,
        ref=pending.ref,
        previous_version=previous_version,
        snapshot=snapshot,
        manifest=manifest,
        problems=tuple(problems),
    )


def update_app(
    repo_root: Path,
    name: str,
    requested_ref: str | None,
    is_continuing: bool,
    runtime: WorkspaceRuntimeInterface,
) -> UpdateOutcome:
    """Bring the installed app ``name`` to ``requested_ref`` of the repository it came from, or to its latest
    version; with ``is_continuing``, finish an update that stopped for a person.

    Raises AppRecordNotFoundError for an app with no record, NotInstalledAppError for an app this workspace
    built, UpdateInProgressError when an update of the app is already waiting (or, when continuing, none is),
    NoSnapshotError when the app's data cannot be kept safe, and CollisionError when the new version would take a
    name something in the workspace holds. The primary checkout is unchanged unless the result is UPDATED.
    """
    record = load_record(repo_root, name)
    if record.origin is not AppOrigin.INSTALLED or not record.upstream_url:
        raise NotInstalledAppError(f"{name} was built in this workspace, so there is nothing to update it from")
    pending_file = _pending_path(repo_root, name)
    worktree = update_worktree_path(repo_root, name)
    if is_continuing:
        if not pending_file.is_file() or not worktree.is_dir():
            raise UpdateInProgressError(f"no update of {name} is waiting to be continued")
        try:
            pending = PendingUpdate.model_validate(json.loads(pending_file.read_text(encoding="utf-8")))
        except (ValueError, AppManifestError) as e:
            raise AppInstallError(f"the waiting update of {name} cannot be read: {e}") from e
        return _finish(repo_root, record, pending, runtime)
    if pending_file.exists() or worktree.exists():
        raise UpdateInProgressError(
            f"an update of {name} is waiting at {worktree.relative_to(repo_root).as_posix()}; resolve it there and "
            "run the update again with --continue"
        )
    ref = resolve_ref(repo_root, record.upstream_url, requested_ref)
    commit = fetch_commit(repo_root, record.upstream_url, ref)
    if commit == record.upstream_commit:
        return UpdateOutcome(
            result=UpdateResult.UP_TO_DATE, record=record, ref=ref, previous_version=record.upstream_version
        )
    snapshot = snapshot_app_data(repo_root, record, runtime)
    pending = PendingUpdate(ref=ref, commit=commit, snapshot=snapshot)
    conflicted = _merge_new_version(repo_root, record, pending, runtime)
    if conflicted:
        return UpdateOutcome(
            result=UpdateResult.CONFLICT,
            record=record,
            ref=ref,
            previous_version=record.upstream_version,
            conflicted_paths=conflicted,
            worktree=worktree.relative_to(repo_root).as_posix(),
            snapshot=snapshot,
        )
    return _finish(repo_root, record, pending, runtime)
