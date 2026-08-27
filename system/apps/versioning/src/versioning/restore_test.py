from collections.abc import Callable
from pathlib import Path

import pytest

from versioning.data_types import AppRef
from versioning.data_types import RestoreError
from versioning.data_types import VersionKind
from versioning.git_repo import SubprocessGitRepo
from versioning.history import build_app_history
from versioning.restore import build_restore_preview
from versioning.restore import perform_restore

_NEWS_APP = AppRef(name="news", package_dir="system/apps/news", title="News", program=None)


def test_perform_restore_creates_trailered_commit_and_sets_version_aside(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    target_sha = commit_app_file("news", "runner.py", "v1", "news: first build")
    commit_app_file("news", "runner.py", "v2", "news: second version")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    result = perform_restore(
        git_repo=repo,
        app=_NEWS_APP,
        target_sha=target_sha,
        lock_file=tmp_path / "restore.lock",
        is_service_managed=False,
    )

    # The folder matches the target version again, recorded as a new commit.
    assert (scratch_repo / "system/apps/news/runner.py").read_text() == "v1"
    assert not result.is_service_restarted
    history = build_app_history(repo, _NEWS_APP)
    assert len(history.nodes) == 3
    restore_node = history.nodes[-1]
    assert restore_node.sha == result.restore_commit_sha
    assert restore_node.kind == VersionKind.RESTORE
    assert restore_node.restored_from_sha == target_sha
    assert restore_node.is_current
    assert history.nodes[1].is_set_aside
    # The restore version is named after the version it went back to.
    assert restore_node.raw_title == 'Restored from "news: first build"'


def test_perform_restore_saves_work_in_progress_as_its_own_version(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    target_sha = commit_app_file("news", "runner.py", "v1", "news: first build")
    commit_app_file("news", "runner.py", "v2", "news: second version")
    (scratch_repo / "system/apps/news/runner.py").write_text("uncommitted edits")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    perform_restore(
        git_repo=repo,
        app=_NEWS_APP,
        target_sha=target_sha,
        lock_file=tmp_path / "restore.lock",
        is_service_managed=False,
    )

    history = build_app_history(repo, _NEWS_APP)
    # first build, second version, the WIP save, then the restore.
    assert len(history.nodes) == 4
    wip_node = history.nodes[2]
    assert wip_node.raw_title == "Saved work in progress on News"
    assert wip_node.is_set_aside
    assert (scratch_repo / "system/apps/news/runner.py").read_text() == "v1"


def test_perform_restore_to_identical_version_fails_loudly(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    target_sha = commit_app_file("news", "runner.py", "v1", "news: first build")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    with pytest.raises(RestoreError, match="already identical"):
        perform_restore(
            git_repo=repo,
            app=_NEWS_APP,
            target_sha=target_sha,
            lock_file=tmp_path / "restore.lock",
            is_service_managed=False,
        )


def test_browse_only_apps_cannot_be_restored(scratch_repo: Path, tmp_path: Path) -> None:
    repo = SubprocessGitRepo(repo_root=scratch_repo)
    browse_only = [
        AppRef(name="versioning", package_dir="system/apps/versioning", title="Versioning", program=None),
        AppRef(name="system-interface", package_dir="system/apps/system_interface", title="System", program=None),
    ]

    for app in browse_only:
        with pytest.raises(RestoreError, match="browsed here but not restored"):
            perform_restore(
                git_repo=repo,
                app=app,
                target_sha="a" * 40,
                lock_file=tmp_path / "restore.lock",
                is_service_managed=False,
            )


def test_build_restore_preview_counts_files_and_later_versions(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    target_sha = commit_app_file("news", "runner.py", "v1", "news: first build")
    commit_app_file("news", "extra.py", "more", "news: second version")
    repo = SubprocessGitRepo(repo_root=scratch_repo)
    history = build_app_history(repo, _NEWS_APP)

    preview = build_restore_preview(repo, history, target_sha)

    assert preview.target_sha == target_sha
    assert preview.changed_file_count == 1
    assert preview.set_aside_node_count == 1
    assert "extra.py" in preview.diff_stat


def test_build_restore_preview_rejects_unknown_version(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    commit_app_file("news", "runner.py", "v1", "news: first build")
    repo = SubprocessGitRepo(repo_root=scratch_repo)
    history = build_app_history(repo, _NEWS_APP)

    with pytest.raises(RestoreError, match="No version"):
        build_restore_preview(repo, history, "d" * 40)
