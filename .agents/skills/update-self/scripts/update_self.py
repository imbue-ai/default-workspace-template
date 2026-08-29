#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic helpers for the safe, background-worker-driven update-self flow.

The update-self orchestration is mostly agent judgement (triage conflicts,
decide validation depth, work the report's impact analysis). This script owns
the parts that are *deterministic* and therefore belong in tested code rather
than agent prose:

``resolve-target``
    Resolve the ref to update to. Default is the latest **stable** ``minds-v*``
    tag (semver-sorted, ``-rc``/prerelease excluded) that is **not newer than the
    minds app driving this workspace**; an explicit override may name a specific
    tag, ``main``, or any other ref, and is reported back as exceeding the
    ceiling when it cannot be proven to sit at or below it.

    The ceiling exists because a workspace's template ships the code the outer
    app talks to (the system interface, the vendored ``mngr``), so updating past
    the app's own release would leave the workspace speaking a protocol its app
    does not know. It is read from the app itself (``GET /api/v1/app/version``,
    baseline-allowed through the latchkey gateway, no grant needed); when it
    cannot be read the command **fails** rather than silently updating uncapped.

    The output also carries ``held_back_by_ceiling`` -- whether the ceiling, and
    not the user, is why a newer release was not taken -- alongside
    ``latest_available``, the newest stable tag upstream *ignoring* the ceiling
    (``null`` if there is none) and so the release that flag names.

    A default target the workspace is **already on** is a refusal too: the command
    asks git whether the chosen ref is already an ancestor of ``HEAD``, rather
    than spending a backup, a worker, and a validation run on a merge that changes
    nothing. This is what makes the ceiling bite for a workspace sitting *at* it:
    with a newer release upstream the refusal names the app as the reason it
    cannot be had, and without one it is a plain "already up to date". A workspace
    *behind* the ceiling still updates to it.

``classify-merge``
    Split the files upstream changed into the reconciled **merged** set (local
    also diverged there -- validate) vs the clean **pulled-in** set (local left
    it untouched, so the merge just took upstream -- trust as upstream-tested),
    and map each file onto its change class and its test project. This drives
    both validation depth (merged set) and what ``apply`` must do to make the
    live workspace consistent with the merge. ``has_merge_work``
    is the mechanical half of the review-gate rule: true whenever the merged
    set is non-empty (any merge work at all happened). A false value is
    necessary but not sufficient to skip the gates -- the worker's impact
    analysis must also find no user-created code affected, and the worker must
    have authored no in-branch edits of its own (which this diff cannot see at
    all); the worker reference owns that half.

``changelog-entries``
    List ``changelog/`` entries newly added between two refs -- the raw input for
    the worker's "what's new" report.

``surface-chat-tab``
    Open this run's own chat tab in the workspace UI, so a user sent into the
    workspace by the minds app lands on the conversation performing the update.
    The interface can only place a tab in front of a client that is connected,
    and the user may still be on their way in, so the command detaches a helper
    that retries ``layout.py open`` until one takes it (or a deadline passes)
    and returns at once; the open is a no-op on a tab that is already there.

``bootstrap-skill``
    Stage the copy of the update-self skill (SKILL.md, references, scripts) that
    the rest of the pass runs, at a single fixed path, and report whether it
    differs from the local copy. Normally that staged copy is the target ref's
    *own* copy (extracted from the already-fetched object); when the ref predates
    the skill it is the local copy instead. Either way the fixed path is left
    populated with a runnable flow, so the lead and worker can dispatch against it
    by literal path without carrying any value across shell invocations. This is
    what lets the flow, after resolving the target, hand off to the update-self
    process *as it exists at the version being updated to* -- so fixes to the
    update flow itself are applied live rather than being gated on the
    possibly-stale local copy. ``differs`` gates only which SKILL.md prose the
    lead follows, not the path.

``apply``
    Land a prepared merge and make the live workspace consistent with it, as
    one atomic, idempotent, rollback-on-failure motion inside a single
    near-OOM-exempt process: merge (fast-forward for update-self, ordinary for
    update-system-interface), pre-apply state snapshots, dependency refresh,
    provisioner run, frontend build (or the worker's already-built bundle),
    pre-flight, restart, health probes, the VERSION_HISTORY.md ledger entry,
    and ``env-converge upgrade``. On any failure it reverts the entire merge
    as a forward revert commit and restores the pre-apply snapshots -- plain
    file copies needing no network, no package manager, and no working
    ``mngr``. A full-information marker under ``data/.state/update-apply/``
    makes an interruption detectable: written before the merge, updated per
    phase, cleared on every exit path. Exit codes: 0 applied / 2 rolled back /
    3 emergency / 1 precondition (nothing changed).

``recover``
    Roll an interrupted apply back from its marker. ``--if-stale`` is the
    unattended guard (bootstrap at boot, the recovery cron every ~5 minutes):
    it acts only when the marker's recorded process is dead and the marker has
    gone a grace period without an update, and silently exits 0 in every
    normal state. ``--no-restart`` is the boot path (nothing is running yet,
    so disk state is the whole job). Bare ``recover`` is the explicit
    agent-driven rollback. Exit codes: 0 rolled back (or nothing to do) /
    1 the tree restore failed, marker kept for the next pass / 3 the tree is
    rolled back but the pre-apply state or health could not be put back
    (emergency recorded, marker cleared).

Impact analysis -- which services and skills depend on a changed file -- is
deliberately NOT scripted here: it requires open-ended exploration (imports,
shelled-out scripts, API-surface coupling) that a deterministic helper would
only pretend to cover. The worker reference owns that recipe.

This file is the command line: argument parsing and the git-touching wrappers.
The logic lives in the sibling modules, imported by name from this directory
(the whole ``scripts/`` directory is staged and run as one unit):
``update_target`` (which ref to update to), ``update_classification`` (change
classes and the apply plan), ``update_apply_contract`` (every path, phase,
verdict and record the Minds app, bootstrap and the system interface read),
``update_layout``, ``update_banding``, ``update_runtime``,
``update_environment``, ``update_probes``, ``update_ledger``, and
``update_apply`` (the apply and recover orchestration). All of it is covered
by ``update_self_test.py``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Callable, Sequence

from update_apply import apply_update, recover
from update_apply_contract import (
    DEFAULT_RECOVER_GRACE_SECONDS,
    ENV_DRI_AGENT,
    RUN_VERDICTS,
    RunStatus,
    read_run_status,
    run_status_path,
    write_run_status,
)
from update_banding import protect_from_memory_shed
from update_classification import classify_merge
from update_runtime import ApplyPreconditionError, HttpClient, Runner, Spawner
from update_target import (
    CeilingUnavailableError,
    NoUpdateTargetError,
    already_current_message,
    fetch_app_template_ref,
    is_held_back_by_ceiling,
    pick_latest_stable_tag,
    resolve_target,
)

# The repo-relative directory holding the update-self skill (SKILL.md,
# references/, scripts/). Used by ``bootstrap-skill`` to extract the target
# ref's own copy of the flow.
SKILL_DIR_REL = ".agents/skills/update-self"


def _git(args: Sequence[str], repo_root: Path) -> str:
    """Run a git command in ``repo_root`` and return its stdout (stripped)."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _list_names(output: str) -> list[str]:
    return [line for line in output.splitlines() if line]


def _is_already_merged(ref: str, repo_root: Path) -> bool:
    """Whether ``ref`` is already reachable from ``HEAD``, so merging it changes nothing.

    Cannot use :func:`_git` (``check=True``): exit 1 is the ordinary "not an
    ancestor" answer, not a failure. Any other code is a real git error -- a ref
    that does not resolve, or no ``HEAD`` at all -- and is raised rather than read
    as "not merged".
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def _repo_root(args: argparse.Namespace) -> Path:
    """The ``--repo-root`` value, whether given before or after the subcommand.

    The attribute is absent (not defaulted) when the flag was never passed --
    see the ``SUPPRESS`` note in ``main`` -- so the cwd fallback lives here.
    """
    return getattr(args, "repo_root", Path.cwd())


def _cmd_resolve_target(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    tags = _list_names(
        _git(["tag", "--list", "minds-v*"], repo_root)
        if args.local_tags
        else _git(["ls-remote", "--tags", "--refs", args.remote, "minds-v*"], repo_root)
    )
    if not args.local_tags:
        # ``ls-remote`` lines are ``<sha>\trefs/tags/<tag>``; take the tag.
        tags = [line.rsplit("/", 1)[-1] for line in tags]
    ceiling = args.ceiling if args.ceiling is not None else fetch_app_template_ref()
    target = resolve_target(args.override, tags, remote=args.remote, ceiling=ceiling)
    latest_available = pick_latest_stable_tag(tags)
    is_held_back = is_held_back_by_ceiling(
        resolved_ref=target.ref,
        latest_available=latest_available,
        ceiling=target.ceiling,
        has_override=args.override is not None,
    )
    # Only the default path: an override was asked for by name, and the rule that
    # it is never silently blocked outranks saving a no-op merge.
    if args.override is None and _is_already_merged(target.ref, repo_root):
        raise NoUpdateTargetError(
            already_current_message(
                target.ref, latest_available, target.ceiling, is_held_back
            )
        )
    print(
        json.dumps(
            {
                "ref": target.ref,
                "kind": target.kind,
                "ceiling": target.ceiling,
                "exceeds_ceiling": target.exceeds_ceiling,
                "latest_available": latest_available,
                "held_back_by_ceiling": is_held_back,
            }
        )
    )
    return 0


def _cmd_classify_merge(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    # A --local that already contains --target is a degenerate invocation: the
    # merge base collapses to the target itself, the "upstream changed" diff is
    # empty, and an 800-file merge silently classifies as nothing at all. This
    # happens when the guide's post-merge `--local HEAD^1` is re-run after any
    # commit was added on top of the merge (HEAD^1 is then the merge commit,
    # not the pre-merge local). Refuse loudly instead of printing the empty
    # classification.
    contains = subprocess.run(
        ["git", "merge-base", "--is-ancestor", args.target, args.local],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if contains.returncode not in (0, 1):
        contains.check_returncode()
    if contains.returncode == 0:
        print(
            f"error: --local ({args.local}) already contains --target "
            f"({args.target}), so the merge base collapses to the target and "
            "every upstream change would classify as empty. Did you mean the "
            "merge commit's first parent? While HEAD is the merge commit that "
            "is --local HEAD^1; after further commits on top, name the merge "
            "commit itself (--local <merge-sha>^1).",
            file=sys.stderr,
        )
        return 1
    base = args.base or _git(["merge-base", args.local, args.target], repo_root)
    upstream_changed = _list_names(
        _git(["diff", "--name-only", base, args.target], repo_root)
    )
    local_changed = _list_names(
        _git(["diff", "--name-only", base, args.local], repo_root)
    )
    result = classify_merge(upstream_changed, local_changed)
    print(
        json.dumps(
            {
                "base": base,
                "merged": result.merged,
                "pulled_in": result.pulled_in,
                "reveal_classes_merged": result.reveal_classes_merged,
                "reveal_classes_pulled_in": result.reveal_classes_pulled_in,
                "projects_to_validate": result.projects_to_validate,
                "has_merge_work": result.has_merge_work,
            },
            indent=2,
        )
    )
    return 0


def _cmd_changelog_entries(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    # Per-PR changelog entries live in a ``changelog/`` dir under each project
    # bucket -- ``system/changelog/``, ``.agents/changelog/``, and
    # ``system/{libs,services,apps}/<name>/changelog/`` (see
    # system/scripts/check_changelog_entries.py for the bucket definition).
    # Match every one of them at any depth with a single glob rather than one
    # dir alone, or the "what's new" digest silently drops everything landed
    # under the bucketed layout. Exclude the vendored subtree, which carries
    # its own separate changelog system. ``top`` anchors both pathspecs at the
    # repository root: a git pathspec is otherwise relative to the cwd, so run
    # from a subdirectory the glob matched nothing and the digest came back
    # empty with no error.
    added = _list_names(
        _git(
            [
                "diff",
                "--name-only",
                "--diff-filter=A",
                args.base,
                args.target,
                "--",
                ":(top,glob)**/changelog/*",
                ":(top,exclude)system/vendor",
            ],
            repo_root,
        )
    )
    print(json.dumps({"added": added}))
    return 0


# How long the detached helper keeps trying to place the tab. Generous enough
# to cover a user arriving after a stopped machine's cold boot; past it the
# app's own copy naming the tab is the fallback.
SURFACE_CHAT_TAB_DEADLINE_SECONDS = 600.0

SURFACE_CHAT_TAB_RETRY_SECONDS = 5.0


def wait_and_open_chat_tab(
    try_open: Callable[[], bool],
    deadline_seconds: float,
    retry_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Call ``try_open`` until it succeeds or the deadline passes; whether it did.

    Stops on the first success: a tab is surfaced once, and re-opening it later
    would yank a user who has since moved on back to it.
    """
    started_at = monotonic()
    while True:
        if try_open():
            return True
        if monotonic() - started_at >= deadline_seconds:
            return False
        sleep(retry_seconds)


def _try_open_chat_tab(repo_root: Path, chat_name: str) -> bool:
    result = subprocess.run(
        [sys.executable, "system/scripts/layout.py", "open", f"chat:{chat_name}"],
        cwd=repo_root,
        capture_output=True,
    )
    return result.returncode == 0


def _cmd_surface_chat_tab(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    if args.wait:
        return (
            0
            if wait_and_open_chat_tab(
                lambda: _try_open_chat_tab(repo_root, args.name),
                deadline_seconds=SURFACE_CHAT_TAB_DEADLINE_SECONDS,
                retry_seconds=SURFACE_CHAT_TAB_RETRY_SECONDS,
            )
            else 1
        )
    # Detached so the lead's tool call returns now rather than after the user
    # arrives: its own session, and no inherited stdio for the caller's shell
    # to wait on.
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "surface-chat-tab",
            "--name",
            args.name,
            "--repo-root",
            str(repo_root),
            "--wait",
        ],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


def _cmd_bootstrap_skill(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    dest = Path(args.dest)
    dest_root = (dest if dest.is_absolute() else repo_root / dest).resolve()
    staged_skill = dest_root / SKILL_DIR_REL

    # Always stage into a clean dir. The flow runs the skill from ``staged_skill``
    # unconditionally (a single fixed path the lead and worker both reference by
    # literal -- no state carried across shell invocations), so this command must
    # leave a runnable copy there in *every* case, including the ref-predates-skill
    # fallback below.
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True)

    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{args.ref}:{SKILL_DIR_REL}"],
        cwd=repo_root,
        capture_output=True,
    )
    if exists.returncode != 0:
        # The target ref predates the skill, so there is no target copy to hand
        # off to: stage the *local* copy at the fixed path (so the worker still
        # finds the flow there) and report ``differs=False`` -- the caller stays
        # on the local flow. Skip ``__pycache__`` so stale bytecode caches
        # never ride along.
        shutil.copytree(
            repo_root / SKILL_DIR_REL,
            staged_skill,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        print(
            json.dumps(
                {"skill_dir": str(staged_skill), "differs": False, "ref": args.ref}
            )
        )
        return 0

    # Extract the ref's own copy of the skill via ``git archive`` (reads the
    # already-fetched object, no network, no working-tree mutation). The archive
    # lays the tree down under ``SKILL_DIR_REL``.
    archive = subprocess.run(
        ["git", "archive", args.ref, SKILL_DIR_REL],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest_root, filter="data")

    # Whether the ref's skill differs from the local working-tree copy. Let git
    # do the compare: ``git diff`` ignores untracked files, so the ``__pycache__/
    # *.pyc`` that importing the script drops into ``scripts/`` never registers as
    # a spurious difference. ``--quiet`` exits 0 if identical, 1 on any
    # difference; ``check_returncode`` surfaces any other code as a real git error.
    diff = subprocess.run(
        ["git", "diff", "--quiet", args.ref, "--", SKILL_DIR_REL],
        cwd=repo_root,
        capture_output=True,
    )
    if diff.returncode not in (0, 1):
        diff.check_returncode()
    differs = diff.returncode == 1
    print(
        json.dumps(
            {"skill_dir": str(staged_skill), "differs": differs, "ref": args.ref}
        )
    )
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    return apply_update(
        args.merge_ref,
        _repo_root(args).resolve(),
        ff_only=args.ff_only,
        worker_bundle=args.worker_bundle,
        target_ref=args.target_ref,
        runner=Runner(),
        http=HttpClient(),
        spawner=Spawner(),
    )


def _cmd_run_status_start(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    chat_agent_name = args.chat or os.environ.get(ENV_DRI_AGENT, "")
    if not chat_agent_name:
        print(
            f"error: no chat agent name: pass --chat or run with {ENV_DRI_AGENT} set.",
            file=sys.stderr,
        )
        return 1
    now = time.time
    write_run_status(
        RunStatus(
            chat_agent_name=chat_agent_name,
            started_at=now(),
            updated_at=0.0,
        ),
        repo_root,
        now,
    )
    print(f"Recorded the run's start for {chat_agent_name}.")
    return 0


def _cmd_run_status_verdict(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    now = time.time
    recorder = args.chat or os.environ.get(ENV_DRI_AGENT, "")
    # A verdict with no record of its own start still deserves one: the app
    # can at least report how the run ended, and the env names the agent.
    status = _run_status_for_recorder(repo_root, recorder, now)
    status.verdict = args.verdict
    status.detail = args.detail
    status.resulting_ref = args.resulting_ref
    status.in_place_compatible_ref = args.in_place_compatible_ref
    status.verdict_at = now()
    # A verdict ends the run, so a hold it was recorded under ends with it,
    # and so does the worker it had delegated to.
    status.is_holding = False
    status.hold_detail = ""
    status.worker_agent_name = None
    write_run_status(status, repo_root, now)
    print(f"Recorded the {args.verdict} verdict.")
    return 0


def _run_status_for_recorder(
    repo_root: Path, recorder: str, now: Callable[[], float]
) -> RunStatus:
    """The current run record if it is ``recorder``'s, else a fresh one under that name.

    The app matches a record to a workspace's row by chat name, so writing
    this pass's facts onto another pass's record would file them under a run
    the app is not watching.
    """
    status = read_run_status(repo_root)
    if status is not None and recorder and status.chat_agent_name != recorder:
        sys.stderr.write(
            f"warning: {run_status_path(repo_root)} records {status.chat_agent_name or '<unnamed>'}, "
            f"not {recorder}; recording under {recorder} instead.\n"
        )
        status = None
    if status is None:
        status = RunStatus(
            chat_agent_name=recorder,
            started_at=now(),
            updated_at=0.0,
        )
    return status


def _cmd_run_status_delegate(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    now = time.time
    recorder = args.chat or os.environ.get(ENV_DRI_AGENT, "")
    status = _run_status_for_recorder(repo_root, recorder, now)
    status.worker_agent_name = args.worker
    write_run_status(status, repo_root, now)
    print(f"Recorded the hand-off to worker {args.worker}.")
    return 0


def _cmd_run_status_hold(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    now = time.time
    recorder = args.chat or os.environ.get(ENV_DRI_AGENT, "")
    status = _run_status_for_recorder(repo_root, recorder, now)
    status.is_holding = True
    status.hold_detail = args.detail
    write_run_status(status, repo_root, now)
    print("Recorded the hold.")
    return 0


def _cmd_run_status_resume(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    now = time.time
    recorder = args.chat or os.environ.get(ENV_DRI_AGENT, "")
    status = _run_status_for_recorder(repo_root, recorder, now)
    status.is_holding = False
    status.hold_detail = ""
    write_run_status(status, repo_root, now)
    print("Cleared the hold.")
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    return recover(
        _repo_root(args).resolve(),
        if_stale=args.if_stale,
        grace_seconds=args.grace_seconds,
        no_restart=args.no_restart,
        runner=Runner(),
        http=HttpClient(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    # ``--repo-root`` lives on a shared parent parser so it is accepted both
    # before and after the subcommand (an option defined only on the top-level
    # parser would reject ``update_self.py <subcommand> --repo-root X``).
    # The default must be ``SUPPRESS``, not a value: on Python < 3.13 a
    # subparser re-applies its defaults over the namespace the top-level parser
    # already filled in (bpo-9351), so a concrete default here would clobber a
    # ``--repo-root`` given before the subcommand. With ``SUPPRESS`` the
    # attribute is only set when the flag is actually passed; ``_repo_root``
    # falls back to cwd.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="Repo root the git subcommands run in (default: cwd).",
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_parser = sub.add_parser(
        "resolve-target", help="Resolve the update target ref.", parents=[common]
    )
    resolve_parser.add_argument(
        "--override",
        default=None,
        help="A tag, 'main', or any ref to update to (default: latest stable "
        "minds-v* tag).",
    )
    resolve_parser.add_argument(
        "--remote", default="upstream", help="Remote to read tags from."
    )
    resolve_parser.add_argument(
        "--local-tags",
        action="store_true",
        help="Read already-fetched local tags instead of querying the remote.",
    )
    resolve_parser.add_argument(
        "--ceiling",
        default=None,
        help="Newest template ref to allow (default: ask the running minds app). "
        "A non-release ref (e.g. a branch) imposes no ceiling.",
    )
    resolve_parser.set_defaults(func=_cmd_resolve_target)

    classify_parser = sub.add_parser(
        "classify-merge",
        help="Split upstream-changed files into merged vs pulled-in and classify each.",
        parents=[common],
    )
    classify_parser.add_argument(
        "--target", required=True, help="The upstream ref being merged in."
    )
    classify_parser.add_argument(
        "--local",
        default="HEAD",
        help="The local ref (default HEAD; use HEAD^1 after the merge commit).",
    )
    classify_parser.add_argument(
        "--base",
        default=None,
        help="Merge base (default: git merge-base <local> <target>).",
    )
    classify_parser.set_defaults(func=_cmd_classify_merge)

    changelog_parser = sub.add_parser(
        "changelog-entries",
        help="List per-PR changelog entries newly added between two refs "
        "(across every project bucket, not just the top-level changelog/).",
        parents=[common],
    )
    changelog_parser.add_argument("--base", required=True, help="Base ref.")
    changelog_parser.add_argument("--target", required=True, help="Target ref.")
    changelog_parser.set_defaults(func=_cmd_changelog_entries)

    surface_parser = sub.add_parser(
        "surface-chat-tab",
        help="Open this run's own chat tab once a workspace client can show it.",
        parents=[common],
    )
    surface_parser.add_argument(
        "--name", required=True, help="This run's chat agent name ($MNGR_AGENT_NAME)."
    )
    surface_parser.add_argument(
        "--wait",
        action="store_true",
        help="Run the retry loop in this process (what the detached helper does) instead of detaching one.",
    )
    surface_parser.set_defaults(func=_cmd_surface_chat_tab)

    bootstrap_parser = sub.add_parser(
        "bootstrap-skill",
        help="Extract the target ref's own update-self skill into a staging dir "
        "and report whether it differs from the local copy.",
        parents=[common],
    )
    bootstrap_parser.add_argument(
        "--ref",
        required=True,
        help="The resolved target ref to extract the skill from.",
    )
    bootstrap_parser.add_argument(
        "--dest",
        default="data/.tasks/update-self/skill-at-target",
        help="Staging dir the skill is extracted into (default: "
        "data/.tasks/update-self/skill-at-target).",
    )
    bootstrap_parser.set_defaults(func=_cmd_bootstrap_skill)

    apply_parser = sub.add_parser(
        "apply",
        help="Land a prepared merge and make the live workspace consistent with "
        "it: one atomic, idempotent, rollback-on-failure motion (merge, "
        "snapshots, env refresh, provisioner, build, pre-flight, restart, "
        "probes, ledger, env-converge).",
        parents=[common],
    )
    apply_parser.add_argument(
        "--merge-ref",
        required=True,
        help="The worker branch / prepared merge commit to land.",
    )
    apply_parser.add_argument(
        "--ff-only",
        action="store_true",
        help="Require a fast-forward landing (the update-self flow; the worker "
        "branched off this HEAD). Default is an ordinary merge "
        "(update-system-interface).",
    )
    apply_parser.add_argument(
        "--worker-bundle",
        default=None,
        help="Path to the worker's already-built static/ bundle (the artifact "
        "the worker validated); a live build is the fallback.",
    )
    apply_parser.add_argument(
        "--target-ref",
        default=None,
        help="The release this update lands (update-self mode): enables the "
        "VERSION_HISTORY.md ledger entry and the post-success "
        "`env-converge upgrade`.",
    )
    apply_parser.set_defaults(func=_cmd_apply)

    recover_parser = sub.add_parser(
        "recover",
        help="Roll back an interrupted apply from its marker (dependency-free: "
        "git restore + snapshot copies).",
        parents=[common],
    )
    recover_parser.add_argument(
        "--if-stale",
        action="store_true",
        help="Unattended guard (boot/cron): act only when the marker's process "
        "is dead and the marker is older than the grace period; silently "
        "exit 0 in every normal state.",
    )
    recover_parser.add_argument(
        "--grace-seconds",
        type=float,
        default=DEFAULT_RECOVER_GRACE_SECONDS,
        help="How long a marker must have gone without an update before "
        "--if-stale acts (default: %(default)s).",
    )
    recover_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Boot path: restore disk state only, without service restarts or "
        "health probes (services boot fresh from the restored state).",
    )
    recover_parser.set_defaults(func=_cmd_recover)

    run_status_parser = sub.add_parser(
        "run-status",
        help="Record this run for the Minds app (data/.state/update-apply/run.json).",
        parents=[common],
    )
    run_status_sub = run_status_parser.add_subparsers(
        dest="run_status_command", required=True
    )
    start_parser = run_status_sub.add_parser(
        "start",
        help="Record that a run has begun, overwriting the previous run's record.",
        parents=[common],
    )
    start_parser.add_argument(
        "--chat",
        default="",
        help=f"This run's chat agent name (default: ${ENV_DRI_AGENT}).",
    )
    start_parser.set_defaults(func=_cmd_run_status_start)
    verdict_parser = run_status_sub.add_parser(
        "verdict",
        help="Record the run's one terminal verdict onto the current record.",
        parents=[common],
    )
    verdict_parser.add_argument(
        "verdict",
        choices=RUN_VERDICTS,
        help="How the run ended.",
    )
    verdict_parser.add_argument(
        "--chat",
        default="",
        help=f"This run's chat agent name (default: ${ENV_DRI_AGENT}).",
    )
    verdict_parser.add_argument(
        "--detail",
        default="",
        help="One plain-language line for the Minds app's modal.",
    )
    verdict_parser.add_argument(
        "--resulting-ref",
        default="",
        help="The ref the workspace is on now (success verdicts).",
    )
    verdict_parser.add_argument(
        "--in-place-compatible-ref",
        default="",
        help="On REFUSED/NEEDS_RECREATION: the newest ref that could still be "
        "applied in place, when one exists.",
    )
    verdict_parser.set_defaults(func=_cmd_run_status_verdict)
    delegate_parser = run_status_sub.add_parser(
        "delegate",
        help="Record the background worker this run has handed its work to, so "
        "the app reads the worker's liveness while this chat waits on it.",
        parents=[common],
    )
    delegate_parser.add_argument(
        "worker",
        help="The worker agent's name (as `mngr list` shows it).",
    )
    delegate_parser.add_argument(
        "--chat",
        default="",
        help=f"This run's chat agent name (default: ${ENV_DRI_AGENT}).",
    )
    delegate_parser.set_defaults(func=_cmd_run_status_delegate)
    hold_parser = run_status_sub.add_parser(
        "hold",
        help="Record that the run has stopped to ask the user something, and what.",
        parents=[common],
    )
    hold_parser.add_argument(
        "--chat",
        default="",
        help=f"This run's chat agent name (default: ${ENV_DRI_AGENT}).",
    )
    hold_parser.add_argument(
        "--detail",
        default="",
        help="One plain-language line naming what the run is waiting on.",
    )
    hold_parser.set_defaults(func=_cmd_run_status_hold)
    resume_parser = run_status_sub.add_parser(
        "resume",
        help="Clear the hold: the user answered and the run is moving again.",
        parents=[common],
    )
    resume_parser.add_argument(
        "--chat",
        default="",
        help=f"This run's chat agent name (default: ${ENV_DRI_AGENT}).",
    )
    resume_parser.set_defaults(func=_cmd_run_status_resume)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CeilingUnavailableError, NoUpdateTargetError, ApplyPreconditionError) as e:
        # These carry the "why you cannot update right now" explanation the lead
        # relays to the user, so print the message alone: a traceback would bury it
        # and read as a crash rather than a refusal.
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: git command failed: {e}", file=sys.stderr)
        return 1


def _shed_protection_target(argv: Sequence[str]) -> Path | None:
    """The repo root to band for when ``argv`` names apply/recover, else None.

    Only those two band themselves: they are the motions that can be
    interrupted half-way through replacing what the workspace runs, and the
    ones holding the only copies of what it ran before. A crude parse rather
    than argparse, because banding must happen before ``main`` does anything.
    """
    tokens = list(argv)
    repo_root = Path.cwd()
    subcommand: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--repo-root" and index + 1 < len(tokens):
            repo_root = Path(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--repo-root="):
            repo_root = Path(token.split("=", 1)[1])
            index += 1
            continue
        if subcommand is None and not token.startswith("-"):
            subcommand = token
        index += 1
    if subcommand in ("apply", "recover"):
        return repo_root
    return None


if __name__ == "__main__":
    _banding_root = _shed_protection_target(sys.argv[1:])
    if _banding_root is not None:
        protect_from_memory_shed(_banding_root.resolve())
    sys.exit(main())
