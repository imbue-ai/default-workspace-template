#!/usr/bin/env python3
"""Reveal a merged system-interface change to the live UI -- and auto-recover if it breaks.

This is the reveal step of the ``update-system-interface`` flow. The lead agent
merges a verified worker branch into the served working tree, then runs this
script. It owns the *entire* reveal sequence as a single deterministic motion,
because the failure mode is catastrophic: if the ``system-interface`` backend
fails to start, the user loses their whole chat UI and there is nowhere left to
surface an error message. So detection is not enough -- this script must always
leave the served UI in a working state, on its own, without the agent.

What it does, given the pre-merge revision (``--rollback-to``):

1. Refuse to run on a dirty tree (so a rollback can never clobber unrelated work).
2. Classify what changed since the known-good revision (frontend src / frontend
   manifest / backend src / backend manifest).
3. Refresh dependencies only if a manifest changed (``npm ci`` / ``uv tool
   install -e system/apps/system_interface --reinstall``). A plain restart does NOT
   re-resolve the editable tool's dependencies, so a backend dependency add
   would otherwise crash the service on restart.
4. For a backend change, *pre-flight* the merged code on a throwaway port before
   touching the live service -- if it cannot boot, the live service is never
   restarted and we go straight to rollback (the UI never went down).
5. Build the frontend bundle, restart the backend, and tell open browsers to
   reload, as applicable.
6. Probe the live service's loopback endpoint until healthy (with a deadline).
7. On ANY failure, restore the served tree to the known-good revision (as a
   forward revert commit) and re-probe to *confirm* the UI is back. The live
   backend is restarted during recovery only if the failed reveal had already
   restarted it (a failed post-restart health check); when the failure happened
   before the live restart (pre-flight, dependency refresh, frontend build) the
   live service is still serving known-good code and is left untouched, so the
   UI never blips. Only then does the script exit -- reporting what happened via
   its exit code and stderr.

Run via bare ``python3`` (standard library only), like ``forward_port.py`` and
``reload_system_interface``'s predecessor -- it orchestrates the environment, so
it must not depend on any particular venv being synced.

The ``preview`` / ``unpreview`` subcommands are thin system-interface adapters
over the shared ``serve_isolated_instance.py`` motion (the previewable-instance
substrate every service flow shares). They hand it the system-interface
specifics -- boot ``uv run system-interface`` from an already-built
``--work-dir`` on a free port; point layout persistence at a throwaway copy of
the live layout (``SYSTEM_INTERFACE_LAYOUT_DIR``) so the preview renders the
user's real tabs while its autosaves land in the copy, with MNGR_AGENT_ID also
dropped as a guard against clobbering the live ``layout.json``; declare the two
preview service names self-referential
(``SYSTEM_INTERFACE_SELF_REFERENTIAL_SERVICES``) so the preview tab that layout
almost always contains explains itself rather than nesting; keep agent
discovery; source agent lifecycle events by *following* the live observer rather
than running a second one (``SYSTEM_INTERFACE_AGENT_EVENTS_MODE=FOLLOW``, since
``mngr observe`` is single-writer per mngr host dir); probe ``/api/health``,
which refuses to go green unless that lifecycle stream is actually live; and
register the inner app plus the labeled
"preview" wrapper frame the user opens. The shared script owns the ports, the
process/service teardown, and the state file; no fetch, checkout, or rebuild
happens, and the served tree and the previewed folder are never touched.

That ``--work-dir`` is either the **lead's own editing worktree** (the live
editing loop, where no worker exists yet) or a **worker's work_dir** (the
optional final pre-merge preview). Either way it must be a folder that has
already been built, and it must still exist at preview time -- for a worker's
work_dir that means previewing before the worker is destroyed.

The non-deterministic part -- opening the tab and gating on the user's judgment
-- stays with the agent.

Usage:
    python3 reveal_system_interface.py reveal --rollback-to <pre-merge-sha> [--repo-root PATH]
    python3 reveal_system_interface.py preview --slug <name> --work-dir <built-work-dir> [--repo-root PATH]
    python3 reveal_system_interface.py preview-refresh --slug <name> [--repo-root PATH]
    python3 reveal_system_interface.py unpreview --slug <name> [--repo-root PATH]

The ``preview-refresh`` subcommand re-boots the preview's inner app on its
existing port (for a backend round in the live editing loop) so an edit/rebuild
is picked up in place, without disturbing the wrapper frame or the user's tab.

Environment:
    MINDS_WORKSPACE_SERVER_URL  Base URL of the live workspace server
                                (default http://127.0.0.1:8000).
    MNGR_AGENT_ID               Sent for telemetry on the reload broadcast.

Exit codes (``reveal``):
    0  Revealed successfully; live UI is healthy.
    1  Precondition error (dirty tree, bad arguments) -- nothing was changed.
    2  The change was bad and was rolled back; the live UI is confirmed healthy
       on the known-good revision (the requested change did NOT land).
    3  EMERGENCY: even rollback could not restore a healthy UI.

Exit codes (``preview`` / ``preview-refresh`` / ``unpreview``):
    0  Success (preview is up / rebooted in place / torn down).
    1  The preview failed to build or boot (and tore itself down); or there was
       no live preview to refresh, or the rebooted inner app never became
       healthy; or a bad argument / unreadable state file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"

# The served app, the editable tool the live service runs from, and the build
# surfaces. These mirror system/scripts/build_workspace.sh -- the source of truth for
# how the served environment is constructed.
APP_DIR = "system/apps/system_interface"
FRONTEND_DIR = f"{APP_DIR}/frontend"
# The frontend build output the backend serves at ``/``. Both ``node_modules``
# and this ``static/`` bundle are gitignored, so a fresh worktree has neither
# until the worker builds it. The preview serves the worker's app dir as-is and
# will not build for it (see ``preview``): a work_dir without this bundle is a
# worker that skipped its build, and the preview refuses it rather than boot the
# backend's "Frontend not built" placeholder.
FRONTEND_BUILD_INDEX = f"{APP_DIR}/imbue/system_interface/static/index.html"
TOOL_NAME = "system-interface"
RELOAD_OP = "reload_system_interface"

# Pre-merge preview: the deterministic boot + teardown of a previewable instance
# is the shared ``serve_isolated_instance.py`` motion that every service flow
# reuses. ``preview`` / ``unpreview`` below are thin adapters that hand it the
# system-interface specifics; the shared script owns the ports, the
# process/service teardown, and the state file. It lives two levels up under
# ``.agents/shared/scripts/`` and is stdlib-only, so it runs under the same
# interpreter as this script.
_SHARED_SERVE_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "shared"
    / "scripts"
    / "serve_isolated_instance.py"
)
# The service names the preview registers: the inner booted app and the
# outer wrapper the user actually opens. Fixed because the flow runs one preview
# at a time -- enforced by the guard in ``preview`` (a different slug's live
# preview refuses to boot); a re-run of the *same* slug is fine because the
# shared script clears its own stale instance first.
PREVIEW_INNER_SERVICE_NAME = "si-preview-app"
PREVIEW_SERVICE_NAME = "si-preview"
# Where the shared script files each instance's state (mirrors its STATE_ROOT /
# STATE_FILENAME). Used only to detect a different slug's live preview: because
# the service names above are fixed, a second concurrent preview would silently
# hijack the tab of the one already up, and its teardown would later deregister
# the service out from under it.
_INSTANCES_ROOT = "data/.state/isolated-instances"
_INSTANCE_STATE_FILENAME = "instance.json"
# The system interface reads its bind host/port from the environment; the shared
# script injects the free port into PORT and 127.0.0.1 into HOST.
PREVIEW_PORT_ENV = "SYSTEM_INTERFACE_PORT"
PREVIEW_HOST_ENV = "SYSTEM_INTERFACE_HOST"

# Seed the preview with the user's real tab layout. The preview boots pointed at
# a throwaway *copy* of the live layout (via SYSTEM_INTERFACE_LAYOUT_DIR -- the
# env spelling of the server's ``Config.system_interface_layout_dir`` field, which
# it honors ahead of the MNGR_AGENT_ID-derived path) so it renders the
# user's existing tabs while its own layout autosaves land in the copy -- never
# clobbering the live layout. This script owns the copy (the shared serve
# script wipes its own state dir on every boot, so the seed can't live there):
# ``preview`` re-seeds it fresh, ``unpreview`` removes it. Only the layout files
# are copied, never the client-activity log or terminal banner that also live
# under workspace_layout/. The seed persists across a ``preview-refresh`` because
# the shared script records the env override at ``up`` time and reapplies it.
PREVIEW_LAYOUT_DIR_ENV = "SYSTEM_INTERFACE_LAYOUT_DIR"
_PREVIEW_LAYOUT_SEED_ROOT = "data/.state/si-preview-layout"
_WORKSPACE_LAYOUT_SUBDIR = "workspace_layout"
# What gets copied: the per-slug layout contents, the slug registry (display names
# + last-active slug), and any un-migrated legacy single-layout file.
_SEEDED_LAYOUT_ENTRIES = ("layouts", "layouts_meta.json", "layout.json")
ENV_MNGR_HOST_DIR = "MNGR_HOST_DIR"
# The layout belongs to the workspace's *primary* agent -- the services agent
# supervisord (and therefore the live system interface) runs under, which mngr
# labels ``is_primary=true``. It is emphatically NOT whichever agent runs this
# script: the frontend hides is_primary agents from the agent list, so the lead
# driving this skill is always some other agent (a chat agent, or a worktree
# worker) whose state dir has no workspace_layout/ at all. Resolving the primary
# agent by label is the same convention as system/scripts/with_agent_env.sh and
# bootstrap's _read_main_agent_labels.
_AGENT_DATA_FILENAME = "data.json"
_PRIMARY_AGENT_LABEL = "is_primary"

# The seeded layout will nearly always contain the preview tab itself -- it stays
# open across the whole editing pass, so any re-``preview`` copies a layout that
# has it. Rendering it would make the preview show *itself*: the inner app
# resolves ``service:si-preview`` against the same live app registry, so the panel
# proxies back to the wrapper that frames it, and each nested iframe loads another
# full system interface. Telling the previewed instance that those two service
# names resolve to itself makes it serve a one-line explanation in that tab
# instead. That keeps the rest of the layout exactly as the user has it, which
# neither dropping the layout (their real tabs vanish) nor editing dockview's
# serialized grid by hand (fragile, and a malformed grid renders blank) manages.
PREVIEW_SELF_REFERENTIAL_SERVICES_ENV = "SYSTEM_INTERFACE_SELF_REFERENTIAL_SERVICES"
_PREVIEW_SERVICE_NAMES = (PREVIEW_SERVICE_NAME, PREVIEW_INNER_SERVICE_NAME)

# How a throwaway second instance sources agent lifecycle events. ``mngr observe``
# is single-writer per mngr host dir (an exclusive flock), and this box's live
# system interface already holds that lock -- so an instance booted alongside it
# (the preview, the pre-flight) that tried to run its own observer would have it
# die seconds into boot, leaving that instance's agent list and chat panels
# frozen at boot state forever while terminals and everything else kept working.
# FOLLOW makes it read the live observer's event stream instead, which is all a
# read-only second instance ever needed. This is the env spelling of the server's
# ``Config.system_interface_agent_events_mode``.
PREVIEW_AGENT_EVENTS_MODE_ENV = "SYSTEM_INTERFACE_AGENT_EVENTS_MODE"
FOLLOW_AGENT_EVENTS_MODE = "FOLLOW"

# Endpoints used to probe liveness.
#
# ``/api/health`` is the strict gate, used for the *throwaway* instances (the
# preview and the pre-flight boot). It asserts both that a fresh mngr discovery
# works -- the plugin/config path a missing backend dependency or a broken
# plugin-config parse would take down -- and that the instance's agent lifecycle
# event stream is actually live. That second half is why ``/api/agents`` is not
# enough: it runs its own discovery rather than reading the cache the lifecycle
# stream feeds, so it answers 200 on an instance whose agent view is dead. A
# preview that came up looking healthy and showed "No conversation data" for
# every agent created after it booted is exactly the gap this closes.
STRICT_HEALTH_PATH = "/api/health"
# ``/api/agents`` stays the probe for the *live* service (post-restart and during
# recovery). Deliberately the looser check: a rollback here is a heavy, risky
# action, and lifecycle-stream trouble on the live service is not something
# reverting a UI change would fix -- it would just escalate a real problem into a
# spurious rollback, and then into an "even rollback failed" emergency.
HEALTH_PATH = "/api/agents"
SERVE_PATH = "/"

# Poll budget for "did the service come back up". Restart is fire-and-forget, so
# we poll rather than assume.
_HEALTH_ATTEMPTS = 30
_HEALTH_INTERVAL_SECONDS = 1.0
# Pre-flight boot is a fresh process on a throwaway port; give it the same grace.
_PREFLIGHT_ATTEMPTS = 30
_PREFLIGHT_INTERVAL_SECONDS = 1.0


class RevealError(Exception):
    """Base class for reveal failures (avoids raising built-in exceptions)."""


class PreconditionError(RevealError):
    """A precondition was not met; nothing was changed, do not roll back."""


class RevealFailed(RevealError):
    """The reveal of the merged change failed; the caller must roll back.

    ``live_service_restarted`` records whether the live service was already
    (re)started before the failure. It is ``False`` for failures that happen
    before the live restart (pre-flight, dependency refresh, frontend build) --
    in which case the live service is untouched and still serving known-good
    code, so recovery must NOT restart it -- and ``True`` once the restart has
    been attempted, where recovery must restart to reload known-good code.
    """

    def __init__(self, message: str, *, live_service_restarted: bool = False) -> None:
        super().__init__(message)
        self.live_service_restarted = live_service_restarted


@dataclass(frozen=True)
class ChangeSet:
    """Which kinds of system-interface change a diff contains."""

    frontend_src: bool
    frontend_manifest: bool
    backend_src: bool
    backend_manifest: bool

    @property
    def frontend(self) -> bool:
        return self.frontend_src or self.frontend_manifest

    @property
    def backend(self) -> bool:
        return self.backend_src or self.backend_manifest

    @property
    def any(self) -> bool:
        return self.frontend or self.backend


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands.

    The default implementation calls ``subprocess.run`` directly; tests inject a
    recording stub instead.
    """

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)


class HttpClient:
    """Indirection over the loopback HTTP calls (health probe + reload broadcast)."""

    def get_status(self, url: str, timeout: float) -> int | None:
        """Return the HTTP status for a GET, or ``None`` if the host is unreachable."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> int | None:
        """POST a JSON body; return the HTTP status or ``None`` if unreachable."""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None


@dataclass
class Spawned:
    """A handle to a spawned throwaway server process."""

    _process: subprocess.Popen

    def terminate(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self._process.kill()


class Spawner:
    """Indirection over ``subprocess.Popen`` for the pre-flight throwaway boot.

    ``spawn`` returns a managed child (terminated in a ``finally``) used to boot
    the merged backend on a throwaway port before touching the live service.
    """

    def spawn(self, argv: Sequence[str], cwd: str, env: dict) -> Spawned:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return Spawned(_process=process)


def classify_changes(paths: Sequence[str]) -> ChangeSet:
    """Classify repo-relative changed ``paths`` into a :class:`ChangeSet`.

    The frontend build output (``.../static/``) is gitignored and so never
    appears in a diff; we do not need to special-case it here.
    """
    frontend_src = False
    frontend_manifest = False
    backend_src = False
    backend_manifest = False
    for path in paths:
        if path in (
            f"{FRONTEND_DIR}/package.json",
            f"{FRONTEND_DIR}/package-lock.json",
        ):
            frontend_manifest = True
        elif path.startswith(f"{FRONTEND_DIR}/src/"):
            frontend_src = True
        elif path == f"{APP_DIR}/pyproject.toml" or path == "uv.lock":
            backend_manifest = True
        elif (
            path.startswith(f"{APP_DIR}/imbue/")
            and path.endswith(".py")
            and not _is_test_file(path)
        ):
            backend_src = True
    return ChangeSet(
        frontend_src=frontend_src,
        frontend_manifest=frontend_manifest,
        backend_src=backend_src,
        backend_manifest=backend_manifest,
    )


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith("_test.py") or name.startswith("test_")


def find_free_port() -> int:
    """Bind to an ephemeral port, then release it for the throwaway server to take."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _diff_name_status(
    repo_root: Path, rollback_to: str, runner: Runner
) -> list[tuple[str, str]]:
    """Return ``(status, path)`` pairs for ``rollback_to..HEAD``.

    ``--no-renames`` makes a rename surface as a delete + add pair, which keeps
    the rollback logic simple (restore the deletes, remove the adds).
    """
    result = runner.run(
        ["git", "diff", "--no-renames", "--name-status", rollback_to, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        pairs.append((fields[0].strip(), fields[-1].strip()))
    return pairs


def _assert_clean_tree(repo_root: Path, runner: Runner) -> None:
    result = runner.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise PreconditionError(
            "working tree has uncommitted changes; refusing to reveal so a rollback "
            "can never clobber unrelated work. Commit or stash, then re-run."
        )


def _run_checked(
    runner: Runner,
    argv: Sequence[str],
    cwd: Path,
    what: str,
    *,
    live_service_restarted: bool = False,
) -> None:
    """Run a reveal command; raise :class:`RevealFailed` on a non-zero exit.

    ``live_service_restarted`` is forwarded onto the raised exception so callers
    that run the live restart can record that recovery must restart (see
    :class:`RevealFailed`)."""
    result = runner.run(
        list(argv), cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if getattr(result, "returncode", 0) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise RevealFailed(
            f"{what} failed (exit {result.returncode}): {stderr}",
            live_service_restarted=live_service_restarted,
        )


def wait_healthy(
    http: HttpClient,
    url: str,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> bool:
    """Poll ``url`` until it returns HTTP 200, up to ``attempts`` times."""
    for index in range(attempts):
        if http.get_status(url, timeout=5.0) == 200:
            return True
        if index < attempts - 1:
            sleeper(interval)
    return False


def _preflight_ok(
    repo_root: Path,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
) -> bool:
    """Boot the merged backend on a throwaway port and probe it, without touching
    the live service. Returns True iff it serves a healthy response.

    Runs in FOLLOW mode: this boots *alongside* the still-running live service,
    which holds the single-writer observe lock, so a pre-flight that tried to run
    its own observer would lose the lock and boot with a dead agent view -- and
    then pass anyway, because the old ``/api/agents`` probe never looked at the
    lifecycle stream. Following the live observer makes the pre-flight both able
    to come up and able to prove the merged backend really can serve a live agent
    view, which is what this gate is for.
    """
    port = find_free_port()
    env = dict(os.environ)
    env["SYSTEM_INTERFACE_HOST"] = "127.0.0.1"
    env["SYSTEM_INTERFACE_PORT"] = str(port)
    env[PREVIEW_AGENT_EVENTS_MODE_ENV] = FOLLOW_AGENT_EVENTS_MODE
    spawned = spawner.spawn([TOOL_NAME], cwd=str(repo_root / APP_DIR), env=env)
    try:
        return wait_healthy(
            http,
            f"http://127.0.0.1:{port}{STRICT_HEALTH_PATH}",
            _PREFLIGHT_ATTEMPTS,
            _PREFLIGHT_INTERVAL_SECONDS,
            sleeper,
        )
    finally:
        spawned.terminate()


def _broadcast_reload(http: HttpClient, base_url: str) -> None:
    """Tell open browsers to reload the whole UI. Best-effort: a no-op when no
    browser is connected, and never fatal on its own."""
    agent_id = os.environ.get(ENV_MNGR_AGENT_ID, "")
    status = http.post_json(
        f"{base_url}/api/layout/broadcast",
        {"op": RELOAD_OP, "args": {}, "agent_id": agent_id},
        {"Content-Type": "application/json", MNGR_AGENT_ID_HEADER: agent_id},
        timeout=10.0,
    )
    if status != 200:
        sys.stderr.write(
            f"warning: reload broadcast returned {status}; if a browser is open it may "
            "not have refreshed (the new bundle is still on disk and will load on next visit).\n"
        )


def _refresh_dependencies(changes: ChangeSet, repo_root: Path, runner: Runner) -> None:
    if changes.frontend_manifest:
        _run_checked(runner, ["npm", "ci"], repo_root / FRONTEND_DIR, "npm ci")
    if changes.backend_manifest:
        _run_checked(
            runner,
            ["uv", "tool", "install", "-e", APP_DIR, "--reinstall"],
            repo_root,
            "uv tool install --reinstall",
        )


def _apply_reveal(
    changes: ChangeSet,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
) -> None:
    """Refresh deps, build, restart, and reload as applicable. Raises
    :class:`RevealFailed` the moment any step does not end healthy."""
    _refresh_dependencies(changes, repo_root, runner)
    if changes.frontend:
        _run_checked(
            runner, ["npm", "run", "build"], repo_root / FRONTEND_DIR, "npm run build"
        )
    if changes.backend:
        if not _preflight_ok(repo_root, http, spawner, sleeper):
            # Live service was never restarted, so it is still serving known-good
            # code -- recovery must not restart it (live_service_restarted=False).
            raise RevealFailed(
                "merged backend failed to boot in a pre-flight check; live service not restarted"
            )
        # From here on the live service has been (or is being) restarted, so any
        # failure leaves it potentially running broken code: recovery must restart.
        _run_checked(
            runner,
            ["mngr", "start", "--restart", "system-services"],
            repo_root,
            "mngr start --restart",
            live_service_restarted=True,
        )
        if not wait_healthy(
            http,
            f"{base_url}{HEALTH_PATH}",
            _HEALTH_ATTEMPTS,
            _HEALTH_INTERVAL_SECONDS,
            sleeper,
        ):
            raise RevealFailed(
                "backend did not become healthy after restart",
                live_service_restarted=True,
            )
    if changes.frontend:
        _broadcast_reload(http, base_url)


def _restore_tree(
    name_status: Sequence[tuple[str, str]],
    rollback_to: str,
    repo_root: Path,
    runner: Runner,
) -> None:
    """Restore every changed path to its ``rollback_to`` state, staged for commit.

    Added-since paths are removed; modified/deleted paths are checked out from
    the known-good revision. Build output is gitignored and untouched here -- the
    recovery rebuild regenerates it.
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


def _commit_rollback(
    repo_root: Path, runner: Runner, rollback_to: str, reason: str
) -> None:
    message = (
        f"Roll back system-interface reveal (restore to {rollback_to[:12]})\n\n{reason}"
    )
    runner.run(
        ["git", "commit", "--no-verify", "-m", message],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )


def _recover_running_state(
    changes: ChangeSet,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    sleeper: Callable[[float], None],
    live_service_restarted: bool,
) -> bool:
    """After the tree is restored to known-good, rebuild/restart from it as
    needed and confirm the live UI is healthy. Returns True iff confirmed healthy.

    ``live_service_restarted`` says whether the failed reveal had already
    restarted the live backend. When it did not (pre-flight / dependency-refresh
    / frontend-build failures), the live service is still running known-good code
    in memory and the on-disk tree has just been restored to match it, so we must
    NOT restart -- doing so would needlessly blip a healthy UI. We only restart
    when the failed reveal had actually restarted the service into broken code.

    Unlike :func:`_apply_reveal`, nothing here raises -- this is the last line of
    defense, so a failed step just means "not recovered" (exit 3)."""
    try:
        _refresh_dependencies(changes, repo_root, runner)
        if changes.frontend:
            _run_checked(
                runner,
                ["npm", "run", "build"],
                repo_root / FRONTEND_DIR,
                "npm run build",
            )
        if changes.backend:
            if live_service_restarted:
                _run_checked(
                    runner,
                    ["mngr", "start", "--restart", "system-services"],
                    repo_root,
                    "mngr start --restart",
                )
            # Probe the backend health endpoint either way: after a restart to
            # confirm known-good booted, or (no restart) to confirm the untouched
            # service is still serving.
            healthy = wait_healthy(
                http,
                f"{base_url}{HEALTH_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
        else:
            # Frontend-only: the server was never restarted; confirm it still serves.
            healthy = wait_healthy(
                http,
                f"{base_url}{SERVE_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
    except RevealFailed as exc:
        sys.stderr.write(f"recovery step failed: {exc}\n")
        return False
    if healthy and changes.frontend:
        _broadcast_reload(http, base_url)
    return healthy


def reveal(
    rollback_to: str,
    repo_root: Path,
    *,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
    base_url: str | None = None,
) -> int:
    """Run the full reveal-and-recover sequence. Returns the process exit code."""
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    _assert_clean_tree(repo_root, runner)
    name_status = _diff_name_status(repo_root, rollback_to, runner)
    changes = classify_changes([path for _, path in name_status])
    if not changes.any:
        sys.stderr.write(
            f"no system-interface changes since {rollback_to[:12]}; nothing to reveal.\n"
        )
        return 0

    try:
        _apply_reveal(changes, repo_root, resolved_base, runner, http, spawner, sleeper)
    except RevealFailed as exc:
        sys.stderr.write(
            f"reveal failed: {exc}\nrolling back to {rollback_to[:12]} and restoring the live UI...\n"
        )
        _restore_tree(name_status, rollback_to, repo_root, runner)
        _commit_rollback(
            repo_root,
            runner,
            rollback_to,
            f"Reveal failed and was auto-reverted: {exc}",
        )
        if _recover_running_state(
            changes,
            repo_root,
            resolved_base,
            runner,
            http,
            sleeper,
            live_service_restarted=exc.live_service_restarted,
        ):
            sys.stderr.write(
                "rolled back to last-known-good; the live UI is confirmed healthy. "
                "The requested change did NOT land -- diagnose it before retrying.\n"
            )
            return 2
        sys.stderr.write(
            "EMERGENCY: rollback did not restore a healthy UI. The system interface may be down; "
            "manual intervention is required.\n"
        )
        return 3

    sys.stderr.write(
        "revealed: the live system interface is updated and confirmed healthy.\n"
    )
    return 0


def _preview_instance_name(slug: str) -> str:
    """The name the shared script files this preview's instance under (its state
    dir + the stable id ``unpreview`` tears down). One preview per slug."""
    return f"{PREVIEW_SERVICE_NAME}-{slug}"


def _find_other_preview(repo_root: Path, slug: str) -> str | None:
    """Return another slug's live preview instance name, or ``None``.

    Only a *different* slug's preview blocks: both would register the same fixed
    service names, so booting a second one hijacks the first's tab. Re-running
    the same slug stays allowed -- the shared script clears its own stale
    instance, which is the normal retry path.
    """
    instances_root = repo_root / _INSTANCES_ROOT
    if not instances_root.is_dir():
        return None
    own_name = _preview_instance_name(slug)
    prefix = f"{PREVIEW_SERVICE_NAME}-"
    for state_dir in sorted(instances_root.iterdir()):
        if not state_dir.name.startswith(prefix) or state_dir.name == own_name:
            continue
        if (state_dir / _INSTANCE_STATE_FILENAME).exists():
            return state_dir.name
    return None


def _mngr_host_dir() -> Path:
    """The mngr host dir, mirroring the system interface's own resolver."""
    return Path(os.environ.get(ENV_MNGR_HOST_DIR, "") or (Path.home() / ".mngr"))


def _is_primary_agent_data(data_path: Path) -> bool:
    """Whether this agent record carries the ``is_primary=true`` label."""
    try:
        data = json.loads(data_path.read_text())
    except (OSError, ValueError):
        # A half-written or unreadable record just isn't a match; the scan moves
        # on rather than failing the whole preview over one bad file.
        return False
    if not isinstance(data, dict):
        return False
    labels = data.get("labels")
    if not isinstance(labels, dict):
        return False
    # Labels round-trip through pydantic serialization, so coerce rather than
    # comparing against a bare `True`.
    return str(labels.get(_PRIMARY_AGENT_LABEL, "")).lower() == "true"


def _live_layout_dir() -> Path | None:
    """The live workspace's layout dir, or None if the primary agent isn't found.

    The system interface persists layouts under *its own* agent's state dir --
    ``$MNGR_HOST_DIR/agents/<primary-agent-id>/workspace_layout/`` -- and it runs
    under the workspace's services agent, the one mngr labels
    ``is_primary=true``. So this resolves that agent by scanning the host dir's
    agent records, exactly as ``system/scripts/with_agent_env.sh`` does.

    It deliberately does *not* use the ambient ``MNGR_AGENT_ID``, even though the
    server's resolver does: the server reads its own id, whereas here the ambient
    id belongs to whoever ran this script -- the lead chat agent, or a worker.
    Those agents never own a workspace_layout/, so deriving the path from that id
    resolved somewhere that does not exist and silently seeded nothing, which is
    why the preview always opened with the fresh-workspace layout.

    Returns None when the host dir has no primary agent (dev/test, or a host that
    isn't a minds workspace); the caller reports that rather than seeding blind.
    """
    agents_dir = _mngr_host_dir() / "agents"
    if not agents_dir.is_dir():
        return None
    for state_dir in sorted(agents_dir.iterdir()):
        if _is_primary_agent_data(state_dir / _AGENT_DATA_FILENAME):
            return state_dir / _WORKSPACE_LAYOUT_SUBDIR
    return None


def _preview_layout_seed_dir(repo_root: Path, slug: str) -> Path:
    """Where this slug's throwaway layout copy lives (gitignored runtime state)."""
    return repo_root / _PREVIEW_LAYOUT_SEED_ROOT / _preview_instance_name(slug)


def _seed_preview_layout(repo_root: Path, slug: str) -> Path:
    """Copy the live layout files verbatim into a fresh throwaway dir; return it.

    Re-seeded from scratch on every ``preview`` call, since the live layout is
    the source of truth at preview time. When there is no live layout to copy the
    dir is left empty and the preview renders the fresh-workspace state -- but it
    says so on stderr first. "The preview opened with default tabs" and "seeding
    found nothing" are indistinguishable on screen, so a silent empty seed reads
    as a working preview; that silence is what hid the primary-agent resolution
    bug (see ``_live_layout_dir``) through a whole round of real use.

    Every layout is copied as-is, including one that opens the preview tab
    itself -- which, in the editing loop, is nearly all of them, since that tab
    stays open across the whole pass. The nesting that would cause is refused by
    the previewed instance instead (``system_interface_self_referential_services``
    below), which is the only place that can do it without either editing
    dockview's serialized grid by hand or silently dropping the user's real tabs.
    """
    seed_dir = _preview_layout_seed_dir(repo_root, slug)
    shutil.rmtree(seed_dir, ignore_errors=True)
    seed_dir.mkdir(parents=True, exist_ok=True)
    live_dir = _live_layout_dir()
    if live_dir is None:
        sys.stderr.write(
            f"preview: no agent under {_mngr_host_dir() / 'agents'} carries the "
            f"'{_PRIMARY_AGENT_LABEL}=true' label, so the live layout could not be "
            "located; the preview will open with the default tabs rather than "
            "yours.\n"
        )
        return seed_dir
    if not live_dir.is_dir():
        sys.stderr.write(
            f"preview: {live_dir} does not exist, so this workspace has no saved "
            "layout yet; the preview will open with the default tabs.\n"
        )
        return seed_dir
    for entry in _SEEDED_LAYOUT_ENTRIES:
        source = live_dir / entry
        if source.is_dir():
            # The per-slug layout contents. Copied file-by-file rather than with
            # copytree so a nested sub-tree under the same dir could never ride
            # along; today only flat ``<slug>.json`` files live here.
            destination_dir = seed_dir / entry
            destination_dir.mkdir(parents=True, exist_ok=True)
            for layout_file in sorted(source.iterdir()):
                if layout_file.is_file():
                    shutil.copy2(layout_file, destination_dir / layout_file.name)
        elif source.is_file():
            shutil.copy2(source, seed_dir / entry)
    return seed_dir


def preview(slug: str, work_dir: str, repo_root: Path, *, runner: Runner) -> int:
    """Stand up a preview of an already-built ``work_dir``.

    ``work_dir`` is the lead's own editing worktree during the live editing loop
    (no worker exists yet), or a worker's work_dir for the optional final
    pre-merge preview. Either way it must already be built, and it must still
    exist -- for a worker's work_dir, run this before the worker is destroyed.

    Thin system-interface adapter over the shared ``serve_isolated_instance.py``
    ``up`` motion: validate the app dir, require that its frontend bundle was
    built, then hand the shared script the system-interface specifics --
    boot ``uv run system-interface`` from that already-built app dir on a
    free port; point layout persistence at a throwaway copy of the live layout
    (``SYSTEM_INTERFACE_LAYOUT_DIR``) so the preview renders the user's real tabs
    while its autosaves land in the copy, and additionally drop MNGR_AGENT_ID as a
    belt-and-suspenders guard against clobbering the live ``layout.json``; declare
    its own two service names self-referential
    (``SYSTEM_INTERFACE_SELF_REFERENTIAL_SERVICES``) so the preview tab the seeded
    layout almost always contains renders an explanation instead of nesting the
    preview inside itself; keep discovery so real conversations still render; run
    in FOLLOW mode so the preview reads the live observer's agent lifecycle stream
    instead of trying to start a second observer it cannot get the lock for; probe
    ``/api/health``, which stays red unless that stream really is feeding the
    preview; register the inner app and the labeled wrapper frame. The shared
    script owns the ports, the process/service teardown, and the state file.

    Because the health gate is strict, a preview whose lifecycle stream cannot be
    established does not come up at all -- the shared script tears the partial
    instance down and this returns non-zero. That is deliberate: a preview whose
    agent view is silently frozen is worse than no preview, because the user
    reads it as the real UI.
    """
    # Sanity-check the work_dir before disturbing anything: a wrong --work-dir
    # should fail fast rather than reaching the shared script.
    previewed_app_dir = Path(work_dir) / APP_DIR
    if not previewed_app_dir.is_dir():
        sys.stderr.write(
            f"preview: {previewed_app_dir} is not a directory; is --work-dir "
            "correct, and does that folder still exist (an editing worktree that "
            "was removed, or a worker that was destroyed)?\n"
        )
        return 1
    # The preview serves the app dir as-is; it never builds for it. A work_dir
    # without a frontend bundle means whoever produced it skipped the build (a
    # fresh worktree has no gitignored static/ until built), so booting would only
    # serve the backend's "Frontend not built" placeholder -- a dead preview that
    # reads as working. Refuse loudly and point at the fix.
    if not (Path(work_dir) / FRONTEND_BUILD_INDEX).exists():
        sys.stderr.write(
            f"preview: no frontend build in {work_dir} "
            f"({FRONTEND_BUILD_INDEX} is missing), so the preview would serve the "
            "'Frontend not built' placeholder. Build the frontend first "
            "(cd system/apps/system_interface/frontend && npm ci && npm run build) "
            "-- in your editing worktree, or by re-briefing the worker -- then "
            "retry.\n"
        )
        return 1
    other = _find_other_preview(repo_root, slug)
    if other is not None:
        other_slug = other.removeprefix(f"{PREVIEW_SERVICE_NAME}-")
        sys.stderr.write(
            f"preview: another pass's preview is already up ({other}); the "
            f"'{PREVIEW_SERVICE_NAME}' tab can only show one at a time, so booting "
            "this one would hijack it. Surface this to the user and coordinate "
            "with that pass -- or, if it is abandoned, tear it down first with "
            f"'unpreview --slug {other_slug}'.\n"
        )
        return 1
    seed_dir = _seed_preview_layout(repo_root, slug)
    result = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "up",
            "--name",
            _preview_instance_name(slug),
            "--cwd",
            str(previewed_app_dir),
            "--port-env",
            PREVIEW_PORT_ENV,
            "--host-env",
            PREVIEW_HOST_ENV,
            "--env",
            f"{PREVIEW_LAYOUT_DIR_ENV}={seed_dir}",
            "--env",
            f"{PREVIEW_AGENT_EVENTS_MODE_ENV}={FOLLOW_AGENT_EVENTS_MODE}",
            "--env",
            (
                f"{PREVIEW_SELF_REFERENTIAL_SERVICES_ENV}="
                f"{','.join(_PREVIEW_SERVICE_NAMES)}"
            ),
            "--unset-env",
            ENV_MNGR_AGENT_ID,
            "--health-path",
            STRICT_HEALTH_PATH,
            "--service-name",
            PREVIEW_INNER_SERVICE_NAME,
            "--preview-service-name",
            PREVIEW_SERVICE_NAME,
            "--preview-title",
            slug,
            "--repo-root",
            str(repo_root),
            "--",
            "uv",
            "run",
            TOOL_NAME,
        ],
        cwd=str(repo_root),
        check=False,
    )
    returncode = int(getattr(result, "returncode", 0))
    if returncode != 0:
        # The boot failed, so nothing will consume the copy we just seeded and no
        # ``unpreview`` is owed for an instance that never came up. Remove it here
        # rather than leaving a stale layout copy behind for a preview that does
        # not exist.
        shutil.rmtree(seed_dir, ignore_errors=True)
    return returncode


def preview_refresh(slug: str, repo_root: Path, *, runner: Runner) -> int:
    """Re-boot the preview's inner app on its existing port via the shared script.

    Thin adapter over ``serve_isolated_instance.py refresh``. Used during the live
    editing loop for a **backend** round: after the lead rebuilds/edits the code
    in its worktree (which the inner app runs from), this bounces just the inner
    app process on the same port so the new backend is picked up -- leaving the
    wrapper frame, the service registration, and the user's tab untouched. A
    frontend-only round needs no process bounce (the inner app serves the rebuilt
    ``static/`` bundle straight from disk); the lead just rebuilds and reloads the
    tab's iframe with ``layout.py refresh si-preview``. Either way the lead
    reloads the iframe itself afterward -- this never touches the tab. Returns the
    shared script's exit code (0 healthy; 1 if there is nothing to refresh or the
    rebooted app did not come up)."""
    result = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "refresh",
            "--name",
            _preview_instance_name(slug),
            "--repo-root",
            str(repo_root),
        ],
        cwd=str(repo_root),
        check=False,
    )
    return int(getattr(result, "returncode", 0))


def unpreview(slug: str, repo_root: Path, *, runner: Runner) -> int:
    """Tear down the preview for ``slug`` via the shared script. Idempotent: a
    missing instance is a no-op success, so this is safe on reject, after a
    successful reveal, or to recover from a half-set-up preview."""
    result = runner.run(
        [
            sys.executable,
            str(_SHARED_SERVE_SCRIPT),
            "down",
            "--name",
            _preview_instance_name(slug),
            "--repo-root",
            str(repo_root),
        ],
        cwd=str(repo_root),
        check=False,
    )
    # Remove the throwaway layout copy this preview booted from (the shared
    # script only tears down its own state dir, not ours). Idempotent.
    shutil.rmtree(_preview_layout_seed_dir(repo_root, slug), ignore_errors=True)
    return int(getattr(result, "returncode", 0))


def _add_repo_root_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Manage the system-interface update lifecycle: preview a built "
            "work_dir as a labeled tab, refresh that preview in place, reveal a "
            "merged change with auto-recovery, and tear the preview down."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reveal_parser = subparsers.add_parser(
        "reveal", help="Reveal a merged change to the live UI, with auto-recovery."
    )
    reveal_parser.add_argument(
        "--rollback-to",
        required=True,
        help="The known-good revision to restore to if the reveal fails (the pre-merge HEAD).",
    )
    _add_repo_root_arg(reveal_parser)

    preview_parser = subparsers.add_parser(
        "preview",
        help="Boot an already-built work_dir and serve it as a previewable tab, "
        "before any merge.",
    )
    preview_parser.add_argument(
        "--slug",
        required=True,
        help="Short kebab-case id for this preview (names the service/state dir).",
    )
    preview_parser.add_argument(
        "--work-dir",
        required=True,
        help="A built work_dir to serve: the lead's editing worktree during the "
        "live loop, or a worker's work_dir (from `mngr ls --include "
        "'name==\"<worker>\"' --format json` -> agent.work_dir) for a final "
        "pre-merge preview. It must still exist when this runs.",
    )
    _add_repo_root_arg(preview_parser)

    preview_refresh_parser = subparsers.add_parser(
        "preview-refresh",
        help="Re-boot the preview's inner app on its existing port to pick up a "
        "backend edit/rebuild, without touching the wrapper or the user's tab.",
    )
    preview_refresh_parser.add_argument(
        "--slug", required=True, help="The slug passed to 'preview'."
    )
    _add_repo_root_arg(preview_refresh_parser)

    unpreview_parser = subparsers.add_parser(
        "unpreview",
        help="Tear down a preview (kill the server, deregister the service). Idempotent.",
    )
    unpreview_parser.add_argument(
        "--slug", required=True, help="The slug passed to 'preview'."
    )
    _add_repo_root_arg(unpreview_parser)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.command == "reveal":
            return reveal(
                args.rollback_to,
                repo_root,
                runner=Runner(),
                http=HttpClient(),
                spawner=Spawner(),
            )
        if args.command == "preview":
            return preview(
                args.slug,
                args.work_dir,
                repo_root,
                runner=Runner(),
            )
        if args.command == "preview-refresh":
            return preview_refresh(args.slug, repo_root, runner=Runner())
        if args.command == "unpreview":
            return unpreview(args.slug, repo_root, runner=Runner())
        parser.error(f"unknown command: {args.command}")
        return 1
    except PreconditionError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"error: git command failed: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
