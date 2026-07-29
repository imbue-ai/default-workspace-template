#!/usr/bin/env python3
"""Rebuild the user's view of this workspace after its interface changed.

Run this after anything that makes the running UI stale -- above all
``mngr start --restart system-services``, which bounces the system-interface
backend underneath whatever the user currently has open. Nothing else reloads
that view: the Minds app only intervenes when a workspace looks *unreachable*
for a sustained stretch, and a services restart that comes back quickly never
crosses that bar. Without this call the user keeps reading a page that was
rendered by the previous build.

Callers run this straight after the restart, so it first waits (bounded) for the
system interface to answer again. That wait is here rather than in each caller
because the broadcast below is a live fan-out with no replay: fired at a port
that is still down it is not retried, it is lost -- and with it the only channel
that reaches a shared viewer.

Two independent channels are then fired, because neither one reaches every viewer:

``reload_system_interface`` broadcast (the workspace's own WebSocket)
    Reaches every *browser* attached to the system interface -- the Minds app's
    view and, importantly, anyone the user shared the workspace with over a
    Cloudflare tunnel. Reloads the page. Cannot drop the browser's HTTP cache
    (see ``frontend/src/reload.ts``); it does not need to, because the shell
    document is served ``no-store`` and the assets it names are content-hashed.

``POST /api/v1/agents/<primary>/refresh`` (the Minds app, via the gateway)
    Reaches only the desktop app, but does what the broadcast cannot: it drops
    the workspace session's HTTP cache before reloading, and it works when the
    page's WebSocket never came back from the restart, so the broadcast landed
    nowhere.

Both are fire-and-forget and neither is fatal. A refresh is a courtesy to whoever
happens to be looking; the change itself has already landed on disk, and failing
a reveal because the user had the window shut would be worse than a stale tab.
Every outcome is reported on stderr and the exit code is always 0.

Run via bare ``python3`` (standard library only), like ``forward_port.py`` -- it
runs from restart paths that must not depend on a synced venv.

Usage:
    python3 system/scripts/refresh_workspace_view.py

Environment:
    MINDS_WORKSPACE_SERVER_URL  Base URL of the live workspace server
                                (default http://127.0.0.1:8000).
    MNGR_AGENT_ID               This agent's id. Sent for telemetry on the
                                broadcast, and the fallback target when the
                                primary agent cannot be resolved.
    LATCHKEY_GATEWAY,           Gateway address + credentials mngr injects into
    LATCHKEY_GATEWAY_PASSWORD,  the agent environment. All three must be present
    LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
                                to reach the Minds app; the broadcast still runs
                                without them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Sequence

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
ENV_MNGR_AGENT_ID = "MNGR_AGENT_ID"
MNGR_AGENT_ID_HEADER = "X-Mngr-Agent-Id"

ENV_GATEWAY = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"

RELOAD_OP = "reload_system_interface"

# Both HTTP calls are courtesies on a path the caller is blocking on, so they get
# a short leash: a wedged desktop app or frontend must not stall a reveal.
_TIMEOUT_SECONDS = 10.0

# The primary-agent lookup gets its own, longer budget. It is not an HTTP call to
# a service that is either up or wedged -- it is a ``mngr ls`` subprocess, and its
# cost is a Python interpreter start plus provider discovery. That is mostly page
# faults rather than compute, so it tracks how much memory the host has to spare:
# measured at ~1.5s idle in a workspace container, ~3s under CPU contention, and
# a 35s outlier on a host whose swap was ~90% full. Sharing the 10s leash above
# meant a loaded machine timed out here and fell back to the *calling* agent's id
# -- which addresses the wrong window, silently. Reuses the health probe's 30s
# ceiling below rather than inventing a second number: both answer "how long may
# this workspace take to respond before we stop waiting on it".
_PRIMARY_LOOKUP_TIMEOUT_SECONDS = 30.0

# Path that answers only once the system interface is serving again, and how long
# we will wait for it. Callers run this straight after
# ``mngr start --restart system-services``, when the server is still coming back
# up -- and the broadcast is a *live* fan-out to currently-connected sockets, with
# no replay, so firing it at a dead port does not just fail, it loses the only
# channel that reaches a shared tunnel viewer. Waiting here rather than in each
# caller keeps the three call sites (the reveal script, update-app, update-self)
# from drifting on it, which is the whole reason this motion is shared.
#
# Matches the reveal script's own post-restart probe (30 x 1s) so the two agree on
# how long a restart is allowed to take. This is a ceiling, not a cost: a healthy
# server answers the first probe, which is the common frontend-only case.
_HEALTH_PATH = "/api/agents"
_HEALTH_ATTEMPTS = 30
_HEALTH_INTERVAL_SECONDS = 1.0


# The Minds app identifies a *workspace* by its primary agent id, not by whoever
# is calling. A sub-agent (an /assist chat, a launch-task worker) has its own
# MNGR_AGENT_ID, so refreshing under that would address the wrong window -- or
# no window at all. Only this workspace's agents are visible from here, so
# exactly one carries ``is_primary``. Mirrors the resolution the ``assist``
# skill uses for bug reports.
#
# ``is_primary`` alone is the whole filter: minds dropped the ``workspace``
# label from its agents, so requiring it too matched nothing and sent every
# caller down the fall-back path below.
_PRIMARY_AGENT_QUERY = "has(labels.is_primary)"


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)


class HttpClient:
    """Indirection over the outbound HTTP so tests can intercept it."""

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


def _describe(status: int | None) -> str:
    """Render a POST outcome for a human reading stderr.

    ``None`` means the host never answered, which is a different situation from
    any status code and should not be reported as one.
    """
    return "was unreachable" if status is None else f"returned {status}"


def resolve_primary_agent_id(runner: Runner) -> str:
    """Return this workspace's primary agent id, falling back to our own.

    The fallback keeps the refresh addressed at *something* plausible when the
    lookup cannot run (no ``mngr`` on PATH, discovery erroring): in a workspace
    whose primary agent is the caller -- the common case for the update flows
    that run this -- our own id is the right answer anyway. It is reported on
    stderr because for a sub-agent it is the *wrong* answer, and the POST that
    follows succeeds either way.
    """
    own_id = os.environ.get(ENV_MNGR_AGENT_ID, "")

    def fall_back(reason: str) -> str:
        sys.stderr.write(
            f"refresh: could not resolve this workspace's primary agent id ({reason}); "
            "falling back to this agent's own id, which addresses the wrong window "
            "if this is a sub-agent.\n"
        )
        return own_id

    try:
        completed = runner.run(
            ["mngr", "ls", "--include", _PRIMARY_AGENT_QUERY, "--ids"],
            capture_output=True,
            text=True,
            timeout=_PRIMARY_LOOKUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return fall_back(f"{type(exc).__name__}: {exc}")
    if completed.returncode != 0:
        return fall_back(f"mngr ls exited {completed.returncode}")
    # ``--ids`` prints one id per line; a workspace has exactly one primary, but
    # take the first line rather than assuming the output is a single token.
    for line in (completed.stdout or "").splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return fall_back("mngr ls listed no primary agent")


def wait_until_serving(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> bool:
    """Poll until the system interface answers, or the attempt ceiling is reached.

    Returns whether it came back. A ``False`` is not fatal -- the caller still
    tries both channels, because a server we cannot probe may yet be reachable
    from a browser (a stale ``MINDS_WORKSPACE_SERVER_URL``, for instance), and
    the Minds app channel does not go through this server at all.
    """
    url = f"{base_url}{_HEALTH_PATH}"
    for attempt in range(_HEALTH_ATTEMPTS):
        if http.get_status(url, timeout=_TIMEOUT_SECONDS) == 200:
            return True
        if attempt < _HEALTH_ATTEMPTS - 1:
            sleeper(_HEALTH_INTERVAL_SECONDS)
    sys.stderr.write(
        f"refresh: the system interface did not answer within "
        f"{int(_HEALTH_ATTEMPTS * _HEALTH_INTERVAL_SECONDS)}s; refreshing anyway, but "
        "an attached browser (including a shared tunnel viewer) may still be showing "
        "the previous build.\n"
    )
    return False


def broadcast_reload(http: HttpClient, base_url: str) -> bool:
    """Tell every attached browser to reload the whole interface.

    Returns whether the broadcast was accepted. A non-200 does not mean no
    browser reloaded -- only that the workspace server did not take the op.
    """
    agent_id = os.environ.get(ENV_MNGR_AGENT_ID, "")
    status = http.post_json(
        f"{base_url}/api/layout/broadcast",
        {"op": RELOAD_OP, "args": {}, "agent_id": agent_id},
        {"Content-Type": "application/json", MNGR_AGENT_ID_HEADER: agent_id},
        timeout=_TIMEOUT_SECONDS,
    )
    if status == 200:
        return True
    sys.stderr.write(
        f"refresh: reload broadcast to the workspace server {_describe(status)}; any "
        "attached browser (including a shared tunnel viewer) may still be showing "
        "the previous build.\n"
    )
    return False


def request_app_refresh(http: HttpClient, primary_agent_id: str) -> bool:
    """Ask the Minds app to drop its cache and reload its view of this workspace.

    Returns whether the app accepted the request. Absent gateway env means we are
    not running under a Minds desktop app at all (a bare ``mngr`` workspace, a
    test harness), which is not a failure -- the broadcast alone is the whole
    story there.
    """
    gateway = os.environ.get(ENV_GATEWAY, "")
    password = os.environ.get(ENV_GATEWAY_PASSWORD, "")
    permissions = os.environ.get(ENV_GATEWAY_PERMISSIONS, "")
    if not gateway or not password or not permissions:
        sys.stderr.write(
            "refresh: latchkey gateway env not set; skipping the Minds app refresh "
            "(the reload broadcast still went out).\n"
        )
        return False
    if not primary_agent_id:
        sys.stderr.write(
            "refresh: could not resolve this workspace's agent id; skipping the "
            "Minds app refresh (the reload broadcast still went out).\n"
        )
        return False
    status = http.post_json(
        f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/agents/{primary_agent_id}/refresh",
        {},
        {
            "Content-Type": "application/json",
            "X-Latchkey-Gateway-Password": password,
            "X-Latchkey-Gateway-Permissions-Override": permissions,
        },
        timeout=_TIMEOUT_SECONDS,
    )
    if status == 200:
        return True
    sys.stderr.write(
        f"refresh: Minds app refresh {_describe(status)}; if the app has this "
        "workspace open it may still be showing the previous build.\n"
    )
    return False


def refresh(
    *,
    runner: Runner,
    http: HttpClient,
    base_url: str | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Fire both refresh channels. Always returns 0 -- see the module docstring."""
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    # Wait before either channel. Callers run this immediately after restarting
    # the services agent, and the broadcast has no replay -- fired at a port that
    # is still down it is simply lost, taking shared tunnel viewers with it. The
    # app channel does not need the wait, but benefits: its reload then lands on
    # the real interface instead of the loading page.
    wait_until_serving(http, resolved_base, sleeper)
    # Deliberately unconditional and independent: each channel reaches viewers
    # the other cannot, and a failure of one says nothing about the other.
    broadcast_ok = broadcast_reload(http, resolved_base)
    app_ok = request_app_refresh(http, resolve_primary_agent_id(runner))
    if broadcast_ok or app_ok:
        sys.stderr.write("refresh: requested a reload of this workspace's view.\n")
    else:
        sys.stderr.write(
            "refresh: no refresh channel succeeded; the change is on disk but an "
            "open view may still be showing the previous build until reloaded.\n"
        )
    return 0


def main() -> int:
    return refresh(runner=Runner(), http=HttpClient())


if __name__ == "__main__":
    sys.exit(main())
