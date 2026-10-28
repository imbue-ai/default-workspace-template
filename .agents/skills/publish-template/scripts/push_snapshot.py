#!/usr/bin/env python3
"""Publish an assembled template: one snapshot commit, pushed through the latchkey gateway.

    uv run --no-project --with 'pydantic>=2' python push_snapshot.py \\
        --worktree <WT> --parent <commit> --repo <owner>/<repo> \\
        --message <commit message> [--tag <name>]

`--parent` is `BASE_REF` for a first publish and the published tip for an update.
In order, and stopping at the first failure with nothing pushed:

  1. the worktree must be a linked worktree -- never the live workspace or any
     repo's main checkout, whose tree is everything the agent built -- and
     clean, since anything uncommitted would be left out;
  2. the pre-push gate: `validate_template.py --pre-push` over the worktree;
  3. one commit is minted from the worktree's final tree, parented on `--parent`
     (a parentless snapshot would share no merge-base with the template, so an
     adopter could never merge it cleanly). The worktree's own branch is never
     pushed: its intermediate commits hold the pre-cleanup states (a value a
     published-version modification removed, the raw assembly), and only the
     final tree may leave the machine;
  4. it is pushed to `main` as a fast-forward, never forced. A rejection means
     the published `main` moved since the parent was read, and someone else's
     work is there.

`--tag` then puts an annotated tag on the new commit (replacing a local one of the
same name) and pushes it without force, so a tag already on the remote is never
moved. A failure there is a follow-up, since the version itself is already
published.

The push goes through the gateway's git proxy, which injects the GitHub
credential itself: no token is in the URL, on disk, or in git config. Prints
the published commit.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from pathlib import Path

from validate_template import main as validate_template

# What a refused push most likely means, matched against git's stderr. Ordered so
# the specific causes win: GitHub's workflow-scope refusal is also printed as a
# "[remote rejected]" line. The stderr also carries the remote URL, so no needle
# may be something a repo name can contain.
_PUSH_FAILURE_HINTS = (
    (
        ("create or update workflow", "`workflow` scope"),
        "the stored GitHub credential cannot push .github/workflows/ files; tell the "
        "user and stop -- never strip files to get the push through",
    ),
    (
        ("not permitted",),
        "the github-git-write permission is missing; request it again",
    ),
    (
        ("http 413", "too large"),
        "the user's Imbue Studio app is older than the gateway's raised body cap and "
        "cannot proxy a push this size; tell the user and stop",
    ),
    (
        ("(non-fast-forward)", "fetch first"),
        "the published main moved since its tip was read; someone else pushed there. "
        "Stop and tell the user -- never force-push over it",
    ),
)


def hint_for_push_failure(output: str) -> str | None:
    lowered = output.lower()
    for needles, hint in _PUSH_FAILURE_HINTS:
        if any(needle in lowered for needle in needles):
            return hint
    return None


class PushSnapshotError(Exception):
    """Raised when a step fails; nothing after it ran."""


def _git(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=worktree, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise PushSnapshotError(
            f"git {' '.join(args[:2])} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _gateway_git_args(repo: str) -> tuple[list[str], str]:
    """The `-c` options and URL for one git command through the gateway."""
    gateway = os.environ.get("LATCHKEY_GATEWAY")
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD")
    if not gateway or not password:
        raise PushSnapshotError(
            "LATCHKEY_GATEWAY and LATCHKEY_GATEWAY_PASSWORD must be set (they are in a workspace)"
        )
    options = ["-c", f"http.extraHeader=X-Latchkey-Gateway-Password: {password}"]
    override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE")
    if override:
        options += [
            "-c",
            f"http.extraHeader=X-Latchkey-Gateway-Permissions-Override: {override}",
        ]
    return options, f"{gateway}/gateway/https://github.com/{repo}.git"


def _push(worktree: Path, repo: str, refspec: str) -> None:
    options, url = _gateway_git_args(repo)
    completed = subprocess.run(
        ["git", *options, "push", url, refspec],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return
    output = completed.stderr.strip()
    hint = hint_for_push_failure(output)
    if hint is not None:
        raise PushSnapshotError(f"the push was refused: {output}\n  -> {hint}")
    raise PushSnapshotError(f"the push failed: {output}")


def _require_linked_worktree(worktree: Path) -> None:
    """Refuse a repo's main worktree: the assembly always runs in a linked one.

    The same check `build_template.sh` makes. An unset `$WT` passed as
    `--worktree ""` resolves to the cwd, which is usually the live workspace.
    """
    if not worktree.is_dir():
        raise PushSnapshotError(
            f"{worktree} does not exist; re-resolve $WT from the worker's done report "
            "and pass it as --worktree"
        )
    git_dir = (worktree / _git(worktree, "rev-parse", "--absolute-git-dir")).resolve()
    common_dir = (worktree / _git(worktree, "rev-parse", "--git-common-dir")).resolve()
    if git_dir == common_dir:
        raise PushSnapshotError(
            f"{worktree} is a repo's main worktree, not the assembly worktree; only "
            "$WT's tree is ever published -- re-resolve $WT and pass it as --worktree"
        )


def mint_snapshot(worktree: Path, parent: str, message: str) -> str:
    """One commit holding the worktree's final tree, parented on `parent`."""
    try:
        parent_commit = _git(
            worktree, "rev-parse", "--verify", "-q", f"{parent}^{{commit}}"
        )
    except PushSnapshotError as e:
        raise PushSnapshotError(
            f"--parent {parent!r} is not a commit in {worktree}; re-resolve it "
            "(BASE_REF for a first publish, the published tip for an update)"
        ) from e
    return _git(
        worktree, "commit-tree", "HEAD^{tree}", "-p", parent_commit, "-m", message
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish an assembled template as one snapshot commit."
    )
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument(
        "--parent", required=True, help="BASE_REF, or the published tip for an update"
    )
    parser.add_argument("--repo", required=True, help="<owner>/<repo>")
    parser.add_argument("--message", required=True)
    parser.add_argument(
        "--tag", help="An annotated tag to create on the snapshot and push"
    )
    args = parser.parse_args(argv)
    worktree = args.worktree.resolve()

    try:
        _require_linked_worktree(worktree)
        if _git(worktree, "status", "--porcelain"):
            raise PushSnapshotError(
                f"{worktree} has uncommitted changes; commit them first, so the push carries them"
            )
        # stdout carries only the published commit, for the caller to read.
        with contextlib.redirect_stdout(sys.stderr):
            gate_status = validate_template([str(worktree), "--pre-push"])
        if gate_status != 0:
            raise PushSnapshotError("the pre-push gate failed (see above)")
        snapshot = mint_snapshot(worktree, args.parent, args.message)
        _push(worktree, args.repo, f"{snapshot}:refs/heads/main")
    except PushSnapshotError as e:
        print(f"push_snapshot: {e}", file=sys.stderr)
        return 1

    if args.tag:
        try:
            _git(worktree, "tag", "-f", "-a", args.tag, snapshot, "-m", args.tag)
            _push(worktree, args.repo, f"refs/tags/{args.tag}")
        except PushSnapshotError as e:
            print(
                f"push_snapshot: follow-up: {snapshot} is published, but the tag "
                f"{args.tag} was not: {e}",
                file=sys.stderr,
            )
    print(snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
