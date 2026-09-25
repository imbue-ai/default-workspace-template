"""Carry a workspace across the template's one-time history rewrite.

The template's history was rewritten to drop :data:`REWRITTEN_PATHS` from every
commit, so a workspace created before that shares no commit with a later
release. Running the same filter over the workspace's own branches and tags
reproduces upstream's rewritten commits exactly, which gives the update a merge
base again. Only refs and indexes move: the working tree keeps the files the
running workspace still loads (untracked, excluded) until an update lands.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

# CLEANUP: remove this module, its `rewrite-history` subcommand, and the Step 3a
# paragraph that runs it once no supported workspace predates the history rewrite.

REWRITTEN_PATHS = ("libs/mngr", "vendor/mngr", "system/vendor/mngr")

# Upstream's rewrite ran this exact tool and argument list; any other version or
# flag can produce different commits, and then no merge base.
FILTER_REPO_COMMAND = ("uvx", "--from", "git-filter-repo==2.47.0", "git-filter-repo")
FILTER_REPO_ARGS = (
    "--force",
    "--preserve-commit-hashes",
    "--invert-paths",
    *(arg for path in REWRITTEN_PATHS for arg in ("--path", path)),
)

DEFAULT_SCRATCH_DIR = "data/.tasks/update-self/history-rewrite"

_REWRITTEN_REFS = "refs/update-self/rewritten/"
_EXCLUDE_HEADER = "# update-self: left on disk by the template history rewrite"
_ZERO_OID = "0" * 40


class HistoryRewriteError(Exception):
    """The workspace's history could not be carried across the rewrite; nothing live changed."""


@dataclass(frozen=True)
class HistoryRewrite:
    is_rewritten: bool
    fork_point: str
    moved_refs: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "rewritten": self.is_rewritten,
                "fork_point": self.fork_point,
                "moved_refs": self.moved_refs,
            }
        )


@dataclass(frozen=True)
class _Worktree:
    path: Path
    head: str
    branch: str | None


def _run(
    args: Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=None if env is None else {**os.environ, **env},
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    result = _run(["git", *args], repo, stdin=stdin)
    if result.returncode != 0:
        raise HistoryRewriteError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _merge_base(repo: Path, left: str, right: str) -> str | None:
    result = _run(["git", "merge-base", left, right], repo)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise HistoryRewriteError(
            f"git merge-base {left} {right} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _worktrees(repo: Path) -> list[_Worktree]:
    worktrees: list[_Worktree] = []
    for block in _git(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in block.splitlines() if line)
        if "worktree" in fields and "HEAD" in fields and "bare" not in fields:
            worktrees.append(
                _Worktree(Path(fields["worktree"]), fields["HEAD"], fields.get("branch"))
            )
    return worktrees


def _stripped_tree(repo: Path, commit: str, scratch: Path) -> str:
    env = {"GIT_INDEX_FILE": str(scratch / "strip.index")}
    for args in (
        ["read-tree", commit],
        ["rm", "-r", "-q", "-f", "--cached", "--ignore-unmatch", "--", *REWRITTEN_PATHS],
    ):
        result = _run(["git", *args], repo, env)
        if result.returncode != 0:
            raise HistoryRewriteError(f"could not strip {commit}: {result.stderr.strip()}")
    return _run(["git", "write-tree"], repo, env).stdout.strip()


def _graft_shallow_boundaries(repo: Path, mirror: Path, target: str, scratch: Path) -> None:
    """Graft each shallow boundary onto its twin in ``target``: the commit whose tree
    is the boundary's minus :data:`REWRITTEN_PATHS`, so the filter drops the boundary as empty."""
    shallow = mirror / "shallow"
    if not shallow.exists():
        return
    twins = {}
    for line in _git(repo, "log", "--format=%T %H", target).splitlines():
        tree, _, commit = line.partition(" ")
        twins.setdefault(tree, commit)
    for boundary in dict.fromkeys(shallow.read_text().split()):
        if not _git(repo, "for-each-ref", "--contains", boundary, "refs/heads", "refs/tags"):
            continue
        twin = twins.get(_stripped_tree(repo, boundary, scratch))
        if twin is None:
            raise HistoryRewriteError(
                f"this workspace's history starts at {boundary[:12]}, and no commit of "
                f"{target} matches it with {', '.join(REWRITTEN_PATHS)} removed"
            )
        _git(mirror, "replace", "--graft", boundary, twin)
    shallow.unlink()


def _rewrite_in_mirror(repo: Path, target: str, scratch: Path) -> Path:
    mirror = scratch / "mirror.git"
    shutil.rmtree(mirror, ignore_errors=True)
    _git(repo, "clone", "--quiet", "--mirror", repo.as_uri(), str(mirror))
    _graft_shallow_boundaries(repo, mirror, target, scratch)
    try:
        result = _run([*FILTER_REPO_COMMAND, *FILTER_REPO_ARGS], mirror)
    except FileNotFoundError as error:
        raise HistoryRewriteError(f"cannot run {FILTER_REPO_COMMAND[0]}: {error}") from error
    if result.returncode != 0:
        raise HistoryRewriteError(
            f"git-filter-repo failed (exit {result.returncode}): {result.stderr.strip()[-2000:]}"
        )
    return mirror


def _fetch_rewritten_refs(repo: Path, mirror: Path) -> dict[str, str]:
    """Fetch the mirror's branches and tags under :data:`_REWRITTEN_REFS`; map each live name to its rewritten oid."""
    _git(
        repo,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        str(mirror),
        f"+refs/heads/*:{_REWRITTEN_REFS}heads/*",
        f"+refs/tags/*:{_REWRITTEN_REFS}tags/*",
    )
    rewritten: dict[str, str] = {}
    listing = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", _REWRITTEN_REFS)
    for line in listing.splitlines():
        ref, _, oid = line.partition(" ")
        rewritten["refs/" + ref.removeprefix(_REWRITTEN_REFS)] = oid
    return rewritten


def _drop_rewritten_refs(repo: Path) -> None:
    listing = _git(repo, "for-each-ref", "--format=delete %(refname)", _REWRITTEN_REFS)
    if listing:
        _git(repo, "update-ref", "--stdin", stdin=listing + "\n")


def _exclude_left_behind_paths(repo: Path) -> None:
    exclude = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    text = exclude.read_text() if exclude.exists() else ""
    existing = text.splitlines()
    missing = [f"/{path}/" for path in REWRITTEN_PATHS if f"/{path}/" not in existing]
    if not missing:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = [] if _EXCLUDE_HEADER in existing else [_EXCLUDE_HEADER]
    prefix = "" if text == "" or text.endswith("\n") else "\n"
    with exclude.open("a") as handle:
        handle.write(prefix + "\n".join([*lines, *missing]) + "\n")


def _sync_indexes(journal: dict) -> None:
    """Point each moved worktree's index at its new commit, leaving its files alone."""
    for entry in journal["worktrees"]:
        _git(Path(entry["path"]), "read-tree", "-m", entry["old"], entry["new"])


def _finish(repo: Path, journal_path: Path, journal: dict) -> None:
    _exclude_left_behind_paths(repo)
    _sync_indexes(journal)
    journal_path.unlink()
    if _git(repo, "status", "--porcelain"):
        raise HistoryRewriteError(
            "the rewritten history is in place but the working tree is not clean: "
            + _git(repo, "status", "--short").splitlines()[0]
        )


def rewrite_history(repo: Path, target: str, scratch: Path) -> HistoryRewrite:
    """Give ``HEAD`` a merge base with ``target`` by rewriting local history the way upstream's was.

    A no-op when a merge base already exists. Re-entrant: an interrupted run is
    finished (refs moved) or started over (refs not yet moved).
    """
    scratch.mkdir(parents=True, exist_ok=True)
    journal_path = scratch / "journal.json"
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else None

    fork_point = _merge_base(repo, "HEAD", target)
    if fork_point is not None:
        if journal is not None and _git(repo, "rev-parse", "HEAD") == journal["head"]:
            _finish(repo, journal_path, journal)
            return HistoryRewrite(True, fork_point, len(journal["refs"]))
        return HistoryRewrite(False, fork_point, 0)
    if journal is not None:
        journal_path.unlink()

    branch = _run(["git", "symbolic-ref", "-q", "HEAD"], repo).stdout.strip()
    if not branch:
        raise HistoryRewriteError("HEAD is detached; check out the workspace's branch first")
    if _git(repo, "status", "--porcelain"):
        raise HistoryRewriteError("the working tree has uncommitted changes")

    try:
        mirror = _rewrite_in_mirror(repo, target, scratch)
        rewritten = _fetch_rewritten_refs(repo, mirror)
        if branch not in rewritten:
            raise HistoryRewriteError(f"rewriting left no {branch} to update")
        fork_point = _merge_base(repo, rewritten[branch], target)
        if fork_point is None:
            raise HistoryRewriteError(
                f"after rewriting, {branch} still shares no history with {target}: this "
                "workspace does not descend from the rewritten template"
            )
        live = dict(
            line.partition(" ")[::2]
            for line in _git(
                repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"
            ).splitlines()
        )
        moves = {ref: oid for ref, oid in rewritten.items() if live.get(ref) != oid}
        journal = {
            "head": moves[branch],
            "refs": sorted(moves),
            "worktrees": [
                {"path": str(tree.path), "old": tree.head, "new": moves[tree.branch]}
                for tree in _worktrees(repo)
                if tree.branch in moves
            ],
        }
        journal_path.write_text(json.dumps(journal))
        transaction = "".join(
            f"update {ref} {oid} {live.get(ref, _ZERO_OID)}\n" for ref, oid in moves.items()
        )
        _git(repo, "update-ref", "--stdin", stdin=transaction)
    finally:
        _drop_rewritten_refs(repo)
        shutil.rmtree(scratch / "mirror.git", ignore_errors=True)
    _finish(repo, journal_path, journal)
    return HistoryRewrite(True, fork_point, len(moves))
