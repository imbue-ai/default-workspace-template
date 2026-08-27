from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path

import pytest

from versioning.data_types import AppRef
from versioning.data_types import RestoreError
from versioning.data_types import VersionKind
from versioning.git_repo import SubprocessGitRepo
from versioning.history import build_app_history
from versioning.restore import build_restore_preview
from versioning.restore import perform_restore
from versioning.supervisor_config import SUPERVISORD_CONFIG_PATH

_NEWS_APP = AppRef(name="news", package_dir="system/apps/news", title="News", program=None)

_SUPERVISED_NEWS_APP = AppRef(name="news", package_dir="system/apps/news", title="News", program="news")

_NEWS_FILE = "system/apps/news/runner.py"


def _config_with_news_command(news_command: str) -> str:
    return (
        "[supervisord]\nnodaemon=true\n\n"
        f"[program:news]\ncommand={news_command}\ndirectory=/home/user/workspace\n\n"
        "[program:weather]\ncommand=uv run weather\n"
    )


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


def test_perform_restore_takes_the_app_startup_entry_back_with_the_folder(
    scratch_repo: Path, commit_repo_files: Callable[[Mapping[str, str], str], str], tmp_path: Path
) -> None:
    target_sha = commit_repo_files(
        {_NEWS_FILE: "v1", SUPERVISORD_CONFIG_PATH: _config_with_news_command("uv run news")},
        "news: first build",
    )
    commit_repo_files(
        {
            _NEWS_FILE: "v2",
            "system/apps/news/icon.svg": "<svg/>",
            SUPERVISORD_CONFIG_PATH: _config_with_news_command("uv run news --icon-file icon.svg"),
        },
        "news: an icon",
    )
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    result = perform_restore(
        git_repo=repo,
        app=_SUPERVISED_NEWS_APP,
        target_sha=target_sha,
        lock_file=tmp_path / "restore.lock",
        is_service_managed=False,
    )

    config_text = (scratch_repo / SUPERVISORD_CONFIG_PATH).read_text()
    assert "command=uv run news\n" in config_text
    assert "--icon-file" not in config_text
    assert result.is_startup_config_restored
    assert "[program:weather]\ncommand=uv run weather\n" in config_text
    committed_files = repo.read_diff_of_commits([result.restore_commit_sha], SUPERVISORD_CONFIG_PATH)
    assert "supervisord.conf" in committed_files


def test_perform_restore_keeps_todays_startup_entry_when_the_version_had_none(
    scratch_repo: Path, commit_repo_files: Callable[[Mapping[str, str], str], str], tmp_path: Path
) -> None:
    target_sha = commit_repo_files({_NEWS_FILE: "v1"}, "news: first build")
    commit_repo_files(
        {_NEWS_FILE: "v2", SUPERVISORD_CONFIG_PATH: _config_with_news_command("uv run news")},
        "news: start being supervised",
    )
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    result = perform_restore(
        git_repo=repo,
        app=_SUPERVISED_NEWS_APP,
        target_sha=target_sha,
        lock_file=tmp_path / "restore.lock",
        is_service_managed=False,
    )

    assert not result.is_startup_config_restored
    assert "command=uv run news\n" in (scratch_repo / SUPERVISORD_CONFIG_PATH).read_text()


def test_perform_restore_proceeds_when_only_the_startup_entry_differs(
    scratch_repo: Path, commit_repo_files: Callable[[Mapping[str, str], str], str], tmp_path: Path
) -> None:
    target_sha = commit_repo_files(
        {_NEWS_FILE: "v1", SUPERVISORD_CONFIG_PATH: _config_with_news_command("uv run news")},
        "news: first build",
    )
    commit_repo_files(
        {SUPERVISORD_CONFIG_PATH: _config_with_news_command("uv run news --broken")},
        "news: change how it starts",
    )
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    result = perform_restore(
        git_repo=repo,
        app=_SUPERVISED_NEWS_APP,
        target_sha=target_sha,
        lock_file=tmp_path / "restore.lock",
        is_service_managed=False,
    )

    assert result.is_startup_config_restored
    assert "--broken" not in (scratch_repo / SUPERVISORD_CONFIG_PATH).read_text()


def test_build_restore_preview_reports_a_changed_startup_entry(
    scratch_repo: Path, commit_repo_files: Callable[[Mapping[str, str], str], str]
) -> None:
    target_sha = commit_repo_files(
        {_NEWS_FILE: "v1", SUPERVISORD_CONFIG_PATH: _config_with_news_command("uv run news")},
        "news: first build",
    )
    commit_repo_files(
        {_NEWS_FILE: "v2", SUPERVISORD_CONFIG_PATH: _config_with_news_command("uv run news --icon-file icon.svg")},
        "news: an icon",
    )
    repo = SubprocessGitRepo(repo_root=scratch_repo)
    history = build_app_history(repo, _SUPERVISED_NEWS_APP)

    preview = build_restore_preview(repo, history, target_sha)

    assert preview.is_startup_config_changed
    assert "--icon-file" in (scratch_repo / SUPERVISORD_CONFIG_PATH).read_text()


def test_build_restore_preview_rejects_unknown_version(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    commit_app_file("news", "runner.py", "v1", "news: first build")
    repo = SubprocessGitRepo(repo_root=scratch_repo)
    history = build_app_history(repo, _NEWS_APP)

    with pytest.raises(RestoreError, match="No version"):
        build_restore_preview(repo, history, "d" * 40)
