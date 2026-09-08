"""Liveness and frontend probes: the pre-flight boot of the merged backend, the health
poll, the served-bundle check, and the view refresh that follows a change.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from update_banding import ExpendWrapper, as_expendable
from update_layout import SYSTEM_INTERFACE_DIR, TOOL_NAME
from update_runtime import (
    FrontendProbe,
    HttpClient,
    Runner,
    Spawner,
    find_free_port,
    tail,
)

# The shared post-change refresh motion, repo-relative. It owns *how* a changed
# interface is pushed to whoever is looking; this script only decides *when*.
_REFRESH_SCRIPT = "system/scripts/refresh_workspace_view.py"

_REFRESH_TIMEOUT_SECONDS = 120.0

# Header the backend stamps on the app shell: ``false`` on the "not built"
# placeholder, ``true`` on the real app.
FRONTEND_BUILT_HEADER = "x-frontend-built"

# The hashed module script the built index.html loads -- what distinguishes the
# real app shell from the placeholder even on a backend too old for the header.
_ASSET_REFERENCE_PATTERN = re.compile(r"/assets/([A-Za-z0-9._-]+\.js)")

# Endpoints used to probe liveness.
#
# ``/api/health`` is the strict gate, used for the throwaway pre-flight boot. It
# asserts both that a fresh mngr discovery works -- the plugin/config path a
# missing backend dependency or a broken plugin-config parse would take down --
# and that the instance's agent lifecycle event stream is actually live. That
# second half is why ``/api/agents`` is not enough there: it runs its own
# discovery rather than reading the cache the lifecycle stream feeds, so it
# answers 200 on an instance whose agent view is dead.
STRICT_HEALTH_PATH = "/api/health"
# ``/api/agents`` stays the probe for the *live* service (post-restart and during
# recovery). Deliberately the looser check: a rollback here is a heavy, risky
# action, and lifecycle-stream trouble on the live service is not something
# reverting a change would fix -- it would just escalate a real problem into a
# spurious rollback, and then into an "even rollback failed" emergency.
HEALTH_PATH = "/api/agents"

# How a throwaway second instance sources agent lifecycle events. ``mngr observe``
# is single-writer per mngr host dir (an exclusive flock), and the live system
# interface already holds that lock -- so a pre-flight boot that tried to run its
# own observer would have it die seconds into boot, leaving the instance's agent
# view frozen while everything else worked, and then pass a probe that never
# looked at the lifecycle stream. FOLLOW makes it read the live observer's event
# stream instead. This is the env spelling of the server's
# ``Config.system_interface_agent_events_mode``.
PREFLIGHT_AGENT_EVENTS_MODE_ENV = "SYSTEM_INTERFACE_AGENT_EVENTS_MODE"
FOLLOW_AGENT_EVENTS_MODE = "FOLLOW"

# The supervisord program that runs the live system interface, and the client
# used to ask whether it has settled.
SUPERVISOR_PROGRAM = "system_interface"
_SUPERVISORCTL = "supervisorctl"
# How many consecutive healthy answers (one poll interval apart) make a verdict.
# One 200 is a point-in-time probe, not settled state: it reads green in a gap
# between two restarts and red on a change that was never broken. Since this
# verdict is what arms the automatic rollback, both directions are expensive --
# green ships a stack that is still turning over, red reverts a good change.
SETTLED_HEALTHY_PROBES = 3

SERVE_PATH = "/"

# Poll budgets. The health and pre-flight budgets are deliberately generous: a
# loaded workspace boots a healthy backend well past the 30s the old reveal
# allowed, and a budget that is too short reads as "your change was bad" over a
# change that was fine -- with the whole release as blast radius and a retry
# that is correctly refused. A budget that is too long costs seconds only on a
# genuinely broken change (the pre-flight also stops early when the boot
# process dies). Tune these down against the per-phase timings the apply
# marker records, not by guesswork.
HEALTH_ATTEMPTS = 240

HEALTH_INTERVAL_SECONDS = 1.0

_PREFLIGHT_ATTEMPTS = 240

_PREFLIGHT_INTERVAL_SECONDS = 1.0

_FRONTEND_PROBE_ATTEMPTS = 5

_FRONTEND_PROBE_INTERVAL_SECONDS = 1.0

_PREFLIGHT_OUTPUT_TAIL_LINES = 40


def wait_healthy(
    http: HttpClient,
    url: str,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Poll ``url`` until it returns HTTP 200, up to ``attempts`` times."""
    for index in range(attempts):
        if http.get_status(url, timeout=5.0) == 200:
            return True
        if should_stop is not None and should_stop():
            return False
        if index < attempts - 1:
            sleeper(interval)
    return False


def preflight(
    repo_root: Path,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
    expend: ExpendWrapper = as_expendable,
) -> str | None:
    """Boot the merged backend on a throwaway port and probe it, without
    touching the live service. Returns ``None`` iff it serves a healthy
    response; otherwise what went wrong -- the tail of what the throwaway boot
    wrote, or, for a boot that could not be spawned at all, a line saying so.

    Healthy means ``STRICT_HEALTH_PATH``: the merged backend has to prove it can
    serve a *live* agent view, not just answer a discovery, which is what this
    gate exists to establish before the live service is restarted into it."""
    port = find_free_port()
    env = dict(os.environ)
    env["SYSTEM_INTERFACE_HOST"] = "127.0.0.1"
    env["SYSTEM_INTERFACE_PORT"] = str(port)
    # The caller is an agent, so its environment carries MNGR_AGENT_ID -- under
    # which the throwaway boot would persist layout state as if it were that
    # agent, clobbering the live layout.json. The preview flow
    # (reveal_system_interface.py) drops it for the same reason; the pre-flight
    # is just as much a throwaway boot and gets the same guard.
    env.pop("MNGR_AGENT_ID", None)
    # Boots *alongside* the still-running live service, which holds the
    # single-writer observe lock -- so it follows the live observer's stream
    # rather than losing a race for the lock and booting with a dead agent view.
    env[PREFLIGHT_AGENT_EVENTS_MODE_ENV] = FOLLOW_AGENT_EVENTS_MODE
    with tempfile.TemporaryDirectory() as scratch:
        output_path = Path(scratch) / "preflight-boot.log"
        try:
            spawned = spawner.spawn(
                expend([TOOL_NAME]),
                cwd=str(repo_root / SYSTEM_INTERFACE_DIR),
                env=env,
                output_path=output_path,
            )
        except OSError as exc:
            # Not booting and failing is the same verdict as failing to boot,
            # and reaching this with the console script missing is exactly what
            # a tool reinstall that half-succeeded leaves behind.
            return f"the merged backend could not be launched ({type(exc).__name__}: {exc})"
        try:
            if wait_healthy(
                http,
                f"http://127.0.0.1:{port}{STRICT_HEALTH_PATH}",
                _PREFLIGHT_ATTEMPTS,
                _PREFLIGHT_INTERVAL_SECONDS,
                sleeper,
                should_stop=spawned.has_exited,
            ):
                return None
        finally:
            spawned.terminate()
        return tail(spawned.read_output(), _PREFLIGHT_OUTPUT_TAIL_LINES)


def parse_supervisor_pid(status_output: str) -> str | None:
    """The pid a ``supervisorctl status`` line reports, or None if it is not RUNNING.

    The line reads ``system_interface   RUNNING   pid 1234, uptime 0:00:05``.
    Every other state (STARTING, BACKOFF, FATAL, STOPPED) names no settled
    process, so it answers None -- which is the same answer a supervisorctl that
    could not be reached gets, because neither is evidence the service has
    settled, and that is the only question asked here.
    """
    if "RUNNING" not in status_output:
        return None
    marker = "pid "
    index = status_output.find(marker)
    if index == -1:
        return None
    return status_output[index + len(marker) :].split(",")[0].strip() or None


def _live_service_pid(repo_root: Path, runner: Runner) -> str | None:
    """Ask supervisord for the live system interface's pid (None if not RUNNING)."""
    result = runner.run(
        [_SUPERVISORCTL, "status", SUPERVISOR_PROGRAM],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_supervisor_pid(getattr(result, "stdout", "") or "")


def wait_settled(
    http: HttpClient,
    url: str,
    repo_root: Path,
    runner: Runner,
    sleeper: Callable[[float], None],
    *,
    require_stable_pid: bool,
    attempts: int = HEALTH_ATTEMPTS,
) -> bool:
    """Whether the live service reaches -- and holds -- a healthy state.

    Requires ``SETTLED_HEALTHY_PROBES`` consecutive healthy answers rather than
    one, and, when the caller has just restarted the service, that supervisord
    reports it RUNNING on the same pid throughout. A pid that turns over mid-run
    restarts the confirmation instead of failing it: the stack is still settling
    (the services agent is supervisord's parent, so a restart turns every
    program over), which is the situation this exists to wait out rather than to
    judge.

    ``require_stable_pid`` is set only by callers that ran the restart themselves,
    so a workspace where supervisorctl cannot be reached never turns into a
    spurious "the UI is down" -- the restart would already have failed loudly.
    """
    healthy_streak = 0
    streak_pid: str | None = None
    for index in range(attempts):
        is_healthy = http.get_status(url, timeout=5.0) == 200
        pid = _live_service_pid(repo_root, runner) if require_stable_pid else None
        if is_healthy and (pid is not None or not require_stable_pid):
            if healthy_streak == 0 or pid != streak_pid:
                healthy_streak = 1
                streak_pid = pid
            else:
                healthy_streak += 1
            if healthy_streak >= SETTLED_HEALTHY_PROBES:
                return True
        else:
            healthy_streak = 0
            streak_pid = None
        if index < attempts - 1:
            sleeper(HEALTH_INTERVAL_SECONDS)
    return False


def probe_frontend(http: HttpClient, base_url: str) -> FrontendProbe:
    """Ask the live UI whether it is serving a working frontend.

    Asks the two questions a browser would -- is this the real app shell, and
    does its module script actually load as JavaScript -- which together cover
    both the missing-bundle state and the blank screen an unserved ``/assets``
    path produces.
    """
    shell = http.get_page(f"{base_url}{SERVE_PATH}", timeout=10.0)
    if shell is None:
        return FrontendProbe(
            "the live service did not answer a request for the app shell",
            is_answered=False,
        )
    if shell.status != 200:
        return FrontendProbe(
            f"the app shell returned HTTP {shell.status}", is_answered=True
        )
    if shell.headers.get(FRONTEND_BUILT_HEADER) == "false":
        return FrontendProbe(
            "the live service is serving the 'frontend not built' placeholder -- the compiled bundle is missing",
            is_answered=True,
        )
    match = _ASSET_REFERENCE_PATTERN.search(shell.body)
    if match is None:
        return FrontendProbe(
            "the app shell loads no bundled script, so it is not the built app",
            is_answered=True,
        )
    asset_url = f"{base_url}/assets/{match.group(1)}"
    asset = http.get_page(asset_url, timeout=10.0)
    if asset is None:
        return FrontendProbe(
            f"the live service did not answer a request for the bundled script {asset_url}",
            is_answered=False,
        )
    if asset.status != 200:
        return FrontendProbe(
            f"the bundled script {asset_url} returned HTTP {asset.status}",
            is_answered=True,
        )
    if "javascript" not in asset.content_type:
        return FrontendProbe(
            f"the bundled script {asset_url} came back as '{asset.content_type}' rather than JavaScript, "
            "so the browser will refuse it and render a blank page",
            is_answered=True,
        )
    return FrontendProbe(None, is_answered=True)


def _probe_frontend_until_answered(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> FrontendProbe:
    """:func:`probe_frontend`, retrying until the service actually answers.

    Only a *non-answer* is retried: a verdict -- the placeholder, a bad status,
    a script served as HTML -- is the service telling us the frontend really is
    broken, and asking again reaches the same conclusion more slowly.
    """
    probe = probe_frontend(http, base_url)
    for _ in range(_FRONTEND_PROBE_ATTEMPTS - 1):
        if probe.is_answered:
            return probe
        sleeper(_FRONTEND_PROBE_INTERVAL_SECONDS)
        probe = probe_frontend(http, base_url)
    return probe


def describe_frontend_failure(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> str | None:
    """Return why the live UI is not serving a working frontend, or ``None``."""
    return _probe_frontend_until_answered(http, base_url, sleeper).failure


def refresh_workspace_view(repo_root: Path, runner: Runner) -> None:
    """Ask every open view of this workspace to reload the changed interface.

    Best-effort and never fatal: the change is already on disk and will load on
    the next visit regardless.
    """
    try:
        completed = runner.run(
            [sys.executable, str(repo_root / _REFRESH_SCRIPT)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_REFRESH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"refresh: could not run {_REFRESH_SCRIPT} ({type(exc).__name__}: {exc}); "
            "an open view may still be showing the previous build until reloaded.\n"
        )
        return
    if completed.stderr:
        sys.stderr.write(completed.stderr)
