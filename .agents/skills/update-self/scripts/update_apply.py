"""``apply`` lands a prepared merge and makes the live workspace consistent with
it, as one deterministic, idempotent, rollback-on-failure motion: merge,
state snapshots, dependency refresh, provisioner run, frontend build (or the
worker's already-built bundle), pre-flight, restart, health probes, the
version-history ledger entry, and the environment converge. On any failure it
reverts the entire merge and restores the pre-apply snapshots -- a recovery
path needing no network, no package manager, and no working ``mngr``.

It serves every update flow, not just update-self: ``update-system-interface``
hands it an ordinary merge and its own already-built bundle, so both flows
land the same way. What it must protect is therefore whole-repo -- the root
venv, the two uv tool environments, ``node_modules`` and the built bundle are
all copied aside first -- and what it must survive includes its own death,
which is what the persistent marker and ``recover`` are for.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

from update_apply_contract import (
    ENV_DRI_AGENT,
    PHASE_BUILT,
    PHASE_MERGED,
    PHASE_PROVISIONED,
    PHASE_REFRESHED,
    PHASE_RESTARTED,
    PHASE_SNAPSHOTTED,
    PHASE_STARTED,
    ApplyMarker,
    SnapshotRecord,
    clear_emergency,
    clear_marker,
    clear_provision_incomplete,
    default_is_pid_a_live_apply,
    provision_incomplete_path,
    read_marker,
    snapshots_root,
    write_emergency,
    write_marker,
    write_provision_incomplete,
)
from update_banding import ExpendWrapper, as_expendable, keep_protected
from update_classification import (
    ApplyPlan,
    AppTool,
    plan_apply,
    read_app_tools,
    read_provisioner_inputs,
)
from update_environment import (
    BACKEND_SNAPSHOT_NAMES,
    ENVIRONMENT_REFRESH_TIMEOUT_SECONDS,
    discard_snapshots,
    refresh_app_tools,
    refresh_backend_dependencies,
    restore_snapshots,
    run_provisioner,
    take_snapshots,
    tool_snapshot_name,
)
from update_layout import (
    BUNDLE_STAMP_FILENAME,
    DEFAULT_WORKSPACE_URL,
    ENV_WORKSPACE_URL,
    FRONTEND_BUILD_INDEX,
    FRONTEND_DIR,
    PROVISIONER_SCRIPT,
    STATIC_DIR,
)
from update_ledger import LedgerCommitError, write_version_history_entry
from update_probes import (
    HEALTH_ATTEMPTS,
    HEALTH_INTERVAL_SECONDS,
    HEALTH_PATH,
    describe_frontend_failure,
    preflight,
    refresh_workspace_view,
    wait_healthy,
)
from update_runtime import (
    ApplyFailed,
    ApplyPreconditionError,
    HttpClient,
    Runner,
    Spawner,
    abort_in_progress_merge,
    assert_clean_tree,
    detail_block,
    diff_name_status,
    git_out,
    run_checked,
)

# Per-step wall-clock budgets for the forward apply steps. Nothing about an
# update should take anywhere near an hour, yet the old reveal ran for 1h28m
# before anyone asked whether it was stuck -- so a hung step (an `npm ci`
# stalled under load, a provisioner download that never completes) has to
# become a rollback with a named phase rather than an open-ended wait. Sized
# generously; the per-phase timings the marker records are the input for
# tuning them down. The rollback and recovery paths carry no budgets: there is
# no further rollback to absorb a timeout there.
_NPM_CI_TIMEOUT_SECONDS = 1200.0

_FRONTEND_BUILD_TIMEOUT_SECONDS = 1200.0


_RESTART_TIMEOUT_SECONDS = 600.0

_ENV_CONVERGE_TIMEOUT_SECONDS = 1200.0


def _restore_tree(
    name_status: Sequence[tuple[str, str]],
    rollback_to: str,
    repo_root: Path,
    runner: Runner,
) -> None:
    """Restore every changed path to its ``rollback_to`` state, staged for commit.

    Added-since paths are removed; modified/deleted paths are checked out from
    the known-good revision. Idempotent: re-running over an already-restored
    tree checks out and removes the same paths to the same state.
    """
    for status, path in name_status:
        if status.startswith("A"):
            runner.run(
                ["git", "rm", "--force", "--ignore-unmatch", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            runner.run(
                ["git", "checkout", rollback_to, "--", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )


# The subject every rollback commit carries. Load-bearing, not cosmetic: the
# rollback is a *forward* revert, so the reverted merge stays in HEAD's
# ancestry and an ancestry check alone cannot tell "already applied" from
# "applied and undone". This prefix is how :func:`_has_rollback_since` tells
# them apart.
_ROLLBACK_SUBJECT_PREFIX = "Roll back update apply"


def _commit_rollback(
    repo_root: Path, runner: Runner, rollback_to: str, reason: str
) -> None:
    """Commit the staged restore as a forward revert, if there is anything to
    commit (a re-entered rollback may find the commit already landed).

    The gate asks the index, not ``git status``: the commit stages nothing of
    its own, and status also lists untracked files, over which a commit of an
    empty index fails -- which would keep the marker at every boot.
    """
    staged_argv = ["git", "diff", "--cached", "--quiet"]
    staged = runner.run(
        staged_argv,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    returncode = getattr(staged, "returncode", 0)
    if returncode == 0:
        return
    if returncode != 1:
        raise subprocess.CalledProcessError(
            returncode,
            staged_argv,
            output=getattr(staged, "stdout", ""),
            stderr=getattr(staged, "stderr", ""),
        )
    message = f"{_ROLLBACK_SUBJECT_PREFIX} (restore to {rollback_to[:12]})\n\n{reason}"
    runner.run(
        ["git", "commit", "--no-verify", "-m", message],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )


def _is_merge_landed(merge_ref: str, repo_root: Path, runner: Runner) -> bool:
    """Whether ``merge_ref`` is already reachable from ``HEAD``."""
    result = runner.run(
        ["git", "merge-base", "--is-ancestor", merge_ref, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    returncode = getattr(result, "returncode", 0)
    if returncode not in (0, 1):
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise ApplyPreconditionError(f"could not resolve {merge_ref}: {stderr}")
    return returncode == 0


def _has_rollback_since(merge_ref: str, repo_root: Path, runner: Runner) -> bool:
    """Whether a rollback commit sits between ``merge_ref`` and ``HEAD``.

    The one signal that distinguishes an already-*applied* merge from an
    already-*undone* one, both of which are ancestors of ``HEAD``: only the
    undone one has a :data:`_ROLLBACK_SUBJECT_PREFIX` commit on top of it.
    Scoped to ``merge_ref..HEAD``, and matching a subject this script itself
    writes, so ordinary workspace commits can never trip it.
    """
    log = git_out(runner, repo_root, ["log", "--format=%s", f"{merge_ref}..HEAD"])
    return any(line.startswith(_ROLLBACK_SUBJECT_PREFIX) for line in log.splitlines())


def _expected_frontend_tree_hash(repo_root: Path, runner: Runner) -> str | None:
    """The merged tree's frontend-source tree hash, or ``None`` when git cannot
    answer (verification then degrades to the index-only check with a warning
    rather than blocking an apply over a read failure)."""
    result = runner.run(
        ["git", "rev-parse", f"HEAD:{FRONTEND_DIR}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 0) != 0:
        return None
    return (getattr(result, "stdout", "") or "").strip() or None


def _read_bundle_stamp(bundle_dir: Path) -> str | None:
    try:
        return (bundle_dir / BUNDLE_STAMP_FILENAME).read_text().strip() or None
    except OSError:
        return None


def _worker_bundle_reject_reason(
    worker_bundle: str | None, expected_hash: str | None
) -> str | None:
    """Why a ``--worker-bundle`` cannot be installed as-is, or ``None``.

    The stamp check is what keeps a stale-but-populated directory from being
    copied over the live UI while the source says otherwise -- the "source
    updated, UI didn't" state a user once had to catch by eye. A rejected
    bundle is not a failed apply: the live build remains the fallback, so the
    correct bundle is still produced (only the "what the worker validated is
    what ships" guarantee is lost, which the caller's note says).
    """
    if worker_bundle is None:
        return None
    source = Path(worker_bundle)
    if not (source / "index.html").exists():
        return "holds no built bundle (index.html missing)"
    if expected_hash is None:
        # Cannot verify (git could not resolve the merged frontend tree); the
        # index-only acceptance is all there is.
        return None
    stamp = _read_bundle_stamp(source)
    if stamp is None:
        return (
            f"carries no {BUNDLE_STAMP_FILENAME} stamp, so it cannot be verified "
            "against the merged source"
        )
    if stamp != expected_hash:
        return (
            f"was built from frontend source tree {stamp}, but the merged tree's "
            f"frontend is {expected_hash} -- it is stale"
        )
    return None


def _assert_bundle_built(
    repo_root: Path, expected_hash: str | None, *, live_service_restarted: bool
) -> None:
    """Raise unless the build actually left a servable bundle of the merged
    source behind.

    A build tool that empties its output directory and then exits 0 without
    writing passes an exit-code check while leaving nothing to serve; the index
    check catches that. The stamp comparison is the consistency check on top:
    whatever is installed (a copied worker bundle, or a live build whose
    postbuild stamped it) must have been built from the merged tree's frontend
    source. It is skipped when ``expected_hash`` is ``None`` (recovery rebuilds
    on a rolled-back tree, where the pre-stamp build is normal) and degrades to
    a warning when the bundle simply carries no stamp (a build without a git
    repo writes none).
    """
    index = repo_root / FRONTEND_BUILD_INDEX
    if not index.exists():
        raise ApplyFailed(
            f"the frontend build reported success but wrote no bundle ({index} is missing)",
            live_service_restarted=live_service_restarted,
        )
    if expected_hash is None:
        return
    stamp = _read_bundle_stamp(repo_root / STATIC_DIR)
    if stamp is None:
        sys.stderr.write(
            f"note: the installed bundle carries no {BUNDLE_STAMP_FILENAME} stamp, "
            "so it could not be verified against the merged source.\n"
        )
        return
    if stamp != expected_hash:
        raise ApplyFailed(
            f"the installed bundle does not correspond to the merged source (built "
            f"from frontend tree {stamp}, merged tree is {expected_hash})",
            live_service_restarted=live_service_restarted,
        )


def _install_or_build_bundle(
    worker_bundle: str | None,
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Put the merged frontend's bundle in place.

    ``worker_bundle`` is the worker's already-built ``static/`` once the caller
    has verified it against the merged source (:func:`_worker_bundle_reject_reason`)
    -- the artifact the worker validated, and installing it is a plain copy that
    needs neither npm nor a registry. ``None`` means build live, tagged
    expendable: a shed build is an ordinary failure the rollback absorbs.
    """
    if worker_bundle is not None:
        source = Path(worker_bundle)
        destination = repo_root / STATIC_DIR
        try:
            if destination.exists():
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
        except OSError as exc:
            raise ApplyFailed(
                f"installing the worker's built bundle from {source} failed "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        return
    run_checked(
        runner,
        expend(["npm", "run", "build"]),
        repo_root / FRONTEND_DIR,
        "npm run build",
        timeout=timeout,
    )


class RecoveryOutcome(NamedTuple):
    """What a rollback's recovery confirmed.

    ``is_recovered`` is the exit-code question: the backend is healthy, and so
    is the frontend when one was expected. ``is_frontend_confirmed`` is the
    stricter fact the emergency record and the closing line turn on -- the
    live UI was probed and serves a working frontend -- which can hold even
    when none was expected (the rollback put a UI back that was already down
    when the apply began).
    """

    is_recovered: bool
    is_frontend_confirmed: bool


_NOT_RECOVERED = RecoveryOutcome(is_recovered=False, is_frontend_confirmed=False)


def _recover_running_state(
    plan: ApplyPlan,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    sleeper: Callable[[float], None],
    *,
    live_service_restarted: bool,
    snapshots: Sequence[SnapshotRecord],
    is_frontend_expected: bool,
    provisioner_ran: bool,
) -> RecoveryOutcome:
    """After the tree is restored to known-good, restore the pre-apply state and
    confirm the workspace is healthy.

    The frontend is always probed once the backend answers, but only held
    against the rollback when ``is_frontend_expected``: a UI that was already
    down when the apply began does not make the rollback a failure, while one
    that works afterwards is still worth knowing about (it is what lets the
    emergency record come down).

    Restores are file copies (no network, no package manager, no ``mngr``);
    rebuild/refresh fallbacks run only where there is no copy to put back. The
    provisioner re-run is best-effort by design: a failure (often no network),
    a hang past its budget, or a spawn failure still counts as recovered, with
    the tools named as left ahead of the tree. Nothing here is tagged
    expendable -- there is no further rollback to absorb a shed. Never raises:
    this is the last line of defense, and the exit code is all the caller has
    to go on.
    """
    try:
        failed = set(restore_snapshots(snapshots))
        restored = {record.name for record in snapshots} - failed
        if provisioner_ran:
            provisioner_failure = run_provisioner(runner, repo_root, is_forced=True)
            if provisioner_failure is not None:
                sys.stderr.write(
                    "recovery: re-running the provisioner from the restored tree failed "
                    f"({provisioner_failure}), so the globally pinned tools may be left "
                    "ahead of the tree. The rollback still counts as recovered -- re-run "
                    f"`bash {PROVISIONER_SCRIPT}` once the cause (often no network) is fixed.\n"
                )
        if plan.frontend and "bundle" not in restored:
            # No copy to put back: compile from source. node_modules likewise
            # has to match the restored lockfile when its own copy is gone.
            if plan.frontend_manifest and "node_modules" not in restored:
                run_checked(runner, ["npm", "ci"], repo_root / FRONTEND_DIR, "npm ci")
            run_checked(
                runner,
                ["npm", "run", "build"],
                repo_root / FRONTEND_DIR,
                "npm run build",
            )
            # No stamp comparison here: the tree is rolled back, and an older
            # tree's build may predate the stamping postbuild step.
            _assert_bundle_built(repo_root, None, live_service_restarted=False)
        if plan.backend_manifest and not BACKEND_SNAPSHOT_NAMES <= restored:
            refresh_backend_dependencies(repo_root, runner, keep_protected)
        # An app tool with no copy to put back (a non-critical app, or a copy
        # that could not be taken) is re-resolved from the restored tree. An
        # app the merge added has no directory there to resolve from, so its
        # environment is left as the failed apply built it.
        rebuildable_app_tools: list[AppTool] = []
        for app in plan.app_tools:
            if tool_snapshot_name(app.tool_name) in restored:
                continue
            if (repo_root / app.directory / "pyproject.toml").is_file():
                rebuildable_app_tools.append(app)
            else:
                sys.stderr.write(
                    f"recovery: the app at {app.directory} is not in the restored tree, so "
                    f"its tool environment ('{app.tool_name}') is left as the failed apply "
                    f"built it; `uv tool uninstall {app.tool_name}` removes it.\n"
                )
        if rebuildable_app_tools:
            refresh_app_tools(rebuildable_app_tools, repo_root, runner, keep_protected)
        if live_service_restarted:
            run_checked(
                runner,
                ["mngr", "start", "--restart", "system-services"],
                repo_root,
                "mngr start --restart",
            )
        healthy = wait_healthy(
            http,
            f"{base_url}{HEALTH_PATH}",
            HEALTH_ATTEMPTS,
            HEALTH_INTERVAL_SECONDS,
            sleeper,
        )
    except (ApplyFailed, OSError) as exc:
        sys.stderr.write(f"recovery step failed: {exc}\n")
        return _NOT_RECOVERED
    if not healthy:
        return _NOT_RECOVERED
    frontend_failure = describe_frontend_failure(http, base_url, sleeper)
    if frontend_failure is not None:
        if is_frontend_expected:
            sys.stderr.write(f"recovery left the frontend broken: {frontend_failure}\n")
            return _NOT_RECOVERED
        sys.stderr.write(
            "recovery: the live UI is not serving a working frontend, and was not "
            "when the apply began either, so the rollback is not held to that "
            f"standard: {frontend_failure}\n"
        )
    refresh_workspace_view(repo_root, runner)
    return RecoveryOutcome(
        is_recovered=True, is_frontend_confirmed=frontend_failure is None
    )


def _phase_timing_line(marker: ApplyMarker) -> str:
    """One stderr line of per-phase durations, from the marker's timings.

    The benchmarking input for tuning the poll and step budgets -- and, read
    from an interrupted apply's marker, what names the phase it hung in.
    """
    if not marker.phase_timings:
        return ""
    previous = marker.started_at
    parts: list[str] = []
    for phase, at in sorted(marker.phase_timings.items(), key=lambda item: item[1]):
        parts.append(f"{phase} +{at - previous:.1f}s")
        previous = at
    return "apply phase timings: " + ", ".join(parts) + "\n"


def _report_rolled_back(is_frontend_confirmed: bool) -> None:
    if is_frontend_confirmed:
        sys.stderr.write(
            "rolled back to last-known-good; the live workspace is confirmed healthy. "
            "The requested update did NOT land -- the worker branch and its report are "
            "kept, so once the failure is diagnosed a retry is a quick re-land.\n"
        )
    else:
        sys.stderr.write(
            "rolled back to last-known-good and the backend is healthy, but the live UI "
            "was not serving a working frontend before this apply either, so the "
            "rollback was not held to that standard and cannot confirm it. The requested "
            "update did NOT land -- diagnose both before retrying (the worker branch and "
            "its report are kept).\n"
        )


def _report_emergency(
    plan: ApplyPlan,
    repo_root: Path,
    reason: str,
    dri_agent: str,
    now: Callable[[], float],
) -> None:
    sys.stderr.write(
        "EMERGENCY: rollback did not restore a healthy workspace. The system interface "
        "may be down; manual intervention is required.\n"
    )
    # Durable, because stderr reaches whoever ran the apply and this state
    # outlives them: the banner reads this file, and so does the next agent.
    write_emergency(repo_root, reason, dri_agent, now)
    # The pre-apply copies outlive this failure on purpose: putting one back is
    # a plain file copy that needs neither npm nor a registry, so it is the way
    # out of exactly the failure that gets here. Only pointed at when the apply
    # touched the frontend -- after a backend-only apply the bundle copy is
    # byte-identical to what is already being served.
    bundle_copy = snapshots_root(repo_root) / "bundle"
    if plan.frontend and bundle_copy.exists():
        sys.stderr.write(
            f"the pre-apply frontend bundle was kept at {bundle_copy} -- copying it over "
            f"{repo_root / STATIC_DIR} restores the UI without needing npm or a registry. "
            "Delete it once you have.\n"
        )


def apply_update(
    merge_ref: str,
    repo_root: Path,
    *,
    ff_only: bool,
    worker_bundle: str | None,
    target_ref: str | None,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
    base_url: str | None = None,
    now: Callable[[], float] = time.time,
    today: str | None = None,
    is_pid_live: Callable[[int], bool] = default_is_pid_a_live_apply,
    expend: ExpendWrapper = as_expendable,
) -> int:
    """Land ``merge_ref`` and make the live workspace consistent with it, as one
    atomic, idempotent, rollback-on-failure motion. Returns the process exit
    code: 0 applied / 2 rolled back / 3 emergency / 1 precondition.

    Idempotent throughout: every phase checks current state before acting
    (merge already landed -> skip; snapshot already taken -> reuse; ledger
    entry present -> skip), so re-running ``apply`` after any interruption is
    safe -- that re-run *is* the DRI agent's recovery path.
    """
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")

    # One in-flight apply at a time, keyed on the marker. A live marker with a
    # dead process is this apply's own interrupted predecessor: adopt it (same
    # merge), or send the caller to ``recover`` (a different merge).
    marker = read_marker(repo_root)
    if marker is not None:
        if marker.pid != os.getpid() and is_pid_live(marker.pid):
            sys.stderr.write(
                f"error: another apply is already running (pid {marker.pid}, started by "
                f"'{marker.dri_agent}'); refusing to interleave with it.\n"
            )
            return 1
        if marker.merge_ref != merge_ref:
            sys.stderr.write(
                f"error: an interrupted apply of a different merge ({marker.merge_ref}) "
                "left the workspace mid-motion; run "
                "`python3 .agents/skills/update-self/scripts/update_self.py recover` "
                "to roll it back before applying anything else.\n"
            )
            return 1
        sys.stderr.write(
            f"resuming the interrupted apply of {merge_ref} (last completed phase: "
            f"{marker.phase}).\n"
        )
        marker.pid = os.getpid()
        # The re-run's own flags win over the recorded ones -- the DRI agent
        # re-invokes with the same command, and a deliberate change (say a
        # corrected --worker-bundle path) must not be silently ignored.
        marker.ff_only = ff_only
        marker.target_ref = target_ref
        marker.worker_bundle = worker_bundle
        # A kill inside ``git merge`` leaves the merge staged but uncommitted.
        # That half-motion is this apply's own, so undo it and re-merge from a
        # clean tree rather than refusing on the dirt it left. Only here: on a
        # fresh apply an in-progress merge belongs to someone else, and the
        # clean-tree refusal below is the right answer.
        abort_in_progress_merge(repo_root, runner)

    assert_clean_tree(repo_root, runner)

    if marker is None:
        marker = ApplyMarker(
            dri_agent=os.environ.get(ENV_DRI_AGENT, ""),
            rollback_to=git_out(runner, repo_root, ["rev-parse", "HEAD"]),
            merge_ref=merge_ref,
            target_ref=target_ref,
            ff_only=ff_only,
            worker_bundle=worker_bundle,
            phase=PHASE_STARTED,
            pid=os.getpid(),
            started_at=now(),
            updated_at=now(),
        )
    # Resolve the merge ref (read-only) BEFORE the marker is written: an
    # unresolvable ref raises the precondition error, and raising after the
    # write would leave a marker behind for an apply that never started --
    # showing the "update interrupted" banner and blocking other applies until
    # a needless `recover`. (A *resumed* apply's pre-existing marker survives
    # the raise, which is right: `recover` must still be able to roll it back.)
    is_merge_landed = _is_merge_landed(merge_ref, repo_root, runner)
    # A rolled-back merge is still an *ancestor* of HEAD -- the rollback is a
    # forward revert -- so without this an apply of it would skip the merge,
    # find an empty diff, and report "nothing live needed to change" plus a
    # version-history line for an update the tree does not contain. Re-running
    # the apply genuinely cannot re-land reverted content, so say so and stop.
    if is_merge_landed and _has_rollback_since(merge_ref, repo_root, runner):
        raise ApplyPreconditionError(
            f"{merge_ref} was landed and then rolled back, so its content is no "
            "longer in the tree even though the commit is still in history. "
            "Re-running the apply cannot re-land it: re-dispatch a fresh worker "
            "pass off the current HEAD instead. Nothing was changed."
        )
    write_marker(marker, repo_root, now)

    def _advance(phase: str) -> None:
        marker.phase = phase
        marker.phase_timings[phase] = now()
        write_marker(marker, repo_root, now)

    # --- Land the merge (skipped when already landed: idempotent re-entry). ---
    if not is_merge_landed:
        merge_argv = (
            ["git", "merge", "--ff-only", merge_ref]
            if ff_only
            else ["git", "merge", "--no-ff", "--no-edit", merge_ref]
        )
        result = runner.run(
            merge_argv, cwd=str(repo_root), capture_output=True, text=True, check=False
        )
        if getattr(result, "returncode", 0) != 0:
            # Nothing has landed: abort any half-merge, drop the marker, and
            # report as a precondition failure (exit 1, workspace untouched).
            runner.run(
                ["git", "merge", "--abort"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            clear_marker(repo_root)
            stderr = (getattr(result, "stderr", "") or "").strip()
            sys.stderr.write(
                f"error: merging {merge_ref} failed (exit {result.returncode}): {stderr}\n"
                "Nothing was changed. "
                + (
                    "A refused fast-forward means HEAD moved under the pass -- "
                    "re-dispatch off the current HEAD rather than hand-resolving.\n"
                    if ff_only
                    else "Resolve the conflict via a fresh worker pass rather than by hand.\n"
                )
            )
            return 1
    _advance(PHASE_MERGED)

    name_status = diff_name_status(repo_root, marker.rollback_to, runner)
    plan = plan_apply(
        [path for _, path in name_status],
        read_provisioner_inputs(repo_root),
        read_app_tools(repo_root),
    )

    unresolved_frontend_failure: str | None = None
    provisioner_failure: str | None = None
    # The regression baseline: whether a working frontend is owed afterwards
    # is decided by what was being served *before* the apply -- measured
    # once and persisted, so a resumed apply is not judged against the
    # wreckage its own interrupted run left: the rollback's recovery and its
    # report are held to this baseline, so leaving it unmeasured would
    # falsely report a healthy UI as already-broken after a rollback.
    if marker.frontend_expected is None:
        marker.frontend_expected = (
            describe_frontend_failure(http, resolved_base, sleeper) is None
        )
        write_marker(marker, repo_root, now)
    is_frontend_expected = bool(marker.frontend_expected)

    # Decide up front whether the worker's already-built bundle will be
    # installed: when it will, the npm dependency refresh below is dead
    # work on the critical path (installing the bundle is a plain copy that
    # needs no node_modules), and `npm ci` is the slowest, most
    # memory-hungry step of the whole motion. The stamp comparison is what
    # makes this decision trustworthy -- an unverifiable or stale bundle is
    # rejected here, so the live-build fallback (and its npm refresh) still
    # runs for it.
    expected_bundle_hash: str | None = None
    usable_worker_bundle: str | None = None
    if plan.frontend:
        expected_bundle_hash = _expected_frontend_tree_hash(repo_root, runner)
        if expected_bundle_hash is None:
            sys.stderr.write(
                f"note: could not resolve the merged tree's {FRONTEND_DIR} hash, so "
                "the bundle cannot be verified against the merged source; accepting "
                "it on its index alone.\n"
            )
        bundle_reject = _worker_bundle_reject_reason(
            marker.worker_bundle, expected_bundle_hash
        )
        if marker.worker_bundle is not None:
            if bundle_reject is None:
                usable_worker_bundle = marker.worker_bundle
            else:
                sys.stderr.write(
                    f"note: --worker-bundle {marker.worker_bundle} "
                    f"{bundle_reject}; building live instead.\n"
                )

    failure: ApplyFailed | None = None
    try:
        marker.snapshots = take_snapshots(plan, repo_root, runner, marker.snapshots)
        _advance(PHASE_SNAPSHOTTED)

        if plan.frontend_manifest and usable_worker_bundle is None:
            run_checked(
                runner,
                expend(["npm", "ci"]),
                repo_root / FRONTEND_DIR,
                "npm ci",
                timeout=_NPM_CI_TIMEOUT_SECONDS,
            )
        if plan.backend_manifest:
            refresh_backend_dependencies(
                repo_root, runner, expend, ENVIRONMENT_REFRESH_TIMEOUT_SECONDS
            )
        if plan.app_tools:
            refresh_app_tools(
                plan.app_tools, repo_root, runner, expend, ENVIRONMENT_REFRESH_TIMEOUT_SECONDS
            )
        _advance(PHASE_REFRESHED)

        # The provisioner runs before any restart, so nothing boots into a
        # tree whose pinned global toolchain has not caught up with it. Its
        # failure alone does not roll the merge back: a failed tool install
        # leaves the tree and services consistent, and re-running the
        # provisioner later is cheap and merge-independent -- whereas the
        # rollback costs the whole release plus a fresh worker pass. So the
        # apply carries on to the restart and the probes; a load-bearing
        # provisioner change (a node bump, a new apt dependency) still
        # fails those and still rolls back, and a landed update records
        # the gap (``write_provision_incomplete``) rather than hiding it.
        if plan.provisioner:
            # Recorded before the run is attempted, like the restart flag:
            # a provisioner that fails part-way (or is killed) may already
            # have moved global tool state, so recovery must re-run it from
            # the restored tree (best-effort) even then.
            marker.provisioner_ran = True
            write_marker(marker, repo_root, now)
            provisioner_failure = run_provisioner(runner, repo_root)
            if provisioner_failure is not None:
                sys.stderr.write(
                    f"warning: {provisioner_failure}\nContinuing without rolling "
                    "back: the tree and services stay consistent without the "
                    "provisioner, so the update lands if the probes pass and is "
                    "recorded as provisioning-incomplete; if they fail it rolls "
                    "back as usual.\n"
                )
            _advance(PHASE_PROVISIONED)

        # The pre-flight runs before the bundle is touched: it needs only
        # the merged tree and its refreshed environment, and a merged
        # backend that cannot boot is then rejected while the live bundle
        # is still the one that was serving, instead of after ``static/``
        # has been rewritten and must be restored from its snapshot.
        preflight_output = preflight(repo_root, http, spawner, sleeper, expend)
        if preflight_output is not None:
            raise ApplyFailed(
                "merged backend failed to boot in a pre-flight check; live "
                "service not restarted",
                detail=preflight_output or "(the pre-flight boot wrote nothing at all)",
                detail_heading="pre-flight boot output",
            )

        if plan.frontend:
            _install_or_build_bundle(
                usable_worker_bundle,
                repo_root,
                runner,
                expend,
                _FRONTEND_BUILD_TIMEOUT_SECONDS,
            )
            _assert_bundle_built(
                repo_root, expected_bundle_hash, live_service_restarted=False
            )
            _advance(PHASE_BUILT)

        # Every apply restarts the services agent, whatever the diff: the
        # running system interface imports the vendored mngr and the
        # workspace libraries in-process and re-reads ``.mngr/settings.toml``
        # per request, and every other supervisord program runs whatever
        # code was on disk when it started -- so a restart is the only way
        # to make "the merged tree is live" true for all of them at once,
        # and deciding it per path is a list nobody keeps complete.
        # Recorded before the restart is attempted, so a kill anywhere past
        # this line leaves a marker that tells recovery to restart.
        marker.live_service_restarted = True
        write_marker(marker, repo_root, now)
        run_checked(
            runner,
            ["mngr", "start", "--restart", "system-services"],
            repo_root,
            "mngr start --restart",
            live_service_restarted=True,
            timeout=_RESTART_TIMEOUT_SECONDS,
        )
        _advance(PHASE_RESTARTED)
        if not wait_healthy(
            http,
            f"{resolved_base}{HEALTH_PATH}",
            HEALTH_ATTEMPTS,
            HEALTH_INTERVAL_SECONDS,
            sleeper,
        ):
            raise ApplyFailed(
                "backend did not become healthy after restart",
                live_service_restarted=True,
            )

        # Scoped to a *regression*: only a frontend that was serving before
        # this apply has to be serving after it. Ahead of the view refresh,
        # so an apply that regressed the frontend rolls back rather than
        # asking every open view to reload into it.
        unresolved_frontend_failure = describe_frontend_failure(
            http, resolved_base, sleeper
        )
        if unresolved_frontend_failure is not None:
            if is_frontend_expected:
                raise ApplyFailed(
                    "the live UI stopped serving a working frontend: "
                    f"{unresolved_frontend_failure}",
                    live_service_restarted=True,
                )
            sys.stderr.write(
                "warning: the live UI is not serving a working frontend, and was "
                "not before this apply either, so it was not rolled back for it: "
                f"{unresolved_frontend_failure}\n"
            )
        # Past the last rollback point: nothing after the probes can raise
        # ApplyFailed, so the interruption marker and the snapshots come
        # down NOW -- before the view refresh (a shell reloading into a
        # lingering marker would render the "update was interrupted"
        # banner over an apply that just succeeded) and before the
        # post-success bookkeeping (so an unattended ``recover`` can never
        # roll back an update that already went live; the ledger append
        # and env-converge are both safely re-runnable without a marker).
        sys.stderr.write(_phase_timing_line(marker))
        if provisioner_failure is not None:
            write_provision_incomplete(
                repo_root, provisioner_failure, marker.dri_agent, merge_ref, now
            )
        elif plan.provisioner:
            clear_provision_incomplete(repo_root)
        clear_marker(repo_root)
        discard_snapshots(repo_root)
        # The emergency record only comes down on confirmed health, which
        # is more than this exit code carries: an apply over a UI that was
        # already broken lands, exits 0 naming the breakage, and leaves a
        # user who still cannot see the workspace -- exactly the state the
        # record exists to keep visible. Confirmed means probed and
        # working, whatever the baseline was: an apply that lands over a
        # broken UI and finds it working afterwards is the repair the
        # record was waiting for.
        if unresolved_frontend_failure is None:
            clear_emergency(repo_root)
        refresh_workspace_view(repo_root, runner)
    except ApplyFailed as failed:
        failure = failed
    except Exception as unexpected:
        # The last resort, and the one place a blind catch is the correct
        # answer: past this point the merge is landed and the pre-apply
        # copies are the only ones left, so an exception nobody predicted
        # must still reach the rollback below. Letting it escape is what
        # strands the workspace half-applied -- which is how a bands module
        # older than this script (an AttributeError inside `as_expendable`)
        # and a missing executable have each already got out. The traceback
        # goes in `detail` so the DRI agent can still see the bug the
        # rollback just tidied away.
        failure = ApplyFailed(
            f"the apply raised an unexpected {type(unexpected).__name__}: {unexpected}",
            live_service_restarted=marker.live_service_restarted,
            detail=traceback.format_exc(),
            detail_heading="traceback",
        )
    if failure is not None:
        sys.stderr.write(
            f"apply failed: {failure}\n{detail_block(failure)}"
            f"{_phase_timing_line(marker)}"
            f"rolling back to {marker.rollback_to[:12]} and restoring the "
            "workspace...\n"
        )
        try:
            _restore_tree(name_status, marker.rollback_to, repo_root, runner)
            _commit_rollback(
                repo_root,
                runner,
                marker.rollback_to,
                f"Apply failed and was auto-reverted: {failure.headline()}",
            )
            outcome = _recover_running_state(
                plan,
                repo_root,
                resolved_base,
                runner,
                http,
                sleeper,
                live_service_restarted=failure.live_service_restarted
                or marker.live_service_restarted,
                snapshots=marker.snapshots,
                is_frontend_expected=is_frontend_expected,
                provisioner_ran=marker.provisioner_ran,
            )
        except (subprocess.CalledProcessError, OSError) as rollback_exc:
            sys.stderr.write(f"the rollback itself failed: {rollback_exc}\n")
            outcome = _NOT_RECOVERED
        if outcome.is_recovered:
            clear_marker(repo_root)
            discard_snapshots(repo_root)
            # Same rule as the success path: only a probed, working
            # frontend takes the record down.
            if outcome.is_frontend_confirmed:
                clear_emergency(repo_root)
            _report_rolled_back(outcome.is_frontend_confirmed)
            return 2
        # The marker is cleared even on the emergency path: this is a
        # deliberate, fully-reported exit, and re-running the same failed
        # rollback from cron would not help. The snapshots are kept -- they
        # are the operator's way back, and so is the emergency record the
        # report writes in the marker's place.
        clear_marker(repo_root)
        _report_emergency(
            plan,
            repo_root,
            f"apply of {merge_ref} failed and its rollback could not restore "
            f"health: {failure.headline()}",
            marker.dri_agent,
            now,
        )
        return 3

    # --- Post-success bookkeeping (update-self mode only). -----------------------
    if target_ref is not None:
        # For the fast-forward landing the merge commit IS the worker branch's
        # tip, so the sha is re-derivable on any re-run -- which is what keeps
        # the ledger append a no-op after an interruption.
        try:
            merge_sha = git_out(runner, repo_root, ["rev-parse", merge_ref])
            write_version_history_entry(
                repo_root,
                runner,
                target_ref,
                merge_sha,
                today or datetime.date.today().isoformat(),
            )
        except (LedgerCommitError, subprocess.CalledProcessError, OSError) as exc:
            sys.stderr.write(
                f"warning: the update landed but the version-history entry could not "
                f"be recorded ({exc}); record it manually per the update-self skill.\n"
            )
        # The one moment package versions are allowed to move. Post-success
        # only, so a failed apply never moved apt state; a failure here is
        # reported but does not un-apply the update.
        try:
            converge = runner.run(
                ["uv", "run", "env-converge", "upgrade"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=_ENV_CONVERGE_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            converge = subprocess.CompletedProcess(
                ["uv", "run", "env-converge", "upgrade"],
                returncode=124,
                stdout="",
                stderr=(
                    f"did not finish within {_ENV_CONVERGE_TIMEOUT_SECONDS:g}s"
                    if isinstance(exc, subprocess.TimeoutExpired)
                    else f"could not be run ({exc})"
                ),
            )
        if getattr(converge, "returncode", 0) != 0:
            stderr = (getattr(converge, "stderr", "") or "").strip()
            sys.stderr.write(
                f"warning: `uv run env-converge upgrade` failed (exit "
                f"{converge.returncode}): {stderr}\nThe update is applied; re-run it "
                "once the cause is fixed so the pinned apt snapshot advances.\n"
            )
        elif getattr(converge, "stdout", ""):
            sys.stdout.write(converge.stdout)

    if provisioner_failure is not None:
        sys.stderr.write(
            f"applied with incomplete provisioning: {provisioner_failure}\nThe update "
            "is landed and the live workspace is healthy, but the pinned global "
            f"toolchain did not catch up with the tree. Re-run `bash {PROVISIONER_SCRIPT}` "
            "once the cause is fixed; the gap is recorded at "
            f"{provision_incomplete_path(repo_root)} until a provisioner run succeeds.\n"
        )
    if unresolved_frontend_failure is not None:
        sys.stderr.write(
            "applied: the update landed and the backend is healthy, but the live UI is "
            "still not serving a working frontend: "
            f"{unresolved_frontend_failure}. That was already true before this apply, "
            "so it was not rolled back for it -- report it and diagnose it separately.\n"
        )
        return 0
    sys.stderr.write(
        "applied: the update is landed and the live workspace is confirmed healthy.\n"
    )
    return 0


def recover(
    repo_root: Path,
    *,
    if_stale: bool,
    grace_seconds: float,
    no_restart: bool,
    runner: Runner,
    http: HttpClient,
    sleeper: Callable[[float], None] = time.sleep,
    base_url: str | None = None,
    now: Callable[[], float] = time.time,
    is_pid_live: Callable[[int], bool] = default_is_pid_a_live_apply,
) -> int:
    """Roll back an interrupted apply from its marker.

    ``--if-stale`` is the unattended guard (boot and cron): act only when a
    marker exists, its recorded process is dead, and it has gone ``grace``
    without an update -- and stay silent in every normal state, because the
    cron runs forever. Bare ``recover`` is the explicit agent-driven rollback.

    ``--no-restart`` is the boot path: nothing is running yet, so disk state is
    the whole job (bootstrap starts the services fresh from the restored tree)
    and the health probes would only time out against a server that has not
    booted. The marker survives a failed *tree restore* (exit 1) so the next
    pass retries it; a rollback that restored the tree but could not put the
    pre-apply state back (boot path) or confirm a healthy workspace (live
    path) clears the marker and records the emergency (exit 3), like the
    apply's own emergency path -- re-running the same failed rollback from
    cron would not help, and the record is what makes the state visible once
    the marker is gone.
    """
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    marker = read_marker(repo_root)
    if marker is None:
        if not if_stale:
            sys.stderr.write("no interrupted apply to recover (no marker found).\n")
        return 0
    if is_pid_live(marker.pid):
        if if_stale:
            return 0
        sys.stderr.write(
            f"error: the apply (pid {marker.pid}) is still running; refusing to roll "
            "back underneath it.\n"
        )
        return 1
    if if_stale and (now() - marker.updated_at) < grace_seconds:
        # Freshly dead: give the DRI agent its window to simply re-run the
        # idempotent apply before the unattended path rolls it back.
        return 0

    sys.stderr.write(
        f"recovering an interrupted apply of {marker.merge_ref} (last completed "
        f"phase: {marker.phase}, DRI agent: '{marker.dri_agent}'); rolling back to "
        f"{marker.rollback_to[:12]}...\n"
    )
    # This process now owns the marker: a DRI agent re-running the apply
    # meanwhile would otherwise see the dead apply's pid, adopt the marker, and
    # merge concurrently with this rollback. With a live pid recorded it
    # refuses instead, like it does for a running apply.
    marker.pid = os.getpid()
    write_marker(marker, repo_root, now)
    name_status = diff_name_status(repo_root, marker.rollback_to, runner)
    plan = plan_apply(
        [path for _, path in name_status],
        read_provisioner_inputs(repo_root),
        read_app_tools(repo_root),
    )
    try:
        # Before anything commits: an apply killed inside its merge left the
        # merge staged, and committing on top of that would land it instead of
        # rolling it back.
        abort_in_progress_merge(repo_root, runner)
        _restore_tree(name_status, marker.rollback_to, repo_root, runner)
        _commit_rollback(
            repo_root,
            runner,
            marker.rollback_to,
            f"Interrupted apply of {marker.merge_ref} (last completed phase: "
            f"{marker.phase}) rolled back by recover",
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        # The marker is kept: the tree is still mid-motion and a later recover
        # (or the DRI agent) must be able to try again.
        sys.stderr.write(f"recover: restoring the tree failed: {exc}\n")
        return 1

    if no_restart:
        failed = restore_snapshots(marker.snapshots)
        if marker.provisioner_ran:
            provisioner_failure = run_provisioner(runner, repo_root, is_forced=True)
            if provisioner_failure is not None:
                sys.stderr.write(
                    "recover: re-running the provisioner from the restored tree failed "
                    f"({provisioner_failure}); the globally pinned tools may be left "
                    "ahead of the tree.\n"
                )
        clear_marker(repo_root)
        if failed:
            # The copies stay, for the same reason the emergency path keeps
            # them: a restore that failed for anything other than a missing
            # copy (a full disk, a permission fault) leaves the copy sitting
            # right there, and putting it back by hand is the way out. Deleting
            # them here would destroy the only remaining route.
            reason = (
                f"could not restore: {', '.join(sorted(failed))}. The tree is "
                "rolled back but the pre-apply state is NOT -- the copies are kept at "
                f"{snapshots_root(repo_root)}, so copying one back by hand is the "
                "quickest repair; whatever has no copy left has to be rebuilt. "
                "Services will boot against that mismatch."
            )
            sys.stderr.write(f"recover: {reason}\n")
            # Nothing is running to probe, so this is the boot path's emergency:
            # the marker is gone, the services are about to boot over a
            # non-restored venv or bundle, and without the record nothing
            # would show it.
            write_emergency(
                repo_root,
                f"an interrupted apply of {marker.merge_ref} was rolled back at boot, "
                f"but {reason}",
                marker.dri_agent,
                now,
            )
            return 3
        discard_snapshots(repo_root)
        sys.stderr.write(
            "recovered: the tree and pre-apply state are rolled back; services will "
            "boot fresh from the restored state.\n"
        )
        return 0

    outcome = _recover_running_state(
        plan,
        repo_root,
        resolved_base,
        runner,
        http,
        sleeper,
        live_service_restarted=marker.live_service_restarted,
        snapshots=marker.snapshots,
        is_frontend_expected=bool(marker.frontend_expected),
        provisioner_ran=marker.provisioner_ran,
    )
    if outcome.is_recovered:
        clear_marker(repo_root)
        discard_snapshots(repo_root)
        # Same rule again, and it decides both what this clears and what it
        # may claim: only a probed, working frontend is confirmed health. A
        # rollback that could not be held to that standard -- no working UI
        # when the apply began, or an apply killed before it measured its
        # baseline (the marker predates the merge, the baseline probe follows
        # it) -- and finds the UI still down has confirmed the backend and
        # nothing else, and this line, often the only account of an
        # unattended recovery, must not sign off on more.
        if outcome.is_frontend_confirmed:
            clear_emergency(repo_root)
            confirmation = "the live workspace is confirmed healthy"
        else:
            unheld = (
                "the live UI was not serving a working frontend when that apply began "
                "either"
                if marker.frontend_expected is False
                else "that apply was killed before it recorded whether the live UI was "
                "serving a working frontend"
            )
            confirmation = (
                f"the backend is healthy, but {unheld}, so this rollback was not held "
                "to that standard and cannot confirm it"
            )
        sys.stderr.write(
            f"recovered: the interrupted apply is rolled back and {confirmation}. The "
            "worker branch and its report are kept, so a diagnosed retry is a quick "
            "re-land.\n"
        )
        return 0
    clear_marker(repo_root)
    _report_emergency(
        plan,
        repo_root,
        f"an interrupted apply of {marker.merge_ref} (last completed phase: "
        f"{marker.phase}) was rolled back, but the live workspace could not be "
        "confirmed healthy",
        marker.dri_agent,
        now,
    )
    return 3
