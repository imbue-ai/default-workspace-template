"""Linking an app: making the first split of its own history, and giving it a private dev repository.

An app's dev repository is fed by ``git subtree split``, which turns the workspace commits that touched the
app's directory into commits of the app's own history. Without a join marker a split walks the whole workspace
history, which takes minutes; after one it processes only the commits made since. Linking makes that marker,
with ``git subtree split --rejoin``, in a clone of the workspace bounded by the age of the app rather than the
age of the workspace, and fast-forwards the primary branch to it. The marker's tree is its parent's tree, so no
file changes.

The sync service never does this: it never commits to the workspace repository. It only splits and pushes the
apps that are linked.
"""

import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from loguru import logger
from pydantic import Field

from app_install.errors import AppInstallError
from app_install.errors import DevRepositoryError
from app_install.errors import GitCommandError
from app_install.errors import NotModifiedAppError
from app_install.errors import NotPrimaryCheckoutError
from app_install.git import is_app_linked
from app_install.git import is_app_modified
from app_install.git import is_primary_checkout
from app_install.git import require_git_subtree
from app_install.git import require_split_holds_only_the_app
from app_install.git import run_git
from app_install.interfaces import WorkspaceRuntimeInterface
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import app_directory_prefix
from app_manifest.records import join_marker_pattern
from app_manifest.records import load_record
from app_manifest.records import write_record
from github_sync.config import is_sync_configured

# A clone of a workspace can be large, which the small in-memory /tmp of a workspace is not for.
_SCRATCH_PARENT: Final[Path] = Path("/var/tmp")
_SCRATCH_PREFIX: Final[str] = "app-install-link-"
# How many times the marker is made again because the primary branch moved while it was being made.
_MAXIMUM_ATTEMPTS: Final[int] = 5
# A split that has to read the whole workspace history takes minutes (about 12 ms a commit where it was
# measured, and a workspace starts with the template's history), which the time given to an ordinary git
# command does not cover.
_SPLIT_TIMEOUT_SECONDS: Final[float] = 1800.0
_DEV_REPOSITORY_SUFFIX: Final[str] = "-dev"


class LinkResult(LowerCaseStrEnum):
    """What linking did about the app's join marker."""

    # The app's first split was made, and its marker is on the primary branch.
    LINKED = auto()
    # A new marker was made at the current commit, so later splits stay fast.
    REFRESHED = auto()
    # The history already holds a marker for the app, and none was made: none was asked for, or the app has
    # not changed since the newest one.
    ALREADY_LINKED = auto()


class LinkOutcome(FrozenModel):
    """What linking an app did, and what is left to do."""

    result: LinkResult = Field(description="What was done about the app's join marker")
    record: AppRecord = Field(description="The app's record as it is now")
    marker_commit: str = Field(default="", description="The join marker that was made, when one was")
    split_commit: str = Field(default="", description="The commit of the app's own history the marker joins")
    is_full_clone_used: bool = Field(
        default=False, description="Whether the marker was made in a clone of the workspace's whole history"
    )
    is_dev_repository_created: bool = Field(default=False, description="Whether this run created the dev repository")
    remaining: tuple[str, ...] = Field(default=(), description="What is left to do before the app is synced")
    problems: tuple[str, ...] = Field(default=(), description="What this run tried and could not do")


class _Marker(FrozenModel):
    """A join marker made in a clone and brought into the workspace repository."""

    commit: str = Field(description="The marker commit")
    split_commit: str = Field(description="The commit of the app's own history it joins")
    is_full_clone_used: bool = Field(description="Whether it was made in a clone of the whole history")


class _DevRepositoryStep(FrozenModel):
    """What was done about an app's dev repository."""

    record: AppRecord = Field(description="The app's record as it is now")
    is_created: bool = Field(default=False, description="Whether the repository was created")
    remaining: tuple[str, ...] = Field(description="What is left to do before the app is synced")
    problems: tuple[str, ...] = Field(default=(), description="What was tried and could not be done")


@contextmanager
def _scratch_directory() -> Generator[Path, None, None]:
    parent = _SCRATCH_PARENT if _SCRATCH_PARENT.is_dir() else None
    directory = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=parent))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _app_commit_times(repository: Path, revision: str, prefix: str) -> tuple[int, ...]:
    """The committer time of every commit in the history of ``revision`` that touches the app's directory."""
    listing = run_git(repository, ["log", "--format=%ct", revision, "--", prefix])
    return tuple(int(line) for line in listing.split())


def _app_history(repository: Path, revision: str, prefix: str) -> tuple[str, ...]:
    """Every commit in the history of ``revision`` that touches the app's directory, each with the nearest such
    commits it follows: the history a split of the app is made from."""
    return tuple(run_git(repository, ["rev-list", "--parents", revision, "--", prefix]).splitlines())


def _clone_at(directory: Path, branch: str, source: str, start_head: str, shallow_since: int | None) -> Path | None:
    """A clone of the primary branch, cut before ``shallow_since`` when that is given; None when the branch moved
    off ``start_head`` meanwhile."""
    clone = directory / "clone"
    cut = [f"--shallow-since={shallow_since}"] if shallow_since is not None else []
    run_git(
        directory, ["clone", "--quiet", "--no-tags", "--single-branch", "--branch", branch, *cut, source, str(clone)]
    )
    return clone if run_git(clone, ["rev-parse", "HEAD"]) == start_head else None


def _clone_bounded_by_app_age(
    repo_root: Path, directory: Path, branch: str, start_head: str, prefix: str
) -> tuple[Path, bool] | None:
    """A clone of the primary branch that holds the app's whole history, and whether it had to be a clone of the
    whole workspace history; None when the branch moved off ``start_head`` meanwhile.

    The clone is shallow, cut at the app's oldest commit, when the app's history reads the same in it as in the
    workspace. It does not when a commit dated earlier than one of its ancestors puts an early commit of the app
    behind the cut, and when a merge made after the app's first commit brings in a commit dated before it: git
    cuts every parent of such a merge, so what the merge follows is missing and the merge itself reads as the
    commit that added the app. The whole history is cloned instead then.
    """
    oldest = min(_app_commit_times(repo_root, start_head, prefix))
    try:
        # Named by URL: git ignores a depth limit when it clones from a plain local path.
        clone = _clone_at(directory, branch, f"file://{repo_root}", start_head, oldest - 1)
    except GitCommandError as e:
        logger.info("Could not clone {} from the app's first commit on, so its whole history is cloned: {}", prefix, e)
    else:
        if clone is None:
            return None
        if _app_history(clone, "HEAD", prefix) == _app_history(repo_root, start_head, prefix):
            return clone, False
        logger.info("The history of {} reads differently from its first commit on, so all of it is cloned", prefix)
    shutil.rmtree(directory / "clone", ignore_errors=True)
    clone = _clone_at(directory, branch, str(repo_root), start_head, None)
    return (clone, True) if clone is not None else None


def _rejoin_in_clone(clone: Path, start_head: str, name: str, prefix: str, is_first: bool) -> tuple[str, str]:
    """Split the app's history in the clone and join it back; the marker commit and the split commit.

    A split that starts from an earlier marker reads only the commits made since, as long as each of them
    follows commits no older than the marker. A merge that brings in older ones (every update of the workspace
    merges the template's history) sends git subtree back through all of their ancestors, one shell call deep
    per commit, which the shell refuses long before the root of a workspace's history. The split is then made
    again with the markers ignored: every commit is read once, in order, the same split commits come out, and
    the marker it leaves is past the merge.
    """
    arguments = ["subtree", "split", f"--prefix={prefix}", "--rejoin", "-m", f"app-install: link {name}"]
    try:
        output = run_git(clone, arguments, timeout_seconds=_SPLIT_TIMEOUT_SECONDS)
    except GitCommandError as e:
        if is_first:
            raise
        logger.info("The split of {} from its newest marker failed, so the whole history is read: {}", prefix, e)
        output = run_git(clone, [*arguments, "--ignore-joins"], timeout_seconds=_SPLIT_TIMEOUT_SECONDS)
    split_commit = output.splitlines()[-1].strip()
    marker = run_git(clone, ["rev-parse", "HEAD"])
    if marker == start_head:
        raise GitCommandError(
            f"`git subtree split --rejoin` joined nothing for {prefix}: its split, {split_commit}, is already "
            "joined to the history"
        )
    return marker, split_commit


def _make_marker_once(
    repo_root: Path, record: AppRecord, branch: str, start_head: str, is_first: bool
) -> _Marker | None:
    """Make a join marker on ``start_head`` and move the primary branch to it; None when the branch moved
    meanwhile, with nothing changed.

    The first marker is made in a clone bounded by the app's age where that is sound, since the split behind it
    walks every commit of the clone. A later one is made in a clone of the whole history: the split starts from
    the marker before it wherever that is.
    """
    prefix = app_directory_prefix(record)
    with _scratch_directory() as directory:
        if is_first:
            cloned = _clone_bounded_by_app_age(repo_root, directory, branch, start_head, prefix)
        else:
            whole = _clone_at(directory, branch, str(repo_root), start_head, None)
            cloned = (whole, True) if whole is not None else None
        if cloned is None:
            return None
        clone, is_full_clone_used = cloned
        marker, split_commit = _rejoin_in_clone(clone, start_head, str(record.name), prefix, is_first)
        run_git(repo_root, ["fetch", "--quiet", "--no-tags", str(clone), f"refs/heads/{branch}"])
    # Asked of the workspace repository, which holds the history a bounded clone cuts off.
    require_split_holds_only_the_app(repo_root, record, split_commit)
    first_parent = run_git(repo_root, ["rev-parse", f"{marker}^1"])
    trees = run_git(repo_root, ["rev-parse", f"{marker}^{{tree}}", f"{start_head}^{{tree}}"]).split()
    if first_parent != start_head or trees[0] != trees[1]:
        raise GitCommandError(
            f"the join marker {marker} made for {record.name} is not {start_head} with nothing changed, so the "
            "primary branch was left where it was"
        )
    try:
        # Moves the branch only if it is still where the marker was made: the marker changes no file, so the
        # checkout and whatever is uncommitted in it stay exactly as they are.
        run_git(
            repo_root,
            ["update-ref", "-m", f"app-install: link {record.name}", f"refs/heads/{branch}", marker, start_head],
        )
    except GitCommandError as e:
        logger.info("The primary branch moved while {} was being linked, so the marker is made again: {}", prefix, e)
        return None
    return _Marker(commit=marker, split_commit=split_commit, is_full_clone_used=is_full_clone_used)


def _make_marker(repo_root: Path, record: AppRecord, is_first: bool) -> _Marker:
    """Make a join marker for the app at the primary branch's current commit, starting again when the branch
    moves meanwhile. Raises AppInstallError when the checkout is on no branch or the app has no commit yet."""
    try:
        branch = run_git(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    except GitCommandError as e:
        raise AppInstallError(
            f"the primary checkout is on no branch, so there is nothing to put the marker of {record.name} on"
        ) from e
    if not _app_commit_times(repo_root, "HEAD", app_directory_prefix(record)):
        raise AppInstallError(
            f"no commit holds {app_directory_prefix(record)} yet: commit the app, then link it, since its own "
            "history is made from the workspace commits that touched it"
        )
    for _attempt in range(_MAXIMUM_ATTEMPTS):
        marker = _make_marker_once(repo_root, record, branch, run_git(repo_root, ["rev-parse", "HEAD"]), is_first)
        if marker is not None:
            return marker
    raise AppInstallError(
        f"the primary branch moved every one of the {_MAXIMUM_ATTEMPTS} times a marker was made for "
        f"{record.name}; run the command again once nothing else is committing"
    )


def _has_app_commits_since_marker(repo_root: Path, record: AppRecord) -> bool:
    """Whether a commit has touched the app's directory since the app's newest join marker. Only then is there
    a newer split to join: git subtree makes no second marker for a split the history already joins, and cannot
    read a history that holds two."""
    newest = run_git(repo_root, ["rev-list", "--max-count=1", f"--grep={join_marker_pattern(record)}", "HEAD"])
    since = run_git(repo_root, ["rev-list", "--count", f"{newest}..HEAD", "--", app_directory_prefix(record)])
    return int(since) > 0


def _ensure_dev_repository(
    repo_root: Path,
    record: AppRecord,
    is_dev_repository_wanted: bool,
    runtime: WorkspaceRuntimeInterface,
) -> _DevRepositoryStep:
    """Give the app a dev repository when it has none and one is called for, write its URL to the app's record,
    and commit that one file."""
    name = str(record.name)
    is_sync_enabled = is_sync_configured(repo_root)
    problems: list[str] = []
    is_created = False
    if not record.dev_repo_url:
        if not is_sync_enabled and not is_dev_repository_wanted:
            return _DevRepositoryStep(
                record=record,
                remaining=(
                    "GitHub sync is not enabled, so no dev repository was created; "
                    "pass --create-dev-repo to make one for this app alone",
                ),
            )
        repository_name = f"{name}{_DEV_REPOSITORY_SUFFIX}"
        try:
            created = runtime.create_private_repository(
                repository_name, f"Private development history of the {name} app of an Imbue Studio workspace"
            )
        except DevRepositoryError as e:
            return _DevRepositoryStep(
                record=record,
                remaining=(
                    f"the app has no dev repository yet: run the command again once {repository_name} can be created",
                ),
                problems=(f"the dev repository {repository_name} was not created: {e}",),
            )
        is_created = True
        record = record.model_copy_update(to_update(record.field_ref().dev_repo_url, created.url))
        record_file = write_record(repo_root, record)
        commit_failure = runtime.commit_paths(
            [record_file.relative_to(repo_root).as_posix()], f"app-install: record the dev repository of {name}"
        )
        if commit_failure is not None:
            problems.append(f"the dev repository's URL is in the app's record but was not committed: {commit_failure}")
        if created.lockdown_problem:
            problems.append(
                f"closing {created.url} to outside contributions (issues, wiki, projects, discussions, outside "
                f"pull requests) did not take: {created.lockdown_problem}"
            )
    if is_sync_enabled:
        remaining = "nothing: the github-sync service pushes the app to its dev repository on its next pass"
    else:
        remaining = (
            f"GitHub sync is not enabled, so nothing pushes the app on its own: run `github-sync sync-app {name}` "
            "now and after later changes"
        )
    return _DevRepositoryStep(record=record, is_created=is_created, remaining=(remaining,), problems=tuple(problems))


def link_app(
    repo_root: Path,
    name: str,
    is_refreshing: bool,
    is_dev_repository_wanted: bool,
    runtime: WorkspaceRuntimeInterface,
) -> LinkOutcome:
    """Link the app ``name``: make the join marker of an app this workspace built when it has none (or, with
    ``is_refreshing``, a new one at the current commit when the app has changed since its newest), and create its
    private dev repository when GitHub sync is enabled or ``is_dev_repository_wanted``, and its record names none.

    An installed app gets no marker: the squash commits its install and updates left carry the trailer the search
    for one finds, and git subtree refuses to rejoin a split of an app that came in as a squash. Linking one only
    creates its dev repository, and only once it is modified.

    Raises MissingCommandError when git has no ``git subtree``, NotPrimaryCheckoutError when run in a linked
    worktree, AppRecordNotFoundError for an app with no record, NotModifiedAppError for an installed app nobody
    has changed, GitCommandError when the app's directory was removed and made again, so that its split would
    hold the workspace's own commits, and AppInstallError when the app has no commit yet or the primary branch
    would not hold still.
    A dev repository that cannot be created is reported in the outcome's ``problems``, not raised: the marker is
    made all the same.
    """
    require_git_subtree(repo_root)
    if not is_primary_checkout(repo_root):
        raise NotPrimaryCheckoutError(
            f"{repo_root} is not the root of the workspace's primary checkout: linking moves the primary branch, "
            "which a linked worktree is not on, so run it from the workspace root"
        )
    record = load_record(repo_root, name)
    marker: _Marker | None = None
    result = LinkResult.ALREADY_LINKED
    if record.origin is AppOrigin.INSTALLED:
        if not is_app_modified(repo_root, record):
            raise NotModifiedAppError(
                f"{name} was installed and is unchanged, so it has no dev repository: it gets one once the "
                "workspace changes it"
            )
    elif not is_app_linked(repo_root, record):
        marker = _make_marker(repo_root, record, is_first=True)
        result = LinkResult.LINKED
    elif is_refreshing and _has_app_commits_since_marker(repo_root, record):
        marker = _make_marker(repo_root, record, is_first=False)
        result = LinkResult.REFRESHED
    else:
        pass

    dev_repository = _ensure_dev_repository(repo_root, record, is_dev_repository_wanted, runtime)
    return LinkOutcome(
        result=result,
        record=dev_repository.record,
        marker_commit=marker.commit if marker is not None else "",
        split_commit=marker.split_commit if marker is not None else "",
        is_full_clone_used=marker.is_full_clone_used if marker is not None else False,
        is_dev_repository_created=dev_repository.is_created,
        remaining=dev_repository.remaining,
        problems=dev_repository.problems,
    )
