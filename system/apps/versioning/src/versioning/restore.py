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
from versioning.supervisor_config import SUPERVISORD_CONFIG_PATH
from versioning.supervisor_config import extract_program_block
from versioning.supervisor_config import replace_program_block
from versioning.trailers import serialize_trailer_block

_SUPERVISORCTL_TIMEOUT_SECONDS: Final[float] = 60.0
_UV_SYNC_TIMEOUT_SECONDS: Final[float] = 300.0

SELF_APP_NAME: Final[str] = "versioning"
UNRESTORABLE_APP_NAMES: Final[frozenset[str]] = frozenset({SELF_APP_NAME, "system-interface"})

_MAX_REQUEST_TITLE_CHARS: Final[int] = 60


@pure
def _restore_request_title(target_title: str | None) -> str:
    """The restore version's name: it names the version it went back to."""
    if target_title is None or not target_title.strip():
        return "Restored from an earlier version"
    title = target_title.strip()
    if len(title) > _MAX_REQUEST_TITLE_CHARS:
        title = title[: _MAX_REQUEST_TITLE_CHARS - 1] + "…"
    return f'Restored from "{title}"'


def _plan_startup_config_restore(
    git_repo: GitRepoInterface,
    app: AppRef,
    target_sha: str,
) -> str | None:
    """How an app was started is part of its version, so it has to travel back with the folder."""
    if app.program is None:
        return None
    config_path = git_repo.repo_root / SUPERVISORD_CONFIG_PATH
    if not config_path.exists():
        return None
    target_config_text = git_repo.read_file_at_commit(target_sha, SUPERVISORD_CONFIG_PATH)
    if target_config_text is None:
        return None
    target_block = extract_program_block(target_config_text, app.program)
    if target_block is None:
        logger.debug("Version {} of {} has no startup entry; keeping the current one", target_sha[:10], app.name)
        return None
    current_config_text = config_path.read_text()
    restored_config_text = replace_program_block(current_config_text, app.program, target_block)
    if restored_config_text is None:
        logger.warning("{} has no startup entry to restore into; leaving the config alone", app.name)
        return None
    if restored_config_text == current_config_text:
        return None
    return restored_config_text


def build_restore_preview(
    git_repo: GitRepoInterface,
    history: AppHistory,
    target_sha: str,
) -> RestorePreview:
    """Raises RestoreError if the target version is not in the app's history."""
    target_idx = next((idx for idx, node in enumerate(history.nodes) if node.sha == target_sha), None)
    if target_idx is None:
        raise RestoreError(f"No version '{target_sha}' for {history.app.name}")
    later_nodes = [node for node in history.nodes[target_idx + 1 :] if not node.is_set_aside]
    return RestorePreview(
        target_sha=target_sha,
        changed_file_count=git_repo.read_changed_file_count_between(target_sha, history.app.package_dir),
        set_aside_node_count=len(later_nodes),
        diff_stat=git_repo.read_diff_stat_between(target_sha, history.app.package_dir),
        is_startup_config_changed=_plan_startup_config_restore(git_repo, history.app, target_sha) is not None,
    )


def _run_supervisorctl(arguments: list[str], failure_description: str) -> None:
    """Raises RestoreError if supervisorctl reports failure."""
    completed = subprocess.run(
        ["supervisorctl"] + arguments,
        capture_output=True,
        text=True,
        timeout=_SUPERVISORCTL_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RestoreError(f"{failure_description}: {completed.stderr.strip() or completed.stdout.strip()}")


def _confirm_service_running(program: str) -> None:
    """Raises RestoreError if the service is not up after being brought back."""
    status = subprocess.run(
        ["supervisorctl", "status", program],
        capture_output=True,
        text=True,
        timeout=_SUPERVISORCTL_TIMEOUT_SECONDS,
    )
    if "RUNNING" not in status.stdout and "STARTING" not in status.stdout:
        raise RestoreError(f"{program} did not come back after restore: {status.stdout.strip()}")


def restart_service(program: str) -> None:
    """Raises RestoreError if the service does not come back RUNNING."""
    _run_supervisorctl(["restart", program], f"Could not restart {program}")
    _confirm_service_running(program)


def reload_service_definition(program: str) -> None:
    """Bring a service back onto a startup entry that just changed under it.

    A plain restart re-runs the old definition supervisord still holds in memory.
    """
    _run_supervisorctl(["reread"], "Could not re-read the service configuration")
    _run_supervisorctl(["update", program], f"Could not apply the restored startup settings for {program}")
    _confirm_service_running(program)


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
    """Restore the app as a new forward commit -- never a reset, so nothing is rewritten or lost."""
    if app.name in UNRESTORABLE_APP_NAMES:
        raise RestoreError(f"{app.title} can be browsed here but not restored")
    with operation_lock(lock_file):
        dirty_paths = git_repo.read_dirty_paths_under(app.package_dir)
        is_startup_config_dirty = app.program is not None and (
            len(git_repo.read_dirty_paths_under(SUPERVISORD_CONFIG_PATH)) > 0
        )
        if len(dirty_paths) > 0 or is_startup_config_dirty:
            logger.debug("Saving {} in-progress files before restore", len(dirty_paths))
            wip_trailers = serialize_trailer_block(
                TrailerBlock(
                    app_name=app.name,
                    request=f"Saved work in progress on {app.title}",
                    kind=VersionKind.CHANGE,
                )
            )
            saved_paths = [app.package_dir] + ([SUPERVISORD_CONFIG_PATH] if is_startup_config_dirty else [])
            git_repo.commit_paths(
                saved_paths,
                f"versioning: save work in progress on {app.name}\n\n{wip_trailers}",
            )

        pyproject_path = f"{app.package_dir}/pyproject.toml"
        pyproject_before = git_repo.read_file_at_commit("HEAD", pyproject_path)
        pyproject_target = git_repo.read_file_at_commit(target_sha, pyproject_path)

        target_commit = next(
            (c for c in git_repo.read_commits_touching_path(app.package_dir) if c.sha == target_sha), None
        )
        target_title = (
            (target_commit.trailers.request or target_commit.subject) if target_commit is not None else None
        )
        git_repo.restore_path_to_commit(target_sha, app.package_dir)

        restored_config_text = _plan_startup_config_restore(git_repo, app, target_sha)
        is_startup_config_restored = restored_config_text is not None
        if restored_config_text is not None:
            (git_repo.repo_root / SUPERVISORD_CONFIG_PATH).write_text(restored_config_text)

        remaining_changes = git_repo.read_dirty_paths_under(app.package_dir)
        if len(remaining_changes) == 0 and not is_startup_config_restored:
            raise RestoreError("The app is already identical to that version")
        restore_trailers = serialize_trailer_block(
            TrailerBlock(
                app_name=app.name,
                request=_restore_request_title(target_title),
                kind=VersionKind.RESTORE,
                restored_from_sha=target_sha,
            )
        )
        restored_paths = [app.package_dir] + ([SUPERVISORD_CONFIG_PATH] if is_startup_config_restored else [])
        restore_commit_sha = git_repo.commit_paths(
            restored_paths,
            f"versioning: restore {app.name} to {target_sha[:10]}\n\n{restore_trailers}",
        )

    is_dependency_sync_run = pyproject_before != pyproject_target
    if is_dependency_sync_run:
        _sync_dependencies(git_repo.repo_root)
    is_service_restarted = False
    if is_service_managed and app.program is not None:
        if is_startup_config_restored:
            reload_service_definition(app.program)
        else:
            restart_service(app.program)
        is_service_restarted = True
        _refresh_open_tab(git_repo.repo_root, app.name)
    return RestoreResult(
        restore_commit_sha=restore_commit_sha,
        is_service_restarted=is_service_restarted,
        is_dependency_sync_run=is_dependency_sync_run,
        is_startup_config_restored=is_startup_config_restored,
    )
