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

from update_banding import ExpendWrapper
from update_banding import as_expendable
from update_layout import SYSTEM_INTERFACE_DIR
from update_layout import TOOL_NAME
from update_runtime import FrontendProbe
from update_runtime import HttpClient
from update_runtime import Runner
from update_runtime import Spawner
from update_runtime import find_free_port
from update_runtime import tail

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

# Endpoints used to probe liveness. ``/api/agents`` exercises the mngr plugin
# discovery path -- exactly what a missing backend dependency or a broken
# plugin-config parse would take down.
HEALTH_PATH = "/api/agents"

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
    wrote, or, for a boot that could not be spawned at all, a line saying so."""
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
                f"http://127.0.0.1:{port}{HEALTH_PATH}",
                _PREFLIGHT_ATTEMPTS,
                _PREFLIGHT_INTERVAL_SECONDS,
                sleeper,
                should_stop=spawned.has_exited,
            ):
                return None
        finally:
            spawned.terminate()
        return tail(spawned.read_output(), _PREFLIGHT_OUTPUT_TAIL_LINES)


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
