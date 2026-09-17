from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_witness.gitops import GitOperationError
from imbue.mngr_witness.gitops import branch_commits
from imbue.mngr_witness.gitops import changed_paths
from imbue.mngr_witness.gitops import checked_out_worktree
from imbue.mngr_witness.gitops import cherry_pick_branch
from imbue.mngr_witness.gitops import commit_kind
from imbue.mngr_witness.gitops import create_branch
from imbue.mngr_witness.gitops import file_text_at


def _git(cg: ConcurrencyGroup, repo: Path, *args: str) -> str:
    return cg.run_process_to_completion(["git", *args], cwd=repo).stdout.strip()


def _commit_file(cg: ConcurrencyGroup, repo: Path, relative_path: str, text: str, subject: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(cg, repo, "add", relative_path)
    _git(cg, repo, "commit", "--quiet", "-m", subject)
    return _git(cg, repo, "rev-parse", "HEAD")


@pytest.fixture
def branch_with_two_commits(temp_git_repo: Path, cg: ConcurrencyGroup) -> tuple[Path, str, str]:
    """A repo whose branch adds a test file, then a changelog entry, on top of the initial commit."""
    base = _git(cg, temp_git_repo, "rev-parse", "HEAD")
    branch = f"agents/{uuid4().hex}"
    _git(cg, temp_git_repo, "checkout", "--quiet", "-b", branch)
    _commit_file(
        cg,
        temp_git_repo,
        "pkg/thing_test.py",
        "def test_thing() -> None:\n    assert 1 == 1\n",
        "[CREATE_TEST] area.a: witness it",
    )
    _commit_file(cg, temp_git_repo, "libs/pkg/changelog/entry.md", "Add a thing.\n", "[CHANGELOG] entry")
    _git(cg, temp_git_repo, "checkout", "--quiet", "-")
    return temp_git_repo, base, branch


def test_changed_paths_lists_what_the_branch_touched_since_its_base(
    branch_with_two_commits: tuple[Path, str, str], cg: ConcurrencyGroup
) -> None:
    repo, base, branch = branch_with_two_commits

    assert changed_paths(cg, repo, base, branch) == ["libs/pkg/changelog/entry.md", "pkg/thing_test.py"]
    assert changed_paths(cg, repo, base, branch, under=Path("libs/pkg")) == ["libs/pkg/changelog/entry.md"]


def test_branch_commits_carry_their_kind_and_touched_paths_oldest_first(
    branch_with_two_commits: tuple[Path, str, str], cg: ConcurrencyGroup
) -> None:
    repo, base, branch = branch_with_two_commits

    commits = branch_commits(cg, repo, base, branch)

    assert [(commit.kind, commit.touched_paths) for commit in commits] == [
        ("CREATE_TEST", ("pkg/thing_test.py",)),
        ("CHANGELOG", ("libs/pkg/changelog/entry.md",)),
    ]


@pytest.mark.parametrize(
    ("subject", "expected_kind"),
    [
        ("[FIX_IMPL] make it so", "FIX_IMPL"),
        ("[REVIEW] area.a: trim", "REVIEW"),
        ("no kind here", None),
        ("[lower] no", None),
    ],
)
def test_commit_kind_reads_the_bracketed_prefix(subject: str, expected_kind: str | None) -> None:
    assert commit_kind(subject) == expected_kind


def test_file_text_at_reads_a_branch_tip_without_checking_it_out(
    branch_with_two_commits: tuple[Path, str, str], cg: ConcurrencyGroup
) -> None:
    repo, _base, branch = branch_with_two_commits

    assert file_text_at(cg, repo, branch, "pkg/thing_test.py") == "def test_thing() -> None:\n    assert 1 == 1\n"
    assert file_text_at(cg, repo, branch, "pkg/missing.py") is None


def test_checked_out_worktree_exposes_the_branch_tip_and_is_removed_afterwards(
    branch_with_two_commits: tuple[Path, str, str], cg: ConcurrencyGroup
) -> None:
    repo, _base, branch = branch_with_two_commits

    with checked_out_worktree(cg, repo, branch) as worktree:
        assert (worktree / "pkg" / "thing_test.py").exists()
        assert str(worktree) in _git(cg, repo, "worktree", "list")
        kept = worktree
    assert not kept.exists()
    assert str(kept) not in _git(cg, repo, "worktree", "list")


def test_git_failures_surface_as_git_operation_errors(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    with pytest.raises(GitOperationError, match="merge-base"):
        changed_paths(cg, temp_git_repo, "no-such-ref", "HEAD")


def test_create_branch_points_at_the_ref_without_checking_it_out(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    branch = f"integrated/{uuid4().hex}"
    before = _git(cg, temp_git_repo, "rev-parse", "--abbrev-ref", "HEAD")

    create_branch(cg, temp_git_repo, branch, "HEAD")

    assert _git(cg, temp_git_repo, "rev-parse", branch) == _git(cg, temp_git_repo, "rev-parse", "HEAD")
    assert _git(cg, temp_git_repo, "rev-parse", "--abbrev-ref", "HEAD") == before
    with pytest.raises(GitOperationError, match="branch"):
        create_branch(cg, temp_git_repo, branch, "HEAD")


def test_cherry_pick_branch_applies_clean_commits_and_reports_conflicts(
    temp_git_repo: Path, cg: ConcurrencyGroup
) -> None:
    base = _git(cg, temp_git_repo, "rev-parse", "HEAD")
    clean = f"agents/{uuid4().hex}"
    conflicting = f"agents/{uuid4().hex}"
    _git(cg, temp_git_repo, "checkout", "--quiet", "-b", clean)
    _commit_file(cg, temp_git_repo, "pkg/a_test.py", "def test_a() -> None:\n    pass\n", "[CREATE_TEST] a")
    _git(cg, temp_git_repo, "checkout", "--quiet", "-b", conflicting, base)
    _commit_file(cg, temp_git_repo, "pkg/shared.py", "VALUE = 1\n", "[FIX_IMPL] shared one way")
    _git(cg, temp_git_repo, "checkout", "--quiet", base)
    integration = f"integrated/{uuid4().hex}"
    create_branch(cg, temp_git_repo, integration, base)
    _git(cg, temp_git_repo, "checkout", "--quiet", integration)
    _commit_file(cg, temp_git_repo, "pkg/shared.py", "VALUE = 2\n", "[FIX_IMPL] shared the other way")
    _git(cg, temp_git_repo, "checkout", "--quiet", base)

    clean_result = cherry_pick_branch(cg, temp_git_repo, integration, base, clean)
    conflict_result = cherry_pick_branch(cg, temp_git_repo, integration, base, conflicting)

    assert clean_result == []
    assert conflict_result == ["pkg/shared.py"]
    assert file_text_at(cg, temp_git_repo, integration, "pkg/a_test.py") is not None
    assert file_text_at(cg, temp_git_repo, integration, "pkg/shared.py") == "VALUE = 2\n"
    assert _git(cg, temp_git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_cherry_pick_branch_with_nothing_to_pick_is_a_no_op(temp_git_repo: Path, cg: ConcurrencyGroup) -> None:
    base = _git(cg, temp_git_repo, "rev-parse", "HEAD")
    integration = f"integrated/{uuid4().hex}"
    create_branch(cg, temp_git_repo, integration, base)
    same_as_base = f"agents/{uuid4().hex}"
    create_branch(cg, temp_git_repo, same_as_base, base)

    assert cherry_pick_branch(cg, temp_git_repo, integration, base, same_as_base) == []
    assert _git(cg, temp_git_repo, "rev-parse", integration) == base


def test_a_worktree_that_cannot_be_removed_is_left_with_a_warning_not_an_error(
    branch_with_two_commits: tuple[Path, str, str], cg: ConcurrencyGroup
) -> None:
    repo, _base, branch = branch_with_two_commits

    with checked_out_worktree(cg, repo, branch) as worktree:
        _git(cg, repo, "worktree", "lock", str(worktree))
        locked = worktree

    assert str(locked) in _git(cg, repo, "worktree", "list")
    worktrees_dir = Path(_git(cg, repo, "rev-parse", "--git-path", "worktrees"))
    for lock_file in (repo / worktrees_dir).glob("*/locked"):
        lock_file.unlink()
    _git(cg, repo, "worktree", "prune")
    assert str(locked) not in _git(cg, repo, "worktree", "list")
