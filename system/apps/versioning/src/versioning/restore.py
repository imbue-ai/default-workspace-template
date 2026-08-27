import subprocess
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger

from versioning.data_types import AppHistory
from versioning.data_types import AppRef
from versioning.data_types import RestoreError
from versioning.data_types import RestorePreview
from versioning.data_types import RestoreResult
from versioning.data_types import TrailerBlock
from versioning.data_types import VersionKind
from versioning.interfaces import GitRepoInterface
from versioning.locking import operation_lock
from versioning.trailers import serialize_trailer_block

_SUPERVISORCTL_TIMEOUT_SECONDS: Final[float] = 60.0
_UV_SYNC_TIMEOUT_SECONDS: Final[float] = 300.0

# The versioning app itself: restoring it would restart the very service running
# the restore, so it can be browsed but not restored.
SELF_APP_NAME: Final[str] = "versioning"

# Browse-only apps: versioning (above) and the workspace shell, whose safe
# rollback path is the update-system-interface reveal machinery, not a plain
# folder restore + restart.
UNRESTORABLE_APP_NAMES: Final[frozenset[str]] = frozenset({SELF_APP_NAME, "system-interface"})


# Keep Versioning-Request values comfortably under the ~80 char convention.
_MAX_REQUEST_TITLE_CHARS: Final[int] = 60


@pure
def _restore_request_title(target_title: str | None) -> str:
    """The restore version's name: it names the version it went back to.

    Phrased as a completed fact rather than an action, so it cannot be misread as
    something to click; it is also the whole description of the version, which is
    why a restore carries no size phrase underneath (see magnitude.version_phrase).
    """
    if target_title is None or not target_title.strip():
        return "Restored from an earlier version"
    title = target_title.strip()
    if len(title) > _MAX_REQUEST_TITLE_CHARS:
        title = title[: _MAX_REQUEST_TITLE_CHARS - 1] + "…"
    return f'Restored from "{title}"'


def build_restore_preview(
    git_repo: GitRepoInterface,
    history: AppHistory,
    target_sha: str,
) -> RestorePreview:
    """Raises RestoreError if the target version is not in the app's history."""
    target_idx = next((idx for idx, node in enumerate(history.nodes) if node.sha == target_sha), None)
    if target_idx is None:
        raise RestoreError(f"No version '{target_sha}' for {history.app.name}")
    # Positional, not time-based: commits made in the same second tie on timestamps.
    later_nodes = [node for node in history.nodes[target_idx + 1 :] if not node.is_set_aside]
    return RestorePreview(
        target_sha=target_sha,
        changed_file_count=git_repo.read_changed_file_count_between(target_sha, history.app.package_dir),
        set_aside_node_count=len(later_nodes),
        diff_stat=git_repo.read_diff_stat_between(target_sha, history.app.package_dir),
    )


def restart_service(program: str) -> None:
    """Raises RestoreError if the service does not come back RUNNING."""
    completed = subprocess.run(
        ["supervisorctl", "restart", program],
        capture_output=True,
        text=True,
        timeout=_SUPERVISORCTL_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RestoreError(f"Could not restart {program}: {completed.stderr.strip() or completed.stdout.strip()}")
    status = subprocess.run(
        ["supervisorctl", "status", program],
        capture_output=True,
        text=True,
        timeout=_SUPERVISORCTL_TIMEOUT_SECONDS,
    )
    if "RUNNING" not in status.stdout and "STARTING" not in status.stdout:
        raise RestoreError(f"{program} did not come back after restore: {status.stdout.strip()}")


def _sync_dependencies(repo_root: Path) -> None:
    """Raises RestoreError if dependency sync fails."""
    completed = subprocess.run(
        ["uv", "sync", "--all-packages"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_UV_SYNC_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RestoreError(f"Dependency sync failed: {completed.stderr.strip()[:500]}")


def _refresh_open_tab(repo_root: Path, app_name: str) -> None:
    # Best-effort: reloading the app's open tab is cosmetic, so a failure only logs.
    completed = subprocess.run(
        ["python3", "system/scripts/layout.py", "refresh", app_name],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_SUPERVISORCTL_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        logger.warning("Could not refresh the {} tab: {}", app_name, completed.stderr.strip())


def perform_restore(
    git_repo: GitRepoInterface,
    app: AppRef,
    target_sha: str,
    lock_file: Path,
    is_service_managed: bool,
) -> RestoreResult:
    """Restore the app's folder to the target commit as a new commit, then revive the app.

    Raises RestoreError when the restore cannot proceed safely.
    """
    if app.name in UNRESTORABLE_APP_NAMES:
        raise RestoreError(f"{app.title} can be browsed here but not restored")
    with operation_lock(lock_file):
        # Save any in-progress edits first so nothing is silently lost. The commit
        # carries its own trailer block so it renders as its own version node.
        dirty_paths = git_repo.read_dirty_paths_under(app.package_dir)
        if len(dirty_paths) > 0:
            logger.debug("Saving {} in-progress files before restore", len(dirty_paths))
            wip_trailers = serialize_trailer_block(
                TrailerBlock(
                    app_name=app.name,
                    request=f"Saved work in progress on {app.title}",
                    kind=VersionKind.CHANGE,
                )
            )
            git_repo.commit_paths(
                app.package_dir,
                f"versioning: save work in progress on {app.name}\n\n{wip_trailers}",
            )

        # Track whether the app's dependency manifest changes so we can re-sync.
        pyproject_path = f"{app.package_dir}/pyproject.toml"
        pyproject_before = git_repo.read_file_at_commit("HEAD", pyproject_path)
        pyproject_target = git_repo.read_file_at_commit(target_sha, pyproject_path)

        # Make the folder match the target version and record it as a new commit,
        # stamped with the trailer the tree derivation reads. The new version is
        # named after the version it went back to, so the timeline stays readable.
        target_commit = next(
            (c for c in git_repo.read_commits_touching_path(app.package_dir) if c.sha == target_sha), None
        )
        target_title = (
            (target_commit.trailers.request or target_commit.subject) if target_commit is not None else None
        )
        git_repo.restore_path_to_commit(target_sha, app.package_dir)
        remaining_changes = git_repo.read_dirty_paths_under(app.package_dir)
        if len(remaining_changes) == 0:
            raise RestoreError("The app is already identical to that version")
        restore_trailers = serialize_trailer_block(
            TrailerBlock(
                app_name=app.name,
                request=_restore_request_title(target_title),
                kind=VersionKind.RESTORE,
                restored_from_sha=target_sha,
            )
        )
        restore_commit_sha = git_repo.commit_paths(
            app.package_dir,
            f"versioning: restore {app.name} to {target_sha[:10]}\n\n{restore_trailers}",
        )

    # Revive the app outside the lock: the commit is durable at this point.
    is_dependency_sync_run = pyproject_before != pyproject_target
    if is_dependency_sync_run:
        _sync_dependencies(git_repo.repo_root)
    is_service_restarted = False
    if is_service_managed and app.program is not None:
        restart_service(app.program)
        is_service_restarted = True
        _refresh_open_tab(git_repo.repo_root, app.name)
    return RestoreResult(
        restore_commit_sha=restore_commit_sha,
        is_service_restarted=is_service_restarted,
        is_dependency_sync_run=is_dependency_sync_run,
    )
