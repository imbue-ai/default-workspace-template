"""The machine-readable contract between an update apply and the things that read
it without importing this code: the Minds app (``run.json`` over ``mngr exec``),
the bootstrap package and the recovery cron (the apply marker), and the system
interface's staleness banner (the marker and the emergency record). Every path,
filename, phase, verdict and record shape lives here; a change to one is a
cross-repo contract change.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from update_layout import PROVISIONER_SCRIPT

# The agent driving this apply -- recorded in the marker so recovery knows who
# to re-engage after an interruption.
ENV_DRI_AGENT = "MNGR_AGENT_NAME"

# Machine state for an in-flight apply. The marker is what makes a hard kill
# detectable (boot-time recovery, the recovery cron, a concurrent apply's
# refusal); the snapshots directory holds the pre-apply copies the rollback
# restores from. Both under ``data/.state`` so they survive a container
# restart, which ``/tmp`` need not.
STATE_DIR_REL = "data/.state/update-apply"

MARKER_FILENAME = "marker.json"

SNAPSHOTS_DIRNAME = "snapshots"

# The run-status file: the whole machine-readable contract between an
# update-self pass and the Minds app. The lead records the run's start here
# (``run-status start``, once it holds the updating-workspace lease), the
# worker it hands the merge to (``run-status delegate``), its one mid-flight
# hold and its clearing (``run-status hold`` / ``resume``), and its
# one terminal verdict (``run-status verdict``); the apply mirrors its marker's
# phase and restamp in alongside. The app's poll reads this file over ``mngr
# exec`` together with the run's chat agent, and needs nothing else -- it never
# opens the marker, and a run recorded here is visible to it whoever launched
# the run. One file per workspace: a new run's ``start`` overwrites the
# previous run's record, which is exactly the app's model (the last run's
# outcome stands until a new run supersedes it). The lease is what keeps that
# single record honest, which is why the start waits for it -- and why a
# verdict is recorded against the agent recording it rather than against
# whatever name the file happens to carry.
RUN_STATUS_FILENAME = "run.json"

# The emergency record, written when a rollback could not put a healthy
# workspace back. The marker cannot carry this: it comes down on the emergency
# path (that exit is deliberate and fully reported, and re-running the same
# failed rollback from cron would not help), and the rollback has made the tree
# content match the pre-apply HEAD again -- so without a separate file the one
# state that most needs to speak is the one nothing can see.
EMERGENCY_FILENAME = "emergency.json"

# The provisioning-incomplete record, written when an update landed healthy
# but its provisioner run failed. A failed tool install leaves the tree and
# services consistent and re-running the provisioner is cheap and
# merge-independent, so it does not roll the whole release back -- but the
# gap must not be silent either: this record (the same durable shape as the
# emergency one) is what the skill reads to re-run the provisioner after the
# fix, and it comes down only when a provisioner run succeeds.
PROVISION_INCOMPLETE_FILENAME = "provision-incomplete.json"

# The apply's phases, recorded in the marker as each completes so an
# interrupted apply can be read (by recovery, and by the system interface's
# "an update was interrupted" banner) without guessing. The marker comes down
# at the apply's last rollback point -- once the live workspace is confirmed
# healthy on the merged tree -- so there is no phase past the restart: the
# post-success bookkeeping (ledger, env-converge) runs marker-free, because an
# interruption there must never read as an update worth rolling back.
#
# INVARIANT: the marker is on disk before anything that can disturb the live
# interface. The restart is the last phase, and every earlier phase works on
# the side (even the pre-flight boots the merged backend on its own port), so
# by the time the workspace's interface can stop answering, the marker has
# been present for the whole apply. The Minds app's misdiagnosis guard depends
# on exactly this ordering -- its stuck-edge probe reads the marker over
# ``mngr exec`` *after* an outage begins, and declines unattended recovery on
# finding it -- so a reordering that lets a service-disturbing step precede
# the marker write would silently break that guard.
PHASE_STARTED = "started"

PHASE_MERGED = "merged"

PHASE_SNAPSHOTTED = "snapshotted"

PHASE_REFRESHED = "environments_refreshed"

PHASE_PROVISIONED = "provisioned"

PHASE_BUILT = "frontend_built"

PHASE_RESTARTED = "restarted"


# ``recover --if-stale``'s default grace: how long a marker must have gone
# without an update (with its process dead) before the cron path rolls the
# apply back. Long enough that a DRI agent re-running the idempotent ``apply``
# right after a kill wins the race; short enough that a workspace does not sit
# half-applied for long when nobody is coming back.
DEFAULT_RECOVER_GRACE_SECONDS = 600.0


@dataclass
class SnapshotRecord:
    """One pre-apply copy: what was copied and where the copy lives.

    ``source`` is the original absolute path (the restore destination);
    ``copy`` the absolute path of the pre-apply copy. Restores are plain file
    copies back to ``source`` -- no network, no package manager.
    """

    name: str
    source: str
    copy: str


@dataclass
class ApplyMarker:
    """The full-information record of an in-flight apply.

    Written before the merge lands and cleared on every exit path, so its
    presence *is* the interruption signal: boot-time recovery, the recovery
    cron, and the system interface's "an update was interrupted" banner all key
    off it, and a concurrent ``apply`` refuses to start while a live one
    exists. It carries everything a dependency-free rollback needs -- the
    rollback point, the snapshot manifest, whether the provisioner ran and
    whether the live service was restarted -- plus the DRI agent to re-engage
    afterwards.
    """

    dri_agent: str
    rollback_to: str
    merge_ref: str
    target_ref: str | None
    ff_only: bool
    worker_bundle: str | None
    phase: str
    pid: int
    started_at: float
    updated_at: float
    provisioner_ran: bool = False
    live_service_restarted: bool = False
    # Whether a working frontend was being served when the apply began -- the
    # regression baseline the probes hold the apply to. Persisted so a resumed
    # apply keeps the original baseline rather than re-measuring a workspace
    # its own interrupted run may have broken. ``None`` = not yet measured.
    frontend_expected: bool | None = None
    snapshots: list[SnapshotRecord] = field(default_factory=list)
    # When each phase was reached (epoch seconds), so every apply yields
    # per-phase durations and an interrupted one names the phase it hung in.
    phase_timings: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "dri_agent": self.dri_agent,
            "rollback_to": self.rollback_to,
            "merge_ref": self.merge_ref,
            "target_ref": self.target_ref,
            "ff_only": self.ff_only,
            "worker_bundle": self.worker_bundle,
            "phase": self.phase,
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "provisioner_ran": self.provisioner_ran,
            "live_service_restarted": self.live_service_restarted,
            "frontend_expected": self.frontend_expected,
            "phase_timings": dict(self.phase_timings),
            "snapshots": [
                {"name": s.name, "source": s.source, "copy": s.copy}
                for s in self.snapshots
            ],
        }
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ApplyMarker":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
        return cls(
            dri_agent=str(raw.get("dri_agent", "")),
            rollback_to=str(raw["rollback_to"]),
            merge_ref=str(raw["merge_ref"]),
            target_ref=raw.get("target_ref"),
            ff_only=bool(raw.get("ff_only", False)),
            worker_bundle=raw.get("worker_bundle"),
            phase=str(raw.get("phase", PHASE_STARTED)),
            pid=int(raw.get("pid", 0)),
            started_at=float(raw.get("started_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            provisioner_ran=bool(raw.get("provisioner_ran", False)),
            live_service_restarted=bool(raw.get("live_service_restarted", False)),
            frontend_expected=raw.get("frontend_expected"),
            phase_timings={
                str(phase): float(at)
                for phase, at in (raw.get("phase_timings") or {}).items()
            },
            snapshots=[
                SnapshotRecord(
                    name=str(s["name"]), source=str(s["source"]), copy=str(s["copy"])
                )
                for s in raw.get("snapshots", [])
            ],
        )


def marker_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / MARKER_FILENAME


def snapshots_root(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / SNAPSHOTS_DIRNAME


def read_marker(repo_root: Path) -> ApplyMarker | None:
    """Read the in-flight apply marker, or ``None`` when there is none.

    An unreadable or unparseable marker file reads as ``None`` plus a warning:
    every caller of this is deciding whether recovery work exists, and a
    corrupt marker must not wedge that decision forever -- the clean-tree and
    ancestor checks still guard the actual mutations.
    """
    path = marker_path(repo_root)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        sys.stderr.write(f"warning: could not read {path} ({exc}); ignoring it.\n")
        return None
    try:
        return ApplyMarker.from_json(text)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(
            f"warning: {path} is not a valid marker ({exc}); ignoring it.\n"
        )
        return None


def write_marker(
    marker: ApplyMarker, repo_root: Path, now: Callable[[], float]
) -> None:
    """Persist ``marker`` atomically (write-then-rename), stamping ``updated_at``.

    Atomic so a reader (the recovery cron, the banner) never sees a torn file,
    and so a kill between write and rename leaves the previous state rather
    than none.
    """
    marker.updated_at = now()
    path = marker_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".json.tmp")
    scratch.write_text(marker.to_json())
    scratch.replace(path)
    _mirror_apply_into_run_status(marker.phase, marker.updated_at, repo_root, now)


def clear_marker(repo_root: Path) -> None:
    marker_path(repo_root).unlink(missing_ok=True)
    _mirror_apply_into_run_status(None, None, repo_root, time.time)


def _mirror_apply_into_run_status(
    phase: str | None,
    apply_updated_at: float | None,
    repo_root: Path,
    now: Callable[[], float],
) -> None:
    """Keep the run record's apply fields equal to the marker's presence and phase.

    The marker is the apply's own recovery record; the run record is what the
    Minds app reads. Stamping the two together at the marker's chokepoints is
    what lets the app size its apply window off this file alone -- the app
    stands back while ``apply_phase`` is set and for the recovery grace after
    its last restamp, exactly as it did off the marker. A workspace with no run
    record (an apply run by hand outside a pass) has nothing to report to.
    """
    status = read_run_status(repo_root)
    if status is None:
        return
    if status.apply_phase == phase and status.apply_updated_at == apply_updated_at:
        return
    status.apply_phase = phase
    status.apply_updated_at = apply_updated_at
    write_run_status(status, repo_root, now)


# The terminal verdicts a run may record, mirrored by the Minds app's
# ``UpdateVerdict`` enum. The app drops a verdict string it does not know, so
# adding one here is a contract change that needs the app taught first.
RUN_VERDICT_UPDATED = "UPDATED"

RUN_VERDICT_UPDATED_WITH_REBUILD_ITEMS = "UPDATED_WITH_REBUILD_ITEMS"

RUN_VERDICT_ALREADY_CURRENT = "ALREADY_CURRENT"

RUN_VERDICT_NEEDS_RECREATION = "NEEDS_RECREATION"

RUN_VERDICT_STUCK = "STUCK"

RUN_VERDICT_REFUSED = "REFUSED"

RUN_VERDICTS = (
    RUN_VERDICT_UPDATED,
    RUN_VERDICT_UPDATED_WITH_REBUILD_ITEMS,
    RUN_VERDICT_ALREADY_CURRENT,
    RUN_VERDICT_NEEDS_RECREATION,
    RUN_VERDICT_STUCK,
    RUN_VERDICT_REFUSED,
)


# Why a run is holding for the user mid-flight, mirrored by the app's
# ``UpdateHoldReason``. Like the verdicts, the app drops a reason it does not
# know, so adding one is a contract change that needs the app taught first.
RUN_HOLD_CUSTOMIZATION = "CUSTOMIZATION"

RUN_HOLD_CONFLICT = "CONFLICT"

RUN_HOLD_REASONS = (RUN_HOLD_CUSTOMIZATION, RUN_HOLD_CONFLICT)


@dataclass
class RunStatus:
    """One update-self run's record for the Minds app: who is running, and how it ended.

    Every timestamp is epoch seconds. ``verdict`` is ``None`` while the run is
    going; the fields after it are only meaningful once it is set.

    Three in-flight facts ride alongside the start and the verdict, because
    they are the things a run does that the user can see, must answer, or
    would otherwise misread:

    * ``worker_agent_name`` -- the background worker the lead has handed the
      merge to (``run-status delegate``). The lead's own chat sits idle while
      it waits on that worker, and idle is what the app reads as "waiting for
      the user"; naming the worker lets the app read its liveness instead.
      Cleared by the verdict (and by the next run's ``start``).
    * ``hold_reason``/``hold_detail`` -- the run has stopped to ask the user
      something (``run-status hold``), and why; cleared by ``run-status resume``.
    * ``apply_phase``/``apply_updated_at`` -- the apply is landing, and its last
      completed phase. Mirrored from the apply marker on its every restamp and
      cleared with it, so the app reads the apply's liveness from this one
      file and never has to know the marker exists.
    """

    chat_agent_name: str
    is_unattended: bool
    started_at: float
    updated_at: float
    worker_agent_name: str | None = None
    hold_reason: str | None = None
    hold_detail: str = ""
    apply_phase: str | None = None
    apply_updated_at: float | None = None
    verdict: str | None = None
    detail: str = ""
    resulting_ref: str = ""
    in_place_compatible_ref: str = ""
    verdict_at: float | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "chat_agent_name": self.chat_agent_name,
                "is_unattended": self.is_unattended,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "worker_agent_name": self.worker_agent_name,
                "hold_reason": self.hold_reason,
                "hold_detail": self.hold_detail,
                "apply_phase": self.apply_phase,
                "apply_updated_at": self.apply_updated_at,
                "verdict": self.verdict,
                "detail": self.detail,
                "resulting_ref": self.resulting_ref,
                "in_place_compatible_ref": self.in_place_compatible_ref,
                "verdict_at": self.verdict_at,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "RunStatus":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
        worker_agent_name = raw.get("worker_agent_name")
        hold_reason = raw.get("hold_reason")
        apply_phase = raw.get("apply_phase")
        apply_updated_at = raw.get("apply_updated_at")
        verdict = raw.get("verdict")
        verdict_at = raw.get("verdict_at")
        return cls(
            chat_agent_name=str(raw.get("chat_agent_name", "")),
            is_unattended=bool(raw.get("is_unattended", False)),
            started_at=float(raw.get("started_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            worker_agent_name=str(worker_agent_name)
            if worker_agent_name is not None
            else None,
            hold_reason=str(hold_reason) if hold_reason is not None else None,
            hold_detail=str(raw.get("hold_detail", "")),
            apply_phase=str(apply_phase) if apply_phase is not None else None,
            apply_updated_at=float(apply_updated_at)
            if apply_updated_at is not None
            else None,
            verdict=str(verdict) if verdict is not None else None,
            detail=str(raw.get("detail", "")),
            resulting_ref=str(raw.get("resulting_ref", "")),
            in_place_compatible_ref=str(raw.get("in_place_compatible_ref", "")),
            verdict_at=float(verdict_at) if verdict_at is not None else None,
        )


def run_status_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / RUN_STATUS_FILENAME


def read_run_status(repo_root: Path) -> RunStatus | None:
    """Read the run-status file, or ``None`` when absent or unreadable.

    Same lenience as :func:`read_marker`, for the same reason: this is status
    reporting, and a corrupt file must not wedge the pass that would overwrite
    it.
    """
    path = run_status_path(repo_root)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        sys.stderr.write(f"warning: could not read {path} ({exc}); ignoring it.\n")
        return None
    try:
        return RunStatus.from_json(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        sys.stderr.write(
            f"warning: {path} is not a valid run status ({exc}); ignoring it.\n"
        )
        return None


def write_run_status(
    status: RunStatus, repo_root: Path, now: Callable[[], float]
) -> None:
    """Persist ``status`` atomically (write-then-rename), stamping ``updated_at``."""
    status.updated_at = now()
    path = run_status_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".json.tmp")
    # Newline-terminated so a reader that `cat`s the file and then echoes a
    # sentinel (the Minds app's probe) sees the sentinel on its own line.
    scratch.write_text(status.to_json() + "\n")
    scratch.replace(path)


def emergency_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / EMERGENCY_FILENAME


def write_emergency(
    repo_root: Path, reason: str, dri_agent: str, now: Callable[[], float]
) -> None:
    """Record that a rollback left the workspace unhealthy, atomically.

    ``dri_agent`` comes from the marker being cleared, never from this
    process's environment: the paths that reach here unattended (the recovery
    cron, bootstrap) carry no ``MNGR_AGENT_NAME`` at all, and an agent-driven
    ``recover`` carries the *recovering* agent rather than the one whose apply
    failed. The marker is the only other place that name lives and it comes
    down on this same path.

    Best-effort: this runs on the way out of a failure that has already been
    written to stderr in full, so a filesystem that will not take the record
    must not turn a reported emergency into a traceback.
    """
    path = emergency_path(repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".json.tmp")
        scratch.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "recorded_at": now(),
                    "dri_agent": dri_agent,
                    "snapshots_dir": str(snapshots_root(repo_root)),
                },
                indent=2,
            )
        )
        scratch.replace(path)
    except OSError as exc:
        sys.stderr.write(
            f"warning: could not record the emergency at {path} ({exc}).\n"
        )


def clear_emergency(repo_root: Path) -> None:
    """Drop the emergency record; the live workspace is confirmed healthy again.

    Call only from an outcome that actually confirmed that -- the frontend
    included. A backend answering over a UI that is still down is not it: a
    broken UI is the usual aftermath of the failure that wrote the record, so
    clearing on the backend alone would take the banner away from the one
    workspace that still needs it.
    """
    emergency_path(repo_root).unlink(missing_ok=True)


def provision_incomplete_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / PROVISION_INCOMPLETE_FILENAME


def write_provision_incomplete(
    repo_root: Path,
    reason: str,
    dri_agent: str,
    merge_ref: str,
    now: Callable[[], float],
) -> None:
    """Record that an update landed with its provisioner run failed, atomically.

    Best-effort like the emergency record: the failure is already on stderr in
    full, and a filesystem that will not take the record must not turn a
    landed update into a traceback.
    """
    path = provision_incomplete_path(repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".json.tmp")
        scratch.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "recorded_at": now(),
                    "dri_agent": dri_agent,
                    "merge_ref": merge_ref,
                    "provisioner": PROVISIONER_SCRIPT,
                },
                indent=2,
            )
        )
        scratch.replace(path)
    except OSError as exc:
        sys.stderr.write(
            f"warning: could not record the incomplete provisioning at {path} ({exc}).\n"
        )


def clear_provision_incomplete(repo_root: Path) -> None:
    """Drop the record; a provisioner run has completed cleanly."""
    provision_incomplete_path(repo_root).unlink(missing_ok=True)


def default_is_pid_a_live_apply(pid: int) -> bool:
    """Whether ``pid`` is alive and is an ``update_self.py`` process.

    The cmdline check (Linux ``/proc``; on hosts without it, liveness alone)
    guards against PID reuse: a recycled PID must not make a dead apply read as
    live forever.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Alive, but owned by someone else.
    except OSError:
        return False
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        cmdline = (
            cmdline_path.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        )
    except OSError:
        return True  # No /proc (macOS): liveness is the best answer available.
    return "update_self" in cmdline
