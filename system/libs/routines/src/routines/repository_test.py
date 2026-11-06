import subprocess
from pathlib import Path

import pytest

from routines.errors import DefinitionCommitError
from routines.mock_clock_test import FakeClock
from routines.paths import RoutinesPaths
from routines.repository import COULD_NOT_SAVE_MESSAGE
from routines.repository import GitRepository


def _log(paths: RoutinesPaths) -> list[str]:
    output = subprocess.run(
        ["git", "log", "--format=%s", "--name-only"], cwd=paths.repo_root, capture_output=True, text=True
    )
    return [line for line in output.stdout.splitlines() if line]


def test_a_commit_takes_only_the_named_path_and_leaves_other_staged_work_alone(
    git_paths: RoutinesPaths, git_repository: GitRepository, written_definition: Path
) -> None:
    other = git_paths.repo_root / "someone-elses.txt"
    other.write_text("staged by another agent\n")
    subprocess.run(["git", "add", "someone-elses.txt"], cwd=git_paths.repo_root, check=True)

    git_repository.commit_path(written_definition, "routines: add digest")

    assert _log(git_paths)[:2] == ["routines: add digest", "system/routines/digest.toml"]
    status = subprocess.run(
        ["git", "status", "--short"], cwd=git_paths.repo_root, capture_output=True, text=True
    ).stdout
    assert "A  someone-elses.txt" in status


def test_a_deletion_is_committed_the_same_way(
    git_paths: RoutinesPaths, git_repository: GitRepository, written_definition: Path
) -> None:
    git_repository.commit_path(written_definition, "routines: add digest")
    written_definition.unlink()

    git_repository.commit_path(written_definition, "routines: remove digest")

    assert _log(git_paths)[0] == "routines: remove digest"


def test_a_path_with_nothing_to_commit_is_not_a_failure(
    git_paths: RoutinesPaths, git_repository: GitRepository
) -> None:
    git_repository.commit_path(git_paths.repo_root / "README.md", "routines: nothing")
    assert _log(git_paths)[0] == "init"


def test_removing_a_written_definition_git_never_knew_commits_nothing_and_raises_nothing(
    git_paths: RoutinesPaths, git_repository: GitRepository, written_definition: Path
) -> None:
    written_definition.unlink()

    git_repository.commit_path(written_definition, "routines: remove digest")

    assert _log(git_paths)[0] == "init"


def test_a_held_index_lock_is_retried_once_and_then_reported_in_the_users_words(
    git_paths: RoutinesPaths, git_repository: GitRepository, written_definition: Path, clock: FakeClock
) -> None:
    (git_paths.repo_root / ".git" / "index.lock").write_text("")

    with pytest.raises(DefinitionCommitError, match=COULD_NOT_SAVE_MESSAGE[:20]):
        git_repository.commit_path(written_definition, "routines: add digest")

    assert clock.pauses == [1.0]


def test_a_failure_that_is_not_another_writer_is_reported_in_gits_words(
    paths: RoutinesPaths, clock: FakeClock
) -> None:
    """A root that is no repository at all is not a transient collision: no retry, and the user is not told to."""
    repository = GitRepository(repo_root=paths.repo_root, clock=clock)
    definition = paths.definitions_dir / "digest.toml"
    definition.write_text('title = "x"\n')

    with pytest.raises(DefinitionCommitError, match="not a git repository") as refused:
        repository.commit_path(definition, "routines: add digest")

    assert "something else is saving" not in str(refused.value)
    assert clock.pauses == []
