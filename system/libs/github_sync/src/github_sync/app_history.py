"""What the workspace repository's history says about one user app, and the
split that turns it into the app's own history.

A user app lives in the workspace repository at `system/apps/<package>` and has
a repository of its own, which `git subtree split` feeds: it turns the workspace
commits that touched the app's directory into commits of the app's own history,
and produces the same commits again from a longer history. `app-install` and the
sync pass both ask the questions here, so they give one answer.

A split is made in a scratch clone whose history is cut at the app's bound, the
commit before which nothing can add to the app's own history: the commit the
app's newest join marker was made on, or the merge that installed the app.
`git subtree` reads one commit at a time, a few shell calls deep per commit, so
a split that left the bound would take minutes on a workspace's history and,
where it had to go back through a merged commit's ancestors, would stop at the
shell's limit on how deep a function calls itself.
"""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from app_manifest.records import (
    AppOrigin,
    AppRecord,
    app_directory_prefix,
    join_marker_pattern,
    modifying_paths,
)
from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

from github_sync.config import GithubSyncError

# A split reads every workspace commit made since the app's bound.
GIT_TIMEOUT_SECONDS = 600.0
_SUBTREE_COMMAND_FILENAME = "git-subtree"
# Where `git subtree split` keeps its scratch files, under the git directory.
_SPLIT_CACHE_DIRNAME = "subtree-cache"
# A clone of a workspace is not for the small in-memory /tmp of a workspace.
_SCRATCH_PARENT = Path("/var/tmp")
# Followed by the id of the process that made the directory.
_SCRATCH_PREFIX = "github-sync-split-"
# The two lines of a commit message `git subtree` reads a join by: a marker
# carries both, the squash commit of an install or an update only the second.
_MAINLINE_KEY = "git-subtree-mainline:"
_SPLIT_KEY = "git-subtree-split:"
_FIELD_SEPARATOR = "\x1f"
_RECORD_SEPARATOR = "\x1e"
# Read the history as it is, whatever a scratch clone hides of it.
_AS_IT_IS = "--no-replace-objects"


class AppHistoryError(GithubSyncError):
    """A git command that reads or splits an app's history failed."""


class GitSubtreeMissingError(AppHistoryError):
    """The git installation has no `git subtree` command."""


class WorkspaceHistoryInSplitError(AppHistoryError):
    """A split of an app's directory holds the workspace's own commits."""


class GitTimeoutError(AppHistoryError):
    """A git command did not finish in the time it is given."""


class SplitTimeoutError(AppHistoryError):
    """A split of an app's directory did not finish in the time it is given."""


class AppSplit(FrozenModel):
    """The app's own history at one workspace commit, for as long as the scratch
    clone it was made in is kept."""

    commit: str = Field(description="The commit of the app's own history")
    tree: str = Field(description="That commit's tree, the app's directory")
    clone: Path = Field(description="The scratch clone the split was made in")
    object_directory: Path = Field(
        description="Where the commits the split made are, and nowhere else"
    )


class _Join(FrozenModel):
    """A commit whose message names an app's directory the way `git subtree`
    looks for it: a join marker, or the squash commit of an install or update."""

    commit: str = Field(description="The commit")
    parents: tuple[str, ...] = Field(description="The commits it follows")
    mainline: str = Field(
        description="The workspace commit a marker was made on; empty for a squash commit"
    )
    split: str = Field(
        description="The commit of the app's own history the commit stands for, or empty"
    )


class _SplitBound(FrozenModel):
    """The commit a split of one app starts reading the workspace's history at,
    and the commits of the app's own history that history joins."""

    start: str = Field(description="The oldest workspace commit a split reads")
    start_parents: tuple[str, ...] = Field(
        description="The parents the start is read with: the squash commit of an "
        "install, none for the commit a marker was made on"
    )
    joined: frozenset[str] = Field(
        description="The commits of the app's own history a later workspace commit "
        "has as a parent: the split a marker joins, or the squash commits"
    )
    joined_split: str = Field(
        default="",
        description="The split a marker joins, read with no parents; empty for an installed app",
    )


def _run_git(
    repo_root: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    timeout_seconds: float = GIT_TIMEOUT_SECONDS,
) -> bytes:
    command_environment = dict(os.environ if environment is None else environment)
    # Never wait on a prompt: nothing here runs with a person at a terminal.
    command_environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *arguments],
            cwd=repo_root,
            env=command_environment,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise GitTimeoutError(
            f"`git {' '.join(arguments)}` did not finish in {timeout_seconds:g} seconds"
        ) from e
    except OSError as e:
        raise AppHistoryError(f"could not run `git {' '.join(arguments)}`: {e}") from e
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode(errors="replace").strip()
        raise AppHistoryError(f"`git {' '.join(arguments)}` failed: {detail}")
    return completed.stdout


def run_git(
    repo_root: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = GIT_TIMEOUT_SECONDS,
) -> str:
    """Run one git command in `repo_root` with hooks off; its stdout, stripped.

    Raises AppHistoryError, carrying what git said, when the command fails, and
    GitTimeoutError when it runs longer than `timeout_seconds`.
    """
    output = _run_git(
        repo_root, arguments, environment, timeout_seconds=timeout_seconds
    )
    return output.decode(errors="replace").strip()


def require_git_subtree(repo_root: Path) -> None:
    """Raise GitSubtreeMissingError, naming the command, when this git has no
    `git subtree`: some distributions package it apart from git itself."""
    exec_path = run_git(repo_root, ["--exec-path"])
    if not (Path(exec_path) / _SUBTREE_COMMAND_FILENAME).is_file():
        raise GitSubtreeMissingError(
            f"the `git subtree` command is not installed (there is no "
            f"{_SUBTREE_COMMAND_FILENAME} in {exec_path}), and an app's own "
            "repository is kept with it; install the package that carries it "
            "(`git` on Debian and Ubuntu, `git-subtree` on Fedora) and run this again"
        )


def is_primary_checkout(repo_root: Path) -> bool:
    """Whether `repo_root` is the root of a repository's primary checkout,
    rather than a directory inside one or a linked worktree, which is on a
    branch of its own."""
    top_level = Path(run_git(repo_root, ["rev-parse", "--show-toplevel"]))
    if top_level.resolve() != repo_root.resolve():
        return False
    git_dir = Path(run_git(repo_root, ["rev-parse", "--absolute-git-dir"]))
    common_dir = Path(
        run_git(repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    )
    return git_dir.resolve() == common_dir.resolve()


def app_tree(repo_root: Path, record: AppRecord) -> str | None:
    """The tree of the app's directory at the checked-out commit, or None when
    that commit does not hold the directory."""
    try:
        return run_git(
            repo_root,
            [
                "rev-parse",
                "--verify",
                "--quiet",
                f"HEAD:{app_directory_prefix(record)}",
            ],
        )
    except AppHistoryError as e:
        logger.debug("No committed directory for the app {}: {}", record.name, e)
        return None


def is_app_linked(repo_root: Path, record: AppRecord) -> bool:
    """Whether the history of the checked-out commit holds a join marker for the
    app's directory.

    `git subtree split --rejoin` leaves one, which is what linking an app this
    workspace built does: a split then starts from the commit it was made on
    rather than reading the whole workspace history. Every `git subtree add`
    and `git subtree merge` with `--squash` leaves a commit the same search
    finds, so an installed app counts as linked from the start, and its split
    starts from the merge that installed it.
    """
    marker = run_git(
        repo_root,
        ["rev-list", "--max-count=1", f"--grep={join_marker_pattern(record)}", "HEAD"],
    )
    return bool(marker)


def is_app_modified(repo_root: Path, record: AppRecord) -> bool:
    """Whether an installed app's committed directory differs from what was
    installed in any path outside the regenerable set. An app this workspace
    built is never modified: it has nothing it was installed from."""
    if record.origin is not AppOrigin.INSTALLED or not record.upstream_tree:
        return False
    tree = app_tree(repo_root, record)
    if tree is None or tree == record.upstream_tree:
        return False
    try:
        differing = run_git(
            repo_root,
            ["diff", "--name-only", "--no-renames", record.upstream_tree, tree],
        )
    except AppHistoryError as e:
        # Without the installed tree the difference cannot be shown to be in
        # regenerable files alone.
        logger.warning(
            "Could not compare {} with the tree it was installed from, so it is "
            "taken as modified: {}",
            record.name,
            e,
        )
        return True
    return bool(modifying_paths(differing.splitlines()))


def _is_process_running(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It runs as another user.
        return True
    return True


def _remove_finished_split_caches(cache_root: Path) -> None:
    """Delete the scratch directory of every split that is no longer running.

    `git subtree split` keeps one per run, named by its process id and holding
    a file per commit it walked, and never deletes it. One whose process is
    still running is another split's, and is left alone.
    """
    if not cache_root.is_dir():
        return
    for entry in cache_root.iterdir():
        if not entry.name.isdigit() or _is_process_running(int(entry.name)):
            continue
        try:
            shutil.rmtree(entry)
        except OSError as e:
            logger.warning(
                "Could not remove the finished split's scratch {}: {}", entry, e
            )


def require_split_holds_only_the_app(
    repo_root: Path, record: AppRecord, split_commit: str
) -> None:
    """Raise WorkspaceHistoryInSplitError when the history of `split_commit`
    holds the workspace's own commits.

    `git subtree split` keeps a workspace commit as it is, rather than making
    the app's copy of it, when the commit has no app directory and the commit
    before it had one: the directory was removed and made again. Everything
    that commit follows comes with it, so the split would carry the whole
    workspace, every file of it, into a repository meant to hold one app.

    `repo_root` is the workspace repository or a clone of it that holds the
    split; the history is read as it is, whatever a scratch clone hides of it.
    """
    workspace_root = run_git(
        repo_root,
        [_AS_IT_IS, "rev-list", "--first-parent", "--max-parents=0", "HEAD"],
    )
    # Nothing of the workspace's first commit is left once the split's history
    # is taken away only when that history holds it.
    outside_split = run_git(
        repo_root,
        [_AS_IT_IS, "rev-list", "--count", workspace_root, f"^{split_commit}"],
    )
    if outside_split == "0":
        raise WorkspaceHistoryInSplitError(
            f"{app_directory_prefix(record)} was removed and made again in the "
            "workspace's history, and a split of such a directory carries the "
            "workspace's own commits, with every file of the workspace, into the "
            f"app's history; nothing was done with it, and {record.name} cannot "
            "have a repository of its own while it lives in that directory"
        )


def _resolve_commit(repository: Path, revision: str) -> str:
    return run_git(repository, ["rev-parse", "--verify", f"{revision}^{{commit}}"])


def _newest_join(repository: Path, record: AppRecord, commit: str) -> _Join | None:
    """The newest commit in the history of `commit` whose message names the
    app's directory, by the search that says whether an app is linked."""
    found = run_git(
        repository,
        [
            "log",
            "--max-count=1",
            "--no-show-signature",
            f"--grep={join_marker_pattern(record)}",
            f"--format=%H{_FIELD_SEPARATOR}%P{_FIELD_SEPARATOR}%B",
            commit,
        ],
    )
    if not found:
        return None
    join, parents, message = found.split(_FIELD_SEPARATOR, maxsplit=2)
    # Read as `git subtree` reads them: the first word of a line is the key and
    # the second its value, and a later line overrides an earlier one.
    named = {
        words[0]: words[1]
        for words in (line.split() for line in message.splitlines())
        if len(words) > 1
    }
    return _Join(
        commit=join,
        parents=tuple(parents.split()),
        mainline=named.get(_MAINLINE_KEY, ""),
        split=named.get(_SPLIT_KEY, ""),
    )


def _parents_by_commit(
    repository: Path, revisions: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """The parents of every commit `revisions` select, each commit after the
    commits it follows."""
    listing = run_git(
        repository, ["rev-list", "--topo-order", "--reverse", "--parents", *revisions]
    )
    return {
        commit: tuple(parents)
        for commit, *parents in (line.split() for line in listing.splitlines())
    }


def _find_bound(repository: Path, record: AppRecord, commit: str) -> _SplitBound | None:
    """Where a split of the app at `commit` can start, or None when the history
    of `commit` holds neither a marker for an app this workspace built nor the
    merge that installed an installed app: the whole history is then read.

    For an app this workspace built the bound is the commit its newest marker
    was made on, which the marker maps to the split it joins. For an installed
    app it is the merge `git subtree add --squash` made: the commits before it
    never held this install of the app, and that merge and each later
    `git subtree merge --squash` reach the publisher's history through their
    squash commits.
    """
    join = _newest_join(repository, record, commit)
    if join is None or not join.split:
        return None
    if record.origin is AppOrigin.LOCAL:
        if not join.mainline:
            return None
        joined_split = _resolve_commit(repository, join.split)
        return _SplitBound(
            start=_resolve_commit(repository, join.mainline),
            start_parents=(),
            joined=frozenset({joined_split}),
            joined_split=joined_split,
        )
    if join.mainline:
        return None
    # The squash commit of each update follows that of the one before it, back
    # to the install's, which follows nothing.
    squashes = run_git(repository, ["rev-list", join.commit]).split()
    installed_squash = squashes[-1]
    install_merges = [
        candidate
        for candidate, parents in _parents_by_commit(
            repository, ["--ancestry-path", f"{installed_squash}..{commit}"]
        ).items()
        if installed_squash in parents and candidate not in squashes
    ]
    if len(install_merges) != 1:
        return None
    return _SplitBound(
        start=install_merges[0],
        start_parents=(installed_squash,),
        joined=frozenset(squashes),
    )


def _replace_parents(
    scratch_clone: Path, parents_by_commit: Mapping[str, Sequence[str]]
) -> None:
    """Have every git command in `scratch_clone` read each commit of
    `parents_by_commit` as following only the parents given, and as it is in
    everything else. Each gets a replace ref; no object of the history changes.

    The replacement is written here rather than by `git replace --graft`, which
    refuses a merge of a signed tag once the tagged parent is left out.
    """
    replacements: list[str] = []
    for commit, parents in parents_by_commit.items():
        original = _run_git(scratch_clone, ["cat-file", "commit", commit])
        header, separator, message = original.partition(b"\n\n")
        tree_line, *other_lines = header.split(b"\n")
        replacement = b"\n".join(
            [
                tree_line,
                *(b"parent " + parent.encode("ascii") for parent in parents),
                # A continuation line of a header begins with a space, so only
                # the commit's own parent lines begin with this.
                *(line for line in other_lines if not line.startswith(b"parent ")),
            ]
        )
        written = _run_git(
            scratch_clone,
            ["hash-object", "-t", "commit", "-w", "--stdin"],
            stdin=replacement + separator + message,
        )
        replaced_by = written.decode("ascii").strip()
        if replaced_by != commit:
            replacements.append(f"create refs/replace/{commit} {replaced_by}\n")
    _run_git(
        scratch_clone,
        ["update-ref", "--stdin"],
        stdin="".join(replacements).encode("ascii"),
    )


def cut_history_at_bound(
    scratch_clone: Path, record: AppRecord, commit: str
) -> int | None:
    """Hide from every git command in `scratch_clone` the part of the history
    of `commit` that a split of the app need not read, and return how many
    workspace commits are left for it to read; None, with nothing hidden, when
    the history holds no bound for the app.

    `scratch_clone` is a clone made to be thrown away, never the workspace
    repository: what is hidden is hidden from everything that reads it.

    What is left is the bound and the commits that follow it. A merge among them
    that also follows a commit from before the bound (an update of the workspace
    merges the template's history, and a worker's branch can have left before
    the bound) is read as following only its parents that follow the bound, so
    `git subtree` never goes back through the older parent's ancestors. Nothing
    in the app's tree at the merge is lost: a change to the app that came in
    through the older parent appears in the app's history as part of the merge.
    The cut depends on the history alone, so the same history gives the same
    split, and a longer one continues it.
    """
    bound = _find_bound(scratch_clone, record, commit)
    if bound is None:
        return None
    parents_as_read: dict[str, Sequence[str]] = {bound.start: bound.start_parents}
    if bound.joined_split:
        # The split's own history was made before the bound, and adds nothing.
        parents_as_read[bound.joined_split] = ()
    following = {bound.start}
    since_bound = _parents_by_commit(scratch_clone, [commit, f"^{bound.start}"])
    for later, parents in since_bound.items():
        if not any(parent in following for parent in parents):
            continue
        following.add(later)
        kept = tuple(p for p in parents if p in following or p in bound.joined)
        if kept != parents:
            parents_as_read[later] = kept
    _replace_parents(scratch_clone, parents_as_read)
    return len(following)


def _what_a_split_reads(record: AppRecord, commit_count: int | None) -> str:
    """How much of the workspace's history a split of the app has to read, and
    what shortens it where something does."""
    if commit_count is None:
        return "it reads every commit of the workspace's history"
    if record.origin is AppOrigin.INSTALLED:
        return f"it reads the {commit_count} workspace commits made since the app was installed"
    return (
        f"it reads the {commit_count} workspace commits made since the app's newest "
        f"join marker, and `app-install link {record.name} --refresh` makes a marker "
        "at the current commit, after which a split reads only what follows that"
    )


def _remove_abandoned_scratch(parent: Path) -> None:
    """Delete the scratch directory of every split whose process has ended
    without removing it, as one stopped by a signal does."""
    for entry in parent.glob(f"{_SCRATCH_PREFIX}*"):
        process_id = entry.name.removeprefix(_SCRATCH_PREFIX).partition("-")[0]
        if process_id.isdigit() and not _is_process_running(int(process_id)):
            shutil.rmtree(entry, ignore_errors=True)


@contextmanager
def open_app_split(
    repo_root: Path, record: AppRecord, timeout_seconds: float = GIT_TIMEOUT_SECONDS
) -> Generator[AppSplit, None, None]:
    """The commit of the app's own history that the checked-out commit splits
    to, for as long as the context is open. `timeout_seconds` is how long the
    split itself is given.

    The split is made by `git subtree split` in a scratch clone under
    `/var/tmp` that shares the workspace repository's objects and is cut at the
    app's bound (`cut_history_at_bound`). The commits it makes are in that
    clone alone, which is removed when the context closes: nothing is written
    to the workspace repository, no object and no ref.

    Raises GitSubtreeMissingError when git has no `git subtree`,
    WorkspaceHistoryInSplitError when the split holds the workspace's own
    commits, SplitTimeoutError, saying how much it had to read, when the split
    does not finish in time, and AppHistoryError when it fails otherwise (no
    commit holds the app's directory).
    """
    require_git_subtree(repo_root)
    prefix = app_directory_prefix(record)
    git_directory = Path(run_git(repo_root, ["rev-parse", "--absolute-git-dir"]))
    scratch_parent = _SCRATCH_PARENT if _SCRATCH_PARENT.is_dir() else None
    if scratch_parent is not None:
        _remove_abandoned_scratch(scratch_parent)
    scratch = Path(
        tempfile.mkdtemp(prefix=f"{_SCRATCH_PREFIX}{os.getpid()}-", dir=scratch_parent)
    )
    try:
        clone = scratch / "clone"
        run_git(
            scratch,
            [
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                "--no-tags",
                str(repo_root),
                str(clone),
            ],
        )
        commit = run_git(clone, ["rev-parse", "HEAD"])
        commit_count = cut_history_at_bound(clone, record, commit)
        # `git subtree` asks only that the directory exists in the checkout.
        (clone / prefix).mkdir(parents=True)
        try:
            output = run_git(
                clone,
                ["subtree", "split", "--quiet", f"--prefix={prefix}", commit],
                timeout_seconds=timeout_seconds,
            )
        except GitTimeoutError as e:
            raise SplitTimeoutError(
                f"the split of {prefix} did not finish in {timeout_seconds:g} seconds: "
                f"{_what_a_split_reads(record, commit_count)}"
            ) from e
        lines = output.splitlines()
        if not lines:
            raise AppHistoryError(f"`git subtree split` of {prefix} named no commit")
        split_commit = lines[-1].strip()
        require_split_holds_only_the_app(clone, record, split_commit)
        yield AppSplit(
            commit=split_commit,
            tree=run_git(clone, ["rev-parse", f"{split_commit}^{{tree}}"]),
            clone=clone,
            object_directory=clone / ".git" / "objects",
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        _remove_finished_split_caches(git_directory / _SPLIT_CACHE_DIRNAME)
