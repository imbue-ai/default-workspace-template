from collections.abc import Callable
from pathlib import Path

import pytest

from versioning.conftest import run_git
from versioning.data_types import ChangeKind
from versioning.data_types import GitReadError
from versioning.git_repo import SubprocessGitRepo


def test_read_commits_touching_path_returns_only_app_commits_oldest_first(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    first_sha = commit_app_file("news", "runner.py", "v1", "news: first build")
    (scratch_repo / "unrelated.txt").write_text("x")
    run_git(scratch_repo, ["add", "-A"])
    run_git(scratch_repo, ["commit", "-q", "-m", "unrelated change"])
    second_sha = commit_app_file("news", "runner.py", "v2", "news: second version")

    commits = SubprocessGitRepo(repo_root=scratch_repo).read_commits_touching_path("system/apps/news")

    assert [c.sha for c in commits] == [first_sha, second_sha]
    assert commits[0].subject == "news: first build"


def test_read_commits_touching_path_skips_merged_branch_commits(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    commit_app_file("news", "runner.py", "v1", "news: first build")
    run_git(scratch_repo, ["checkout", "-q", "-b", "side"])
    side_sha = commit_app_file("news", "extra.py", "side work", "news: side branch change")
    run_git(scratch_repo, ["checkout", "-q", "main"])
    run_git(scratch_repo, ["merge", "-q", "--no-ff", "-m", "merge side", "side"])
    merge_sha = run_git(scratch_repo, ["rev-parse", "HEAD"]).strip()

    commits = SubprocessGitRepo(repo_root=scratch_repo).read_commits_touching_path("system/apps/news")

    shas = [c.sha for c in commits]
    assert side_sha not in shas
    assert merge_sha in shas


def test_restore_path_to_commit_removes_files_added_after_the_target(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    target_sha = commit_app_file("news", "runner.py", "v1", "news: first build")
    commit_app_file("news", "added_later.py", "later", "news: add a second file")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    repo.restore_path_to_commit(target_sha, "system/apps/news")

    assert (scratch_repo / "system/apps/news/runner.py").read_text() == "v1"
    assert not (scratch_repo / "system/apps/news/added_later.py").exists()


def test_commit_paths_records_only_the_given_paths(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    commit_app_file("news", "runner.py", "v1", "news: first build")
    (scratch_repo / "system/apps/news/runner.py").write_text("v2")
    (scratch_repo / "outside.txt").write_text("keep me uncommitted")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    new_sha = repo.commit_paths(["system/apps/news"], "news: edit")

    assert repo.read_dirty_paths_under("system/apps/news") == []
    status = run_git(scratch_repo, ["status", "--porcelain"])
    assert "outside.txt" in status
    assert new_sha == run_git(scratch_repo, ["rev-parse", "HEAD"]).strip()


def test_commit_paths_records_several_paths_as_one_commit(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    commit_app_file("news", "runner.py", "v1", "news: first build")
    (scratch_repo / "system/supervisord.conf").write_text("[program:news]\ncommand=run\n")
    (scratch_repo / "system/apps/news/runner.py").write_text("v2")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    repo.commit_paths(["system/apps/news", "system/supervisord.conf"], "news: edit with its startup entry")

    committed_files = run_git(scratch_repo, ["show", "--name-only", "--format=", "HEAD"])
    assert "system/apps/news/runner.py" in committed_files
    assert "system/supervisord.conf" in committed_files


def test_read_diff_of_commits_covers_the_full_span(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    first_sha = commit_app_file("news", "runner.py", "v1", "news: first build")

    diff = SubprocessGitRepo(repo_root=scratch_repo).read_diff_of_commits([first_sha], "system/apps/news")

    assert "+v1" in diff


def test_read_file_at_commit_returns_none_for_missing_file(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    sha = commit_app_file("news", "runner.py", "v1", "news: first build")
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    assert repo.read_file_at_commit(sha, "system/apps/news/runner.py") == "v1"
    assert repo.read_file_at_commit(sha, "system/apps/news/nope.py") is None


def test_run_git_raises_git_read_error_on_bad_revision(scratch_repo: Path) -> None:
    repo = SubprocessGitRepo(repo_root=scratch_repo)

    with pytest.raises(GitReadError):
        repo.read_diff_stat_between("not-a-sha", "system/apps/news")


def test_read_file_changes_of_commit_classifies_added_edited_removed(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    commit_app_file("news", "keep.py", "v1", "news: first build")
    commit_app_file("news", "gone.py", "bye", "news: add a file")
    (scratch_repo / "system/apps/news/keep.py").write_text("v2")
    (scratch_repo / "system/apps/news/gone.py").unlink()
    (scratch_repo / "system/apps/news/new.py").write_text("hello")
    run_git(scratch_repo, ["add", "-A"])
    run_git(scratch_repo, ["commit", "-q", "-m", "news: rework files"])
    sha = run_git(scratch_repo, ["rev-parse", "HEAD"]).strip()

    changes = SubprocessGitRepo(repo_root=scratch_repo).read_file_changes_of_commit(sha, "system/apps/news")

    kind_by_path = {c.display_path: c.change_kind for c in changes}
    assert kind_by_path == {
        "keep.py": ChangeKind.EDITED,
        "gone.py": ChangeKind.REMOVED,
        "new.py": ChangeKind.ADDED,
    }


def test_read_change_stats_by_sha_counts_each_commits_churn(
    scratch_repo: Path, commit_app_file: Callable[[str, str, str, str], str]
) -> None:
    first = commit_app_file("news", "app.py", "one\ntwo\nthree\n", "news: first build")
    second = commit_app_file("news", "extra.py", "a\nb\n", "news: add a page")

    stats = SubprocessGitRepo(repo_root=scratch_repo).read_change_stats_by_sha("system/apps/news")

    assert stats[first].files_changed == 1
    assert stats[first].lines_changed == 3
    assert stats[second].files_changed == 1
    assert stats[second].lines_changed == 2
