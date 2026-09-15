import re
import tempfile
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError

_KIND_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[([A-Z_]+)\]")
_GIT_TIMEOUT_SECONDS: Final[float] = 120.0


class GitOperationError(MngrError, RuntimeError):
    """Raised when a git command the pipeline relies on fails."""

    ...


class BranchCommit(FrozenModel):
    """One commit a branch added on top of its base, with the change kind its subject declares."""

    commit_hash: str = Field(description="The full commit hash")
    subject: str = Field(description="The first line of the commit message")
    kind: str | None = Field(description="The bracketed kind the subject starts with, or None when it has none")
    touched_paths: tuple[str, ...] = Field(description="Every path the commit changed, relative to the repo root")


def _git(cg: ConcurrencyGroup, repo: Path, args: Sequence[str]) -> str:
    """Run git in the repo and return its stdout; raises GitOperationError on a non-zero exit."""
    result = cg.run_process_to_completion(
        ["git", *args], cwd=repo, timeout=_GIT_TIMEOUT_SECONDS, is_checked_after=False
    )
    if result.returncode != 0:
        raise GitOperationError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def merge_base(cg: ConcurrencyGroup, repo: Path, base_ref: str, branch: str) -> str:
    return _git(cg, repo, ["merge-base", base_ref, branch]).strip()


def changed_paths(
    cg: ConcurrencyGroup, repo: Path, base_ref: str, branch: str, *, under: Path | None = None
) -> list[str]:
    """Paths that differ between the merge base of the two refs and the branch tip, optionally only under a directory."""
    base = merge_base(cg, repo, base_ref, branch)
    scope = ["--", str(under)] if under is not None else []
    return [line for line in _git(cg, repo, ["diff", "--name-only", f"{base}..{branch}", *scope]).splitlines() if line]


def branch_commits(cg: ConcurrencyGroup, repo: Path, base_ref: str, branch: str) -> list[BranchCommit]:
    """The commits the branch added on top of the base, oldest first, each with its kind and touched paths."""
    base = merge_base(cg, repo, base_ref, branch)
    hashes = [line for line in _git(cg, repo, ["rev-list", "--reverse", f"{base}..{branch}"]).splitlines() if line]
    commits: list[BranchCommit] = []
    for commit_hash in hashes:
        subject = _git(cg, repo, ["log", "-1", "--format=%s", commit_hash]).strip()
        touched = [
            line
            for line in _git(cg, repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash]).splitlines()
            if line
        ]
        commits.append(
            BranchCommit(
                commit_hash=commit_hash, subject=subject, kind=commit_kind(subject), touched_paths=tuple(touched)
            )
        )
    return commits


@pure
def commit_kind(subject: str) -> str | None:
    """The bracketed kind a commit subject starts with, ``[CREATE_TEST] ...`` giving ``CREATE_TEST``."""
    match = _KIND_PREFIX_PATTERN.match(subject)
    return match.group(1) if match is not None else None


def file_text_at(cg: ConcurrencyGroup, repo: Path, ref: str, path: str) -> str | None:
    """The content of a file at a ref, or None when the file does not exist there."""
    result = cg.run_process_to_completion(
        ["git", "show", f"{ref}:{path}"], cwd=repo, timeout=_GIT_TIMEOUT_SECONDS, is_checked_after=False
    )
    if result.returncode != 0:
        return None
    return result.stdout


def create_branch(cg: ConcurrencyGroup, repo: Path, branch: str, start_ref: str) -> None:
    """Create a branch at a ref without checking it out; fails when the branch already exists."""
    _git(cg, repo, ["branch", branch, start_ref])


def cherry_pick_branch(cg: ConcurrencyGroup, repo: Path, onto_branch: str, base_ref: str, branch: str) -> list[str]:
    """Cherry-pick the commits a branch added since its base onto another branch, in a worktree of its own.

    Returns the conflicting paths of the first commit that did not apply, after
    aborting, so the target branch is left as it was before that commit; an empty
    list means every commit applied.
    """
    base = merge_base(cg, repo, base_ref, branch)
    if not _git(cg, repo, ["rev-list", f"{base}..{branch}"]).strip():
        return []
    with checked_out_worktree(cg, repo, onto_branch, is_detached=False) as worktree:
        result = cg.run_process_to_completion(
            ["git", "cherry-pick", f"{base}..{branch}"],
            cwd=worktree,
            timeout=_GIT_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
        if result.returncode == 0:
            return []
        conflicting = [
            line for line in _git(cg, worktree, ["diff", "--name-only", "--diff-filter=U"]).splitlines() if line
        ]
        if _is_cherry_pick_in_progress(cg, worktree):
            _git(cg, worktree, ["cherry-pick", "--abort"])
        return conflicting or [f"(cherry-pick failed without a conflict: {result.stderr.strip()})"]


def _is_cherry_pick_in_progress(cg: ConcurrencyGroup, worktree: Path) -> bool:
    result = cg.run_process_to_completion(
        ["git", "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"],
        cwd=worktree,
        timeout=_GIT_TIMEOUT_SECONDS,
        is_checked_after=False,
    )
    return result.returncode == 0


@contextmanager
def checked_out_worktree(cg: ConcurrencyGroup, repo: Path, ref: str, *, is_detached: bool = True) -> Iterator[Path]:
    """A temporary worktree of the repo at a ref, removed on exit; detached for reading, attached to a branch for committing."""
    with tempfile.TemporaryDirectory(prefix="witness-worktree-") as temp_dir:
        worktree = Path(temp_dir) / "tree"
        add_args = (
            ["worktree", "add", "--detach", str(worktree), ref]
            if is_detached
            else ["worktree", "add", str(worktree), ref]
        )
        _git(cg, repo, add_args)
        try:
            yield worktree
        finally:
            try:
                _git(cg, repo, ["worktree", "remove", "--force", str(worktree)])
            except GitOperationError as exc:
                logger.warning("Failed to remove the temporary worktree at {}: {}", worktree, exc)
