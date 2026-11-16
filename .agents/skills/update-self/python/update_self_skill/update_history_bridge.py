"""Bridge a workspace across the template's one-time history rewrite.

The template's history was rewritten to drop :data:`REWRITTEN_PATHS`, so a
workspace created before that shares no commit with a later release. While
that is so, a ``git replace`` graft names the workspace's own fork point as an
extra parent of its rewritten twin in the target, which gives the ordinary
merge its base. Nothing in the workspace is rewritten; once a merge with the
target has landed the graft is dropped.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

# CLEANUP: remove this module, its `bridge-history` subcommand, and the skill
# prose that names it once no supported workspace predates the history rewrite.

REWRITTEN_PATHS = ("libs/mngr", "vendor/mngr", "system/vendor/mngr")

DEFAULT_STATE_PATH = "data/.state/update-self/history-bridge.json"

_LOG_FORMAT = "%H%x00%T%x00%an%x00%ae%x00%at%x00%cn%x00%ce%x00%ct%x00%s"


class HistoryBridgeError(Exception):
    """The history bridge could not be built or dropped; the message says why."""


@dataclass(frozen=True)
class HistoryBridge:
    is_bridged: bool
    fork_point: str | None
    twin: str | None
    dropped: str | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "bridged": self.is_bridged,
                "fork_point": self.fork_point,
                "twin": self.twin,
                "dropped": self.dropped,
            }
        )


def _run(
    repo: Path,
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=None if env is None else {**os.environ, **env},
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(
    repo: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> str:
    result = _run(repo, args, env, stdin)
    if result.returncode != 0:
        raise HistoryBridgeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _merge_base(
    repo: Path, left: str, right: str, *, is_graft_seen: bool
) -> str | None:
    env = None if is_graft_seen else {"GIT_NO_REPLACE_OBJECTS": "1"}
    result = _run(repo, ["merge-base", left, right], env)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise HistoryBridgeError(
            f"git merge-base {left} {right} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


@dataclass(frozen=True)
class _Commit:
    oid: str
    tree: str
    identity: tuple[str, ...]


def _commits(repo: Path, *args: str) -> list[_Commit]:
    """``git log`` of ``args`` ignoring any graft; ``identity`` is what the rewrite keeps: author, committer, dates, subject."""
    out = _git(
        repo,
        "log",
        f"--format={_LOG_FORMAT}",
        *args,
        env={"GIT_NO_REPLACE_OBJECTS": "1"},
    )
    commits = []
    # Names and subjects can hold characters ``str.splitlines`` also breaks on (U+2028, form feed).
    for line in out.split("\n") if out else []:
        oid, tree, *identity = line.split("\0")
        commits.append(_Commit(oid, tree, tuple(identity)))
    return commits


def _shallow_boundaries(repo: Path) -> list[str]:
    shallow = (
        Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        / "shallow"
    )
    return shallow.read_text().split() if shallow.exists() else []


def _stripped_tree(repo: Path, commit: str, index: Path) -> str:
    env = {"GIT_INDEX_FILE": str(index)}
    _git(repo, "read-tree", commit, env=env)
    _git(
        repo,
        "rm",
        "-r",
        "-q",
        "-f",
        "--cached",
        "--ignore-unmatch",
        "--",
        *REWRITTEN_PATHS,
        env=env,
    )
    return _git(repo, "write-tree", env=env)


def _newest_equivalent(repo: Path, fork: str) -> str:
    """The newest commit between ``fork`` and HEAD that differs from it only in :data:`REWRITTEN_PATHS`.

    The rewrite drops a commit that touched only those paths, so a fork point
    followed by vendor refreshes has the same twin as the last of them, and the
    last one is the base that matches the workspace's own vendored copy.
    """
    descendants = _commits(repo, "--topo-order", "--ancestry-path", f"{fork}..HEAD")
    if not descendants:
        return fork
    fork_tree = _git(repo, "rev-parse", f"{fork}^{{tree}}")
    headers = [f"{fork_tree} {commit.tree}" for commit in descendants]
    diff = _git(
        repo,
        "diff-tree",
        "--stdin",
        "-r",
        "--name-only",
        "--",
        ".",
        *(f":(exclude){root}" for root in REWRITTEN_PATHS),
        stdin="\n".join(headers) + "\n",
    )
    changed: list[list[str]] = []
    for line in diff.splitlines():
        if len(changed) < len(headers) and line == headers[len(changed)]:
            changed.append([])
        else:
            changed[-1].append(line)
    for commit, paths in zip(descendants, changed):
        if not paths:
            return commit.oid
    return fork


def _find_fork(repo: Path, target: str, index: Path) -> tuple[str, str]:
    """The newest ancestor of HEAD with a rewritten twin in ``target``'s history, advanced by :func:`_newest_equivalent`, and that twin.

    Twins are matched by author, committer, dates and subject, which the rewrite
    keeps; a fork point the rewrite dropped entirely (a shallow boundary that was
    a vendor refresh) is matched by its tree instead.
    """
    history = _commits(repo, target)
    by_identity: dict[tuple[str, ...], _Commit] = {}
    by_tree: dict[str, _Commit] = {}
    for commit in history:
        by_identity.setdefault(commit.identity, commit)
        by_tree.setdefault(commit.tree, commit)
    for commit in _commits(repo, "--topo-order", "HEAD"):
        twin = by_identity.get(commit.identity)
        if twin is not None and _stripped_tree(repo, commit.oid, index) == twin.tree:
            return _newest_equivalent(repo, commit.oid), twin.oid
    for boundary in _shallow_boundaries(repo):
        twin = by_tree.get(_stripped_tree(repo, boundary, index))
        if (
            twin is not None
            and _run(repo, ["merge-base", "--is-ancestor", boundary, "HEAD"]).returncode
            == 0
        ):
            return _newest_equivalent(repo, boundary), twin.oid
    raise HistoryBridgeError(
        f"HEAD shares no history with {target}, and none of its commits matches one of "
        f"{target}'s with {', '.join(REWRITTEN_PATHS)} removed"
    )


def _drop_recorded_graft(repo: Path, state: Path) -> str | None:
    if not state.exists():
        return None
    twin = json.loads(state.read_text())["twin"]
    if (
        _run(repo, ["rev-parse", "-q", "--verify", f"refs/replace/{twin}"]).returncode
        == 0
    ):
        _git(repo, "replace", "-d", twin)
    state.unlink()
    return twin


def bridge_history(repo: Path, target: str, state: Path) -> HistoryBridge:
    """Keep a graft between HEAD's history and ``target``'s for exactly as long as they share no commit."""
    shared = _merge_base(repo, "HEAD", target, is_graft_seen=False)
    if shared is not None:
        return HistoryBridge(False, shared, None, _drop_recorded_graft(repo, state))

    with tempfile.TemporaryDirectory() as scratch:
        fork, twin = _find_fork(repo, target, Path(scratch) / "index")
    stale = _drop_recorded_graft(repo, state)
    parents = _git(
        repo, "rev-parse", f"{twin}^@", env={"GIT_NO_REPLACE_OBJECTS": "1"}
    ).split()
    state.parent.mkdir(parents=True, exist_ok=True)
    pending = state.with_suffix(".json.tmp")
    pending.write_text(json.dumps({"twin": twin, "fork_point": fork}))
    pending.replace(state)
    try:
        _git(repo, "replace", "-f", "--graft", twin, *parents, fork)
        bridged = _merge_base(repo, "HEAD", target, is_graft_seen=True)
        if bridged != fork:
            raise HistoryBridgeError(
                f"the graft on {twin[:12]} gives a merge base of {bridged}, not the fork point {fork[:12]}"
            )
    except HistoryBridgeError:
        _drop_recorded_graft(repo, state)
        raise
    return HistoryBridge(True, fork, twin, None if stale == twin else stale)


def drop_history_bridge(repo: Path, state: Path) -> HistoryBridge:
    """Remove the graft :func:`bridge_history` recorded, if any; the next pass rebuilds it if still needed."""
    return HistoryBridge(False, None, None, _drop_recorded_graft(repo, state))
