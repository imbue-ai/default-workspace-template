from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from versioning.assistant import AssistError
from versioning.assistant import perform_assist
from versioning.data_types import AppRef
from versioning.data_types import CommitRecord
from versioning.data_types import VersionKind
from versioning.git_repo import SubprocessGitRepo
from versioning.history import build_app_history
from versioning.trailers import parse_trailer_block

_NEWS_APP = AppRef(name="news", package_dir="system/apps/news", title="News", program=None)


def _commit_record(sha: str) -> CommitRecord:
    return CommitRecord(
        sha=sha,
        author="Chat-Test",
        authored_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        subject="news: first build",
        body="",
        trailers=parse_trailer_block("news: first build"),
    )


def test_perform_assist_answers_without_committing_when_nothing_changed(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    sha = commit_app_file("news", "banner.py", "old banner", "news: first build")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    outcome = perform_assist(
        git_repo=repo,
        app=_NEWS_APP,
        version_sha=sha,
        commit=_commit_record(sha),
        summary=None,
        prior_exchanges=[],
        message="What was in this version?",
        is_change_allowed=True,
        lock_file=tmp_path / "restore.lock",
        task_runner=lambda prompt, allowed: "It had a welcome banner at the top.",
    )

    assert outcome.answer == "It had a welcome banner at the top."
    assert outcome.new_version_sha is None
    assert len(build_app_history(repo, _NEWS_APP).nodes) == 1


def test_perform_assist_commits_agent_change_as_ported_version(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    source_sha = commit_app_file("news", "banner.py", "old banner", "news: first build")
    commit_app_file("news", "runner.py", "current code", "news: second version")

    def fake_task_runner(prompt: str, is_change_allowed: bool) -> str:
        assert is_change_allowed
        assert source_sha in prompt
        (scratch_repo / "system/apps/news/banner.py").write_text("old banner, adapted")
        return "Brought the banner back for you.\nCHANGE-NOTE: The welcome banner is back"

    repo = SubprocessGitRepo(repo_root=scratch_repo)
    outcome = perform_assist(
        git_repo=repo,
        app=_NEWS_APP,
        version_sha=source_sha,
        commit=_commit_record(source_sha),
        summary=None,
        prior_exchanges=[],
        message="bring back the welcome banner",
        is_change_allowed=True,
        lock_file=tmp_path / "restore.lock",
        task_runner=fake_task_runner,
    )

    assert outcome.answer == "Brought the banner back for you."
    history = build_app_history(repo, _NEWS_APP)
    new_node = history.nodes[-1]
    assert new_node.sha == outcome.new_version_sha
    assert new_node.kind == VersionKind.PORT
    assert new_node.ported_from_sha == source_sha
    assert new_node.raw_title == "The welcome banner is back"
    assert all(not node.is_set_aside for node in history.nodes)


def test_perform_assist_reverts_changes_made_to_browse_only_app(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    sha = commit_app_file("news", "banner.py", "current banner", "news: first build")

    def misbehaving_task_runner(prompt: str, is_change_allowed: bool) -> str:
        (scratch_repo / "system/apps/news/banner.py").write_text("should not persist")
        return "I changed it anyway."

    repo = SubprocessGitRepo(repo_root=scratch_repo)
    outcome = perform_assist(
        git_repo=repo,
        app=_NEWS_APP,
        version_sha=sha,
        commit=_commit_record(sha),
        summary=None,
        prior_exchanges=[],
        message="change something",
        is_change_allowed=False,
        lock_file=tmp_path / "restore.lock",
        task_runner=misbehaving_task_runner,
    )

    assert outcome.new_version_sha is None
    assert (scratch_repo / "system/apps/news/banner.py").read_text() == "current banner"
    assert len(build_app_history(repo, _NEWS_APP).nodes) == 1


def test_perform_assist_reverts_partial_edits_when_the_task_fails(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    sha = commit_app_file("news", "banner.py", "current banner", "news: first build")

    def failing_task_runner(prompt: str, is_change_allowed: bool) -> str:
        (scratch_repo / "system/apps/news/banner.py").write_text("half-finished edit")
        raise AssistError("the agent died")

    repo = SubprocessGitRepo(repo_root=scratch_repo)
    with pytest.raises(AssistError, match="agent died"):
        perform_assist(
            git_repo=repo,
            app=_NEWS_APP,
            version_sha=sha,
            commit=_commit_record(sha),
            summary=None,
            prior_exchanges=[],
            message="bring back the banner",
            is_change_allowed=True,
            lock_file=tmp_path / "restore.lock",
            task_runner=failing_task_runner,
        )

    assert (scratch_repo / "system/apps/news/banner.py").read_text() == "current banner"
    assert len(build_app_history(repo, _NEWS_APP).nodes) == 1


def test_perform_assist_refuses_when_app_has_unsaved_edits(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str], tmp_path: Path
) -> None:
    sha = commit_app_file("news", "banner.py", "current banner", "news: first build")
    (scratch_repo / "system/apps/news/banner.py").write_text("dirty")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    with pytest.raises(AssistError, match="unsaved edits"):
        perform_assist(
            git_repo=repo,
            app=_NEWS_APP,
            version_sha=sha,
            commit=_commit_record(sha),
            summary=None,
            prior_exchanges=[],
            message="hello",
            is_change_allowed=True,
            lock_file=tmp_path / "restore.lock",
            task_runner=lambda prompt, allowed: "hi",
        )
